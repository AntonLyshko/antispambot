import logging
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, CAPTCHA_TIMEOUT, LOG_GROUP_ID
from moderation_api import check_message
from cas_check import check_cas
from antispam import check_flood, MUTE_DURATION

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

VERIFIED_FILE = "verified_users.json"

# Порог для автобана спама (90%)
SPAM_AUTO_BAN_THRESHOLD = 90


def load_verified() -> set[tuple[int, int]]:
    if not os.path.exists(VERIFIED_FILE):
        return set()
    try:
        with open(VERIFIED_FILE, "r") as f:
            data = json.load(f)
        return {(item[0], item[1]) for item in data}
    except Exception:
        return set()


def save_verified():
    try:
        data = [[c, u] for c, u in verified_users]
        with open(VERIFIED_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error("Сохранение: %s", e)


verified_users: set[tuple[int, int]] = load_verified()
pending_users: dict[tuple[int, int], dict] = {}
known_users: set[tuple[int, int]] = set()
admins_loaded: set[int] = set()

MUTED = ChatPermissions(
    can_send_messages=False, can_send_audios=False,
    can_send_documents=False, can_send_photos=False,
    can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False,
    can_send_other_messages=False, can_add_web_page_previews=False,
    can_change_info=False, can_invite_users=False,
    can_pin_messages=False, can_manage_topics=False,
)

UNMUTED = ChatPermissions(
    can_send_messages=True, can_send_audios=True,
    can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_change_info=False, can_invite_users=True,
    can_pin_messages=False, can_manage_topics=False,
)


async def send_log(context, text, reply_markup=None):
    if not LOG_GROUP_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID, text=text,
            parse_mode="HTML", reply_markup=reply_markup,
        )
    except Exception as e:
        logger.error("Лог: %s", e)


async def auto_delete(context, chat_id, message_id, delay=15):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# ==========================================
# КАПЧА
# ==========================================

async def load_admins(chat_id, context):
    if chat_id in admins_loaded:
        return
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for admin in admins:
            known_users.add((chat_id, admin.user.id))
        admins_loaded.add(chat_id)
    except Exception as e:
        logger.error("Админы: %s", e)


async def start_verification(chat_id, user_id, user_name, context):
    key = (chat_id, user_id)
    if key in pending_users or key in verified_users or key in known_users:
        return

    # === CAS ===
    is_spammer = await check_cas(user_id)
    if is_spammer:
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logger.error("CAS бан %d: %s", user_id, e)

        await send_log(context,
            f"🚫 <b>CAS — спамер заблокирован</b>\n\n"
            f"👤 {user_name}\n"
            f"ID: <code>{user_id}</code>\n"
            f"🔍 Источник: CAS (api.cas.chat)\n"
            f"⚡ Действие: Бан при входе"
        )
        return

    # === Капча ===
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id, permissions=MUTED,
        )
    except Exception as e:
        logger.error("Мьют %d: %s", user_id, e)
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Я не бот — пройти проверку",
            callback_data=f"verify_{user_id}",
        )
    ]])

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text="👋 Новый участник, пройдите проверку!\n\nНажмите кнопку ниже.",
        reply_markup=keyboard,
    )

    pending_users[key] = {"message_id": sent.message_id, "user_name": user_name}

    await send_log(context,
        f"👤 <b>Новый участник</b>\n"
        f"Имя: {user_name}\n"
        f"ID: <code>{user_id}</code>\n"
        f"CAS: ✅ чист\n"
        f"Ожидает проверку..."
    )

    context.application.create_task(
        kick_if_not_verified(context, chat_id, user_id, user_name)
    )


async def kick_if_not_verified(context, chat_id, user_id, user_name):
    await asyncio.sleep(CAPTCHA_TIMEOUT)
    key = (chat_id, user_id)
    data = pending_users.pop(key, None)
    if data is None:
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=data["message_id"])
    except Exception:
        pass

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logger.error("Кик %d: %s", user_id, e)
        return

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=f"❌ <b>{user_name}</b> не прошёл проверку и был удалён.",
        parse_mode="HTML",
    )

    context.application.create_task(auto_delete(context, chat_id, sent.message_id, 15))

    await send_log(context,
        f"❌ <b>Не прошёл проверку</b>\n"
        f"Имя: {user_name}\n"
        f"ID: <code>{user_id}</code>\n"
        f"Удалён по таймауту."
    )


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    await load_admins(msg.chat_id, context)
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        await start_verification(msg.chat_id, member.id, member.full_name, context)


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return
    old = result.old_chat_member.status
    new = result.new_chat_member.status
    user = result.new_chat_member.user
    if old in ("left", "kicked") and new in ("member", "restricted"):
        if user.is_bot:
            return
        await load_admins(result.chat.id, context)
        await start_verification(result.chat.id, user.id, user.full_name, context)


async def on_verify_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.data.startswith("verify_"):
        return

    expected = int(query.data.split("_")[1])
    actual = query.from_user.id
    chat_id = query.message.chat_id

    if actual != expected:
        await query.answer("⛔ Эта кнопка не для вас!", show_alert=True)
        return

    key = (chat_id, actual)
    if key not in pending_users:
        await query.answer("Проверка уже завершена.")
        return

    pending_users.pop(key)
    verified_users.add(key)
    save_verified()

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=actual, permissions=UNMUTED,
        )
    except Exception as e:
        logger.error("Размьют %d: %s", actual, e)

    await query.answer("✅ Проверка пройдена!")

    msg_id = query.message.message_id
    try:
        await query.message.edit_text("✅ Проверка пройдена. Добро пожаловать!")
    except Exception:
        pass

    await send_log(context,
        f"✅ <b>Проверка пройдена</b>\n"
        f"Имя: {query.from_user.full_name}\n"
        f"ID: <code>{actual}</code>"
    )

    context.application.create_task(auto_delete(context, chat_id, msg_id, 15))


# ==========================================
# МОДЕРАЦИЯ + АНТИФЛУД
# ==========================================

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    if msg.new_chat_members or msg.left_chat_member:
        return

    chat_id = msg.chat_id
    user_id = msg.from_user.id
    key = (chat_id, user_id)

    if key in pending_users:
        try:
            await msg.delete()
        except Exception:
            pass
        return

    if key not in verified_users and key not in known_users:
        if msg.from_user.is_bot:
            known_users.add(key)
            return
        await load_admins(chat_id, context)
        if key in known_users:
            return
        verified_users.add(key)
        save_verified()

    # === АНТИФЛУД (админов не трогаем) ===
    if key not in known_users:
        is_flood = check_flood(chat_id, user_id)
        if is_flood:
            user_name = msg.from_user.full_name
            username = msg.from_user.username or ""

            try:
                until = datetime.now(timezone.utc) + timedelta(seconds=MUTE_DURATION)
                await context.bot.restrict_chat_member(
                    chat_id, user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
            except Exception as e:
                logger.error("Мут флудера %d: %s", user_id, e)

            try:
                await msg.delete()
            except Exception:
                pass

            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔇 <b>{user_name}</b> замьючен на 5 минут за флуд.",
                parse_mode="HTML",
            )

            context.application.create_task(auto_delete(context, chat_id, sent.message_id, 15))

            await send_log(context,
                f"🔇 <b>Антифлуд</b>\n\n"
                f"👤 {user_name} (@{username})\n"
                f"ID: <code>{user_id}</code>\n\n"
                f"📊 Превышен лимит сообщений\n"
                f"⚡ Действие: Мут на 5 минут"
            )
            return

    # === МОДЕРАЦИЯ ===
    text = msg.text or msg.caption
    if not text:
        return

    # Определяем контекст: это ответ на другое сообщение?
    is_reply = msg.reply_to_message is not None

    result = await check_message(text, is_reply=is_reply)

    if result:
        top = sorted(result["scores"].items(), key=lambda x: x[1], reverse=True)[:5]
        top_str = ", ".join([f"{cat}: {score:.4f}" for cat, score in top])
        logger.info(
            "📝 [%s] flagged=%s src=%s is_reply=%s | %s | текст: %s",
            msg.from_user.full_name,
            result["flagged"],
            result.get("source", "?"),
            is_reply,
            top_str,
            text[:100],
        )
    else:
        return

    if not result["flagged"]:
        # === Логируем отклонённые Claude (для отладки) ===
        source = result.get("source", "?")
        if source == "openai_rejected_by_claude":
            user_name = msg.from_user.full_name
            username = msg.from_user.username or ""
            claude_answer = result.get("claude_answer", "?")

            top = sorted(result["scores"].items(), key=lambda x: x[1], reverse=True)[:3]
            top_str = "\n".join([
                f"  • {cat}: {score:.0%}" for cat, score in top
            ])

            await send_log(context,
                f"ℹ️ <b>OpenAI flagged → Claude отклонил</b>\n\n"
                f"👤 {user_name} (@{username})\n"
                f"ID: <code>{user_id}</code>\n\n"
                f"💬 <i>{text[:300]}</i>\n\n"
                f"📊 OpenAI оценки:\n{top_str}\n\n"
                f"🤖 Claude ответ: {claude_answer}\n"
                f"⚡ Действие: Нет (ложное срабатывание)"
            )

        elif source == "openai_unconfirmed":
            user_name = msg.from_user.full_name
            username = msg.from_user.username or ""

            top = sorted(result["scores"].items(), key=lambda x: x[1], reverse=True)[:3]
            top_str = "\n".join([
                f"  • {cat}: {score:.0%}" for cat, score in top
            ])

            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚫 Забанить",
                        callback_data=f"mod_{chat_id}_{user_id}_ban",
                    ),
                    InlineKeyboardButton(
                        "🗑 Удалить сообщение",
                        callback_data=f"mod_{chat_id}_{user_id}_kick",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔇 Мут 1 час",
                        callback_data=f"mod_{chat_id}_{user_id}_mute1h",
                    ),
                    InlineKeyboardButton(
                        "✅ OK",
                        callback_data=f"mod_{chat_id}_{user_id}_ok",
                    ),
                ],
            ])

            await send_log(context,
                f"⚠️ <b>OpenAI flagged, Claude недоступен — на рассмотрение</b>\n\n"
                f"👤 {user_name} (@{username})\n"
                f"ID: <code>{user_id}</code>\n\n"
                f"💬 <i>{text[:300]}</i>\n\n"
                f"📊 OpenAI оценки:\n{top_str}\n\n"
                f"🤖 Claude: недоступен\n"
                f"⚡ Действие: Нет (ожидает решения админа)",
                reply_markup=buttons,
            )

        return

    user_name = msg.from_user.full_name
    username = msg.from_user.username or ""
    source = result.get("source", "?")

    cat_names = {
        "harassment": "Оскорбление",
        "harassment/threatening": "Угроза",
        "hate": "Ненависть",
        "hate/threatening": "Угроза ненависти",
        "violence": "Насилие",
        "violence/graphic": "Жёсткий контент",
        "sexual": "Сексуальный контент",
        "sexual/minors": "Сексуализация детей",
        "self-harm": "Самоповреждение",
        "self-harm/intent": "Намерение самоповреждения",
        "self-harm/instructions": "Инструкция самоповреждения",
        "illicit": "Незаконное",
        "illicit/violent": "Незаконное насилие",
        "religious_insult": "Религиозное оскорбление",
        "spam": "Спам / Реклама",
    }

    source_names = {
        "openai": "OpenAI Moderation",
        "openai+claude": "OpenAI + Claude (подтверждено)",
        "claude": "Claude AI (религия)",
        "claude_spam": "Claude AI (антиспам)",
    }

    # === СПАМ: отдельная логика с порогами ===
    if source == "claude_spam":
        spam_confidence = result.get("spam_confidence", 0)
        url_info_list = result.get("url_info", [])

        # Формируем строку с информацией о ссылках
        url_info_str = ""
        if url_info_list:
            url_parts = []
            for info in url_info_list:
                if info:
                    url_parts.append(
                        f"  🔗 {info['url']}\n"
                        f"     📄 {info['title']}"
                    )
            if url_parts:
                url_info_str = "\n".join(url_parts)

        if spam_confidence >= SPAM_AUTO_BAN_THRESHOLD:
            # === 90%+ → автобан ===
            logger.info("🚨 СПАМ-БАН: [%s] confidence=%d%% | %s",
                        user_name, spam_confidence, text[:200])

            try:
                await msg.delete()
            except Exception as e:
                logger.error("Удаление: %s", e)

            try:
                await context.bot.ban_chat_member(chat_id, user_id)
            except Exception as e:
                logger.error("Бан: %s", e)

            log_text = (
                f"🚨 <b>Спамер заблокирован</b>\n\n"
                f"👤 {user_name} (@{username})\n"
                f"ID: <code>{user_id}</code>\n\n"
                f"💬 <i>{text[:500]}</i>\n\n"
            )
            if url_info_str:
                log_text += f"🌐 <b>Ссылки:</b>\n{url_info_str}\n\n"
            log_text += (
                f"📊 Уверенность: <b>{spam_confidence}%</b>\n"
                f"🔍 Источник: {source_names.get(source, source)}\n"
                f"⚡ Действие: Автобан (≥{SPAM_AUTO_BAN_THRESHOLD}%)"
            )

            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Разбанить",
                    callback_data=f"mod_{chat_id}_{user_id}_unban",
                ),
            ]])

            await send_log(context, log_text, reply_markup=buttons)

        else:
            # === 80-89% → на рассмотрение, БЕЗ действий ===
            logger.info("⚠️ СПАМ-РЕВЬЮ: [%s] confidence=%d%% | %s",
                        user_name, spam_confidence, text[:200])

            log_text = (
                f"⚠️ <b>Подозрение на спам — на рассмотрение</b>\n\n"
                f"👤 {user_name} (@{username})\n"
                f"ID: <code>{user_id}</code>\n\n"
                f"💬 <i>{text[:500]}</i>\n\n"
            )
            if url_info_str:
                log_text += f"🌐 <b>Ссылки:</b>\n{url_info_str}\n\n"
            log_text += (
                f"📊 Уверенность: <b>{spam_confidence}%</b>\n"
                f"🔍 Источник: {source_names.get(source, source)}\n"
                f"⚡ Действие: Нет (ожидает решения админа)"
            )

            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚫 Забанить",
                        callback_data=f"mod_{chat_id}_{user_id}_ban",
                    ),
                    InlineKeyboardButton(
                        "🗑 Удалить",
                        callback_data=f"mod_{chat_id}_{user_id}_kick",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔇 Мут 1 час",
                        callback_data=f"mod_{chat_id}_{user_id}_mute1h",
                    ),
                    InlineKeyboardButton(
                        "✅ OK (не спам)",
                        callback_data=f"mod_{chat_id}_{user_id}_ok",
                    ),
                ],
            ])

            await send_log(context, log_text, reply_markup=buttons)

        return

    # === ТОКСИЧНОСТЬ (OpenAI+Claude подтверждено, или Claude религия) — БАН ===
    top = sorted(result["scores"].items(), key=lambda x: x[1], reverse=True)[:3]
    top_str = "\n".join([
        f"  • {cat_names.get(cat, cat)}: {score:.0%}" for cat, score in top
    ])

    claude_note = ""
    if source == "openai+claude":
        claude_answer = result.get("claude_answer", "BAN")
        claude_note = f"\n🤖 Claude подтверждение: {claude_answer}"

    logger.info("🚨 БАН: [%s] src=%s | %s", user_name, source, text[:200])

    try:
        await msg.delete()
    except Exception as e:
        logger.error("Удаление: %s", e)

    try:
        await context.bot.ban_chat_member(_mod_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    if len(parts) != 4:
        return

    _, chat_id, user_id, action = parts
    chat_id = int(chat_id)
    user_id = int(user_id)
    admin = query.from_user.full_name

    try:
        if action == "unban":
            await context.bot.unban_chat_member(chat_id, user_id)
            result = f"✅ Разбанен админом {admin}"

        elif action == "ban":
            await context.bot.ban_chat_member(chat_id, user_id)
            result = f"🚫 Забанен админом {admin}"

        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
            result = f"🗑 Удалён админом {admin}"

        elif action == "mute1h":
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            result = f"🔇 Замьючен на 1 час админом {admin}"

        elif action == "muteforever":
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
            result = f"🔇 Замьючен навсегда админом {admin}"

        elif action == "ok":
            result = f"✅ Одобрено админом {admin} (не спам)"

        else:
            await query.answer("Неизвестное действие")
            return

        await query.answer(result, show_alert=True)
        old_text = query.message.text_html or query.message.text
        await query.edit_message_text(
            text=old_text + f"\n\n<b>{result}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await query.answer(f"Ошибка: {e}", show_alert=True)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Chat ID: <code>{update.effective_chat.id}</code>",
        parse_mode="HTML",
    )


def main():
    if not BOT_TOKEN:
        print("ОШИБКА: BOT_TOKEN не задан!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members,
    ))
    app.add_handler(ChatMemberHandler(
        on_chat_member_update, ChatMemberHandler.CHAT_MEMBER,
    ))
    app.add_handler(CallbackQueryHandler(
        on_verify_button, pattern=r"^verify_\d+$",
    ))
    app.add_handler(CallbackQueryHandler(
        on_mod_button, pattern=r"^mod_",
    ))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
        on_any_message,
    ))

    print("=" * 60)
    print("✅ Бот запущен!")
    print(f"   Верифицированных в базе: {len(verified_users)}")
    print(f"   Порог автобана спама: {SPAM_AUTO_BAN_THRESHOLD}%")
    print(f"   Двойная проверка: OpenAI → Claude")
    print("=" * 60)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()