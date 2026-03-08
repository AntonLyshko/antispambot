import logging
import json
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes

from config import CAPTCHA_TIMEOUT, AUTO_ACTION
from moderation_api import analyze_text, check_thresholds
from db import (
    upsert_user,
    set_verified,
    get_verification,
    add_warning,
    count_warnings,
)
from moderation_log import (
    log_failed_verification,
    log_new_user,
    log_verified,
    log_toxic_message,
    log_name_change,
    log_manual_action,
)

logger = logging.getLogger(__name__)

pending_verifications: dict[tuple[int, int], int] = {}


# ============================================
# /chatid
# ============================================

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Chat ID: <code>{update.effective_chat.id}</code>",
        parse_mode="HTML",
    )


# ============================================
# КАПЧА
# ============================================

def _captcha_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ Я не бот",
            callback_data=f"captcha_{chat_id}_{user_id}",
        )],
    ])


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        user_id = member.id
        full_name = member.full_name
        username = member.username

        verification = get_verification(user_id, chat.id)
        is_returning = verification is not None
        first_registered = (
            verification["first_registered"] if verification else None
        )

        await log_new_user(
            context,
            chat.id, chat.title, chat.username,
            user_id, full_name, username,
            is_returning=is_returning,
            first_registered=first_registered,
        )

        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                ),
            )
        except Exception as e:
            logger.error("Restrict %d: %s", user_id, e)

        msg = await update.message.reply_text(
            f"👋 Привет, <b>{full_name}</b>!\n\n"
            f"Нажми кнопку ниже в течение {CAPTCHA_TIMEOUT} секунд, "
            f"чтобы подтвердить, что ты не бот.",
            parse_mode="HTML",
            reply_markup=_captcha_keyboard(chat.id, user_id),
        )

        pending_verifications[(chat.id, user_id)] = msg.message_id

        context.job_queue.run_once(
            _captcha_timeout_job,
            when=CAPTCHA_TIMEOUT,
            data={
                "chat_id": chat.id,
                "user_id": user_id,
                "full_name": full_name,
                "username": username,
                "message_id": msg.message_id,
                "chat_title": chat.title,
                "chat_username": chat.username,
            },
            name=f"captcha_{chat.id}_{user_id}",
        )

        upsert_user(user_id, member.first_name, member.last_name, username)


async def _captcha_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    user_id = data["user_id"]

    key = (chat_id, user_id)
    if key not in pending_verifications:
        return

    del pending_verifications[key]

    try:
        await context.bot.delete_message(chat_id, data["message_id"])
    except Exception:
        pass

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
    except Exception as e:
        logger.error("Kick %d: %s", user_id, e)

    await log_failed_verification(
        context,
        chat_id, data["chat_title"], data["chat_username"],
        user_id, data["full_name"], data["username"],
    )


async def on_captcha_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    data = query.data

    if not data.startswith("captcha_"):
        return

    parts = data.split("_")
    if len(parts) != 3:
        await query.answer("Ошибка")
        return

    chat_id = int(parts[1])
    target_user_id = int(parts[2])

    if query.from_user.id != target_user_id:
        await query.answer("Это не для тебя!", show_alert=True)
        return

    key = (chat_id, target_user_id)
    if key not in pending_verifications:
        await query.answer("Верификация уже завершена")
        return

    del pending_verifications[key]

    jobs = context.job_queue.get_jobs_by_name(
        f"captcha_{chat_id}_{target_user_id}"
    )
    for job in jobs:
        job.schedule_removal()

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
                can_invite_users=True,
                can_pin_messages=False,
                can_change_info=False,
            ),
        )
    except Exception as e:
        logger.error("Unrestrict: %s", e)

    set_verified(target_user_id, chat_id)

    await log_verified(
        context,
        chat_id,
        query.message.chat.title,
        query.message.chat.username,
        target_user_id,
        query.from_user.full_name,
        query.from_user.username,
    )

    await query.edit_message_text(
        f"✅ <b>{query.from_user.full_name}</b> прошёл проверку. "
        f"Добро пожаловать!",
        parse_mode="HTML",
    )
    await query.answer("Верификация пройдена! ✅")


# ============================================
# МОДЕРАЦИЯ СООБЩЕНИЙ
# ============================================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    chat = update.effective_chat

    if user.is_bot:
        return

    changes = upsert_user(
        user.id, user.first_name, user.last_name, user.username
    )
    if changes:
        await log_name_change(
            context,
            chat.id, chat.title, chat.username,
            user.id, changes,
        )

    text = update.message.text or update.message.caption
    if not text:
        return

    result = await analyze_text(text)
    if result is None:
        return

    violations = check_thresholds(result["scores"])

    if not violations and not result["flagged"]:
        return

    if not violations and result["flagged"]:
        max_cat = max(result["scores"], key=result["scores"].get)
        max_score = result["scores"][max_cat]
        violations = [(max_cat, max_score, 0.0)]

    action = AUTO_ACTION
    action_taken = action

    try:
        await update.message.delete()
    except Exception as e:
        logger.error("Delete msg: %s", e)

    if action == "mute_1h":
        try:
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await context.bot.restrict_chat_member(
                chat.id, user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except Exception as e:
            logger.error("Mute: %s", e)

    elif action == "ban":
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
        except Exception as e:
            logger.error("Ban: %s", e)

    add_warning(
        user.id, chat.id, "toxic_message", json.dumps(result["scores"])
    )
    warn_count = count_warnings(user.id, chat.id)

    if warn_count >= 3 and action == "delete":
        action_taken = "mute_1h"
        try:
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await context.bot.restrict_chat_member(
                chat.id, user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except Exception:
            pass

    await log_toxic_message(
        context,
        chat.id, chat.title, chat.username,
        user.id, user.full_name, user.username,
        text, result["scores"], violations, action_taken,
    )


# ============================================
# КНОПКИ МОДЕРАЦИИ
# ============================================

async def on_moderation_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    data = query.data

    if not data.startswith("mod_"):
        return

    parts = data.split("_")
    if len(parts) != 4:
        await query.answer("Ошибка")
        return

    _, chat_id_str, user_id_str, action = parts
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)

    admin = query.from_user
    admin_name = admin.full_name

    try:
        if action == "ban":
            await context.bot.ban_chat_member(chat_id, user_id)
            result = "🚫 Пользователь забанен"

        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
            result = "🗑 Пользователь удалён"

        elif action == "mute1h":
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            result = "🔇 Заглушён на 1 час"

        elif action == "muteforever":
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
            result = "🔇 Заглушён навсегда"

        elif action == "unban":
            await context.bot.unban_chat_member(chat_id, user_id)
            result = "✅ Разбанен"

        else:
            await query.answer("Неизвестное действие")
            return

        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer(result, show_alert=True)

        await log_manual_action(
            context, admin_name, action, chat_id, user_id
        )

    except Exception as e:
        logger.error("Mod action %s: %s", action, e)
        await query.answer(f"Ошибка: {e}", show_alert=True)


async def on_left_member(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    pass