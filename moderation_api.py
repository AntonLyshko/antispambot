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


async def check_claude_spam(text: str) -> dict | None:
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
                                "Ты модератор группового чата. Определи: является ли это сообщение РЕКЛАМОЙ или СПАМОМ?\n\n"
                                "SPAM — это:\n"
                                "- Реклама заработка, схем, инвестиций, казино, ставок\n"
                                "- Реклама каналов, ботов, групп с призывом подписаться\n"
                                "- Продажа товаров/услуг (не в тему чата)\n"
                                "- Реклама непристойных услуг, интим-контента\n"
                                "- Массовая рассылка, копипаста с призывом писать в ЛС\n"
                                "- Мошенничество, фишинг, подозрительные ссылки\n\n"
                                "OK — это:\n"
                                "- Обычная ссылка в контексте обсуждения (YouTube видео по теме, статья)\n"
                                "- Рекомендация друга (не массовая реклама)\n"
                                "- Ссылка на источник в споре/обсуждении\n"
                                "- Свой контент по теме чата без агрессивного продвижения\n\n"
                                "Ответь ОДНИМ словом: SPAM или OK.\n\n"
                                f"Сообщение: {text}"
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

        answer = data["content"][0]["text"].strip().upper()
        is_spam = "SPAM" in answer

        logger.info("📩 Claude спам: '%s' → %s", text[:100], answer)

        return {
            "flagged": is_spam,
            "categories": {"spam": is_spam},
            "scores": {"spam": 1.0 if is_spam else 0.0},
            "source": "claude_spam",
        }
    except Exception as e:
        logger.error("Claude spam error: %s", e)
        return None


async def check_message(text: str) -> dict | None:
    if not text or len(text.strip()) < 2:
        return None

    # 1. OpenAI Moderation
    result = await check_openai(text)
    if result and result["flagged"]:
        return result

    # 2. Триггер → Claude религия
    if TRIGGER_PATTERN.search(text):
        logger.info("🔍 Триггер найден в: %s", text[:100])
        claude_result = await check_claude_religion(text)
        if claude_result and claude_result["flagged"]:
            return claude_result

    # 3. Ссылка → Claude спам
    if URL_PATTERN.search(text):
        logger.info("🔗 Ссылка найдена в: %s", text[:100])
        spam_result = await check_claude_spam(text)
        if spam_result and spam_result["flagged"]:
            return spam_result

    # 4. Всё чисто
    if result:
        return result
    return None