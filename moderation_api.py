import aiohttp
import logging
import re
from config import OPENAI_API_KEY, CLAUDE_API_KEY
from triggers import RELIGION_TRIGGERS

logger = logging.getLogger(__name__)

TRIGGER_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in RELIGION_TRIGGERS) + r")\b",
    re.IGNORECASE,
)

URL_PATTERN = re.compile(
    r"(https?://|t\.me/|@[\w]+|www\.)\S+",
    re.IGNORECASE,
)

# Паттерн для извлечения полных URL (только http/https/www)
FULL_URL_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+)",
    re.IGNORECASE,
)


async def fetch_url_info(url: str) -> dict | None:
    """
    Пытается получить заголовок страницы по URL.
    """
    if not url.startswith("http"):
        url = "https://" + url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=8),
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ModBot/1.0)"},
            ) as resp:
                status = resp.status
                final_url = str(resp.url)

                if resp.content_type and "text/html" not in resp.content_type:
                    return {
                        "url": final_url,
                        "title": f"[{resp.content_type}]",
                        "status": status,
                    }

                chunk = await resp.content.read(16384)
                text = chunk.decode("utf-8", errors="ignore")

                import re as _re
                title_match = _re.search(
                    r"<title[^>]*>(.*?)</title>",
                    text,
                    _re.IGNORECASE | _re.DOTALL,
                )
                title = title_match.group(1).strip() if title_match else ""
                title = " ".join(title.split())
                if len(title) > 200:
                    title = title[:200] + "…"

                return {
                    "url": final_url,
                    "title": title or "(без заголовка)",
                    "status": status,
                }
    except Exception as e:
        logger.debug("fetch_url_info(%s): %s", url, e)
        return None


async def check_openai(text: str) -> dict | None:
    if not OPENAI_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/moderations",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                },
                json={"model": "omni-moderation-latest", "input": text},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.error("OpenAI %d: %s", resp.status, (await resp.text())[:300])
                    return None
                data = await resp.json()
        result = data["results"][0]
        return {
            "flagged": result["flagged"],
            "categories": result["categories"],
            "scores": result["category_scores"],
            "source": "openai",
        }
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        return None


async def check_claude_confirm(text: str, openai_categories: dict, openai_scores: dict) -> dict | None:
    """
    Перепроверяет сообщение, помеченное OpenAI, через Claude
    с учётом контекста исламского чата.

    Возвращает результат с подтверждением или отклонением.
    """
    if not CLAUDE_API_KEY:
        return None

    # Собираем сработавшие категории OpenAI для контекста
    triggered_cats = []
    cat_names_ru = {
        "harassment": "оскорбление/травля",
        "harassment/threatening": "угрозы расправы",
        "hate": "разжигание ненависти",
        "hate/threatening": "ненависть + угрозы",
        "violence": "насилие",
        "violence/graphic": "графическое насилие",
        "sexual": "сексуальный контент",
        "sexual/minors": "сексуализация детей",
        "self-harm": "самоповреждение",
        "self-harm/intent": "намерение самоповреждения",
        "self-harm/instructions": "инструкции самоповреждения",
        "illicit": "незаконная деятельность",
        "illicit/violent": "незаконное насилие",
    }

    for cat, is_flagged in openai_categories.items():
        if is_flagged:
            score = openai_scores.get(cat, 0)
            name = cat_names_ru.get(cat, cat)
            triggered_cats.append(f"- {name}: {score:.0%}")

    openai_context = "\n".join(triggered_cats) if triggered_cats else "- общее нарушение"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 50,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Ты модератор исламского группового чата в Telegram. "
                                "В чате обсуждают религию, исламскую культуру, повседневную жизнь.\n\n"

                                "Система автомодерации (OpenAI) пометила сообщение как нарушение:\n"
                                f"{openai_context}\n\n"

                                "Твоя задача — ПЕРЕПРОВЕРИТЬ. Учитывай контекст исламского чата.\n\n"

                                "ПОДТВЕРДИ нарушение (ответь BAN), если сообщение содержит:\n"
                                "- Прямые оскорбления конкретных людей или групп\n"
                                "- Разжигание ненависти к этническим/религиозным группам\n"
                                "- Реальные угрозы насилия\n"
                                "- Призывы к насилию или дискриминации\n"
                                "- Сексуальный контент, порнографию\n"
                                "- Контент с детьми сексуального характера\n"
                                "- Инструкции по причинению вреда\n\n"

                                "ОТКЛОНИ (ответь OK), если:\n"
                                "- Это обсуждение религиозных тем (джихад, шахид и т.п. в религиозном контексте)\n"
                                "- Цитирование Корана, хадисов, учёных\n"
                                "- Обсуждение исторических событий (войны, конфликты)\n"
                                "- Эмоциональное но не оскорбительное высказывание\n"
                                "- Критика идей/действий без оскорбления людей\n"
                                "- Обсуждение правил шариата, наказаний в исламе\n"
                                "- Религиозные термины в нормальном контексте\n"
                                "- Обсуждение политических событий без призывов к насилию\n"
                                "- Спор/дискуссия на повышенных тонах без прямых оскорблений\n\n"

                                "Помни: в исламском чате многие темы, которые OpenAI считает "
                                "нарушением, являются нормальным обсуждением религии.\n"
                                "Лучше пропустить сомнительное, чем забанить невиновного.\n\n"

                                "Ответь ОДНИМ словом: BAN или OK\n\n"
                                f"Сообщение: {text}"
                            ),
                        }
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.error("Claude confirm %d: %s", resp.status, (await resp.text())[:300])
                    return None
                data = await resp.json()

        answer = data["content"][0]["text"].strip().upper()
        confirmed = "BAN" in answer

        logger.info(
            "🔍 Claude перепроверка: '%s' → %s (OpenAI категории: %s)",
            text[:100], answer, ", ".join(
                cat for cat, flagged in openai_categories.items() if flagged
            ),
        )

        return {
            "confirmed": confirmed,
            "claude_answer": answer,
        }

    except Exception as e:
        logger.error("Claude confirm error: %s", e)
        return None


async def check_claude_religion(text: str) -> dict | None:
    if not CLAUDE_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 10,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Ты модератор исламского чата. "
                                "Определи: используется ли религиозный термин в этом сообщении "
                                "как ОСКОРБЛЕНИЕ человека или группы людей? "
                                "Учитывай контекст. Если это просто обсуждение религии без оскорбления — это OK. "
                                "Ответь ОДНИМ словом: TOXIC или OK.\n\n"
                                f"Сообщение: {text}"
                            ),
                        }
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.error("Claude %d: %s", resp.status, (await resp.text())[:300])
                    return None
                data = await resp.json()

        answer = data["content"][0]["text"].strip().upper()
        is_toxic = "TOXIC" in answer

        logger.info("🕌 Claude религия: '%s' → %s", text[:100], answer)

        return {
            "flagged": is_toxic,
            "categories": {"religious_insult": is_toxic},
            "scores": {"religious_insult": 1.0 if is_toxic else 0.0},
            "source": "claude",
        }
    except Exception as e:
        logger.error("Claude religion error: %s", e)
        return None


async def check_claude_spam(text: str, url_info_list: list[dict] | None = None) -> dict | None:
    """
    Проверяет текст на спам с помощью Claude.
    Возвращает confidence (0-100).
    Толерантный промпт: пропускаем всё кроме явного спама.
    """
    if not CLAUDE_API_KEY:
        return None

    # Дополняем промпт информацией о ссылках
    url_context = ""
    if url_info_list:
        url_parts = []
        for info in url_info_list:
            if info:
                url_parts.append(
                    f"  URL: {info['url']}\n  Заголовок страницы: {info['title']}"
                )
        if url_parts:
            url_context = (
                "\n\nИнформация о ссылках в сообщении:\n" +
                "\n".join(url_parts)
            )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 20,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Ты модератор исламского группового чата. "
                                "Участники часто делятся полезными ссылками: лекции, статьи, "
                                "YouTube видео, исламские ресурсы, новости, Telegram каналы по теме.\n\n"
                                "Твоя задача — ловить ТОЛЬКО ЯВНЫЙ СПАМ. Будь максимально толерантен.\n\n"
                                "Это ТОЧНО СПАМ (80-100):\n"
                                "- Реклама заработка, финансовых схем, казино, ставок, крипто-скамов\n"
                                "- Реклама интим-услуг, порно, знакомств\n"
                                "- Мошенничество, фишинг, 'вы выиграли приз'\n"
                                "- Массовая рассылка с призывом 'пишите в ЛС'\n"
                                "- Продажа поддельных документов, наркотиков\n\n"
                                "Это НЕ СПАМ (0-30):\n"
                                "- Ссылка на YouTube видео, даже не по теме\n"
                                "- Ссылка на статью, новость, блог\n"
                                "- Ссылка на Telegram канал (даже свой) без агрессивного продвижения\n"
                                "- Рекомендация приложения, книги, ресурса\n"
                                "- Любая ссылка в контексте обсуждения\n"
                                "- Ссылка на исламский ресурс, лекцию, учёного\n"
                                "- Ссылка на магазин (халяль продукты, одежда и т.п.)\n"
                                "- Всё что похоже на обычное общение между участниками\n\n"
                                "СОМНИТЕЛЬНО (40-70) — только если реально подозрительно:\n"
                                "- Ссылка вообще без контекста от нового участника\n"
                                "- Подозрительный домен + подозрительный текст\n\n"
                                "Помни: лучше пропустить спам, чем заблокировать обычного участника.\n\n"
                                "Ответь ОДНИМ числом от 0 до 100 — уверенность что это СПАМ.\n"
                                "Только число, ничего больше.\n\n"
                                f"Сообщение: {text}"
                                f"{url_context}"
                            ),
                        }
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.error("Claude spam %d: %s", resp.status, (await resp.text())[:300])
                    return None
                data = await resp.json()

        answer = data["content"][0]["text"].strip()

        import re as _re
        num_match = _re.search(r"\d+", answer)
        if num_match:
            confidence = int(num_match.group())
            confidence = max(0, min(100, confidence))
        else:
            if "SPAM" in answer.upper():
                confidence = 85
            else:
                confidence = 10

        logger.info("📩 Claude спам: '%s' → %d%% | url_info=%s",
                     text[:100], confidence,
                     [i.get("title", "?") if i else "?" for i in (url_info_list or [])])

        score = confidence / 100.0

        return {
            "flagged": confidence >= 80,  # помечаем только от 80%
            "categories": {"spam": confidence >= 80},
            "scores": {"spam": score},
            "source": "claude_spam",
            "spam_confidence": confidence,
            "url_info": url_info_list,
        }
    except Exception as e:
        logger.error("Claude spam error: %s", e)
        return None


async def check_message(text: str, is_reply: bool = False) -> dict | None:
    """
    Проверяет сообщение.
    is_reply — True если сообщение является ответом на другое сообщение.
    """
    if not text or len(text.strip()) < 2:
        return None

    # 1. OpenAI Moderation
    openai_result = await check_openai(text)

    if openai_result and openai_result["flagged"]:
        # === ДВОЙНАЯ ПРОВЕРКА: OpenAI flagged → отправляем в Claude ===
        logger.info(
            "⚠️ OpenAI flagged: '%s' — отправляем на перепроверку в Claude",
            text[:100],
        )

        claude_confirm = await check_claude_confirm(
            text,
            openai_result["categories"],
            openai_result["scores"],
        )

        if claude_confirm is None:
            # Claude недоступен — НЕ баним, отправляем на ревью
            logger.warning(
                "⚠️ Claude недоступен для перепроверки, пропускаем: '%s'",
                text[:100],
            )
            openai_result["flagged"] = False
            openai_result["claude_unavailable"] = True
            openai_result["source"] = "openai_unconfirmed"
            return openai_result

        if claude_confirm["confirmed"]:
            # Claude подтвердил — БАНИТЬ
            logger.info(
                "✅ Claude ПОДТВЕРДИЛ нарушение: '%s' → BAN",
                text[:100],
            )
            openai_result["source"] = "openai+claude"
            openai_result["claude_answer"] = claude_confirm["claude_answer"]
            return openai_result
        else:
            # Claude отклонил — НЕ банить
            logger.info(
                "❌ Claude ОТКЛОНИЛ нарушение: '%s' → OK (ложное срабатывание)",
                text[:100],
            )
            openai_result["flagged"] = False
            openai_result["source"] = "openai_rejected_by_claude"
            openai_result["claude_answer"] = claude_confirm["claude_answer"]
            # Возвращаем как не-flagged, чтобы в логах было видно
            return openai_result

    # 2. Триггер → Claude религия
    if TRIGGER_PATTERN.search(text):
        logger.info("🔍 Триггер найден в: %s", text[:100])
        claude_result = await check_claude_religion(text)
        if claude_result and claude_result["flagged"]:
            return claude_result

    # 3. Ссылка → Claude спам
    #    Если сообщение — ответ (reply), пропускаем проверку ссылок полностью
    if URL_PATTERN.search(text) and not is_reply:
        logger.info("🔗 Ссылка найдена в: %s", text[:100])

        # Получаем информацию о ссылках
        urls = FULL_URL_PATTERN.findall(text)
        url_info_list = []
        for url in urls[:3]:  # Максимум 3 ссылки
            info = await fetch_url_info(url)
            url_info_list.append(info)

        spam_result = await check_claude_spam(text, url_info_list)
        if spam_result and spam_result["flagged"]:
            return spam_result

    # 4. Всё чисто
    if openai_result:
        return openai_result
    return None