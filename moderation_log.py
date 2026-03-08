import logging
from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import LOG_GROUP_ID

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

# Читаемые названия категорий OpenAI
CATEGORY_NAMES = {
    "harassment": "🟠 Оскорбления/травля",
    "harassment/threatening": "🔴 Угрозы расправы",
    "hate": "🔴 Разжигание ненависти",
    "hate/threatening": "🔴 Ненависть + угрозы",
    "illicit": "🟡 Незаконная деятельность",
    "illicit/violent": "🔴 Незаконное + насилие",
    "self-harm": "🟣 Самоповреждение",
    "self-harm/intent": "🟣 Намерение самоповреждения",
    "self-harm/instructions": "🟣 Инструкции самоповреждения",
    "sexual": "🟡 Сексуальный контент",
    "sexual/minors": "⛔ Сексуальный + дети",
    "violence": "🔴 Насилие",
    "violence/graphic": "🔴 Графическое насилие",
}


def _now_msk() -> str:
    return datetime.now(MSK).strftime("%d %B %Y г. %H:%M:%S MSK")


def _user_link(user_id: int, full_name: str, username: str | None) -> str:
    parts = [f"<b>{full_name}</b>"]
    if username:
        parts.append(f" [<code>@{username}</code>]")
    parts.append(f"[<code>{user_id}</code>]")
    parts.append(f"\n  #user{user_id}")
    return "".join(parts)


def _chat_info(
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
) -> str:
    title = chat_title or "Чат"
    parts = [f"<b>Группа:</b> {title}"]
    if chat_username:
        parts.append(f" [<code>@{chat_username}</code>]")
    parts.append(f"\n  [<code>{chat_id}</code>]")
    return "".join(parts)


def _action_buttons(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    prefix = f"mod_{chat_id}_{user_id}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Забанить", callback_data=f"{prefix}_ban"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"{prefix}_kick"),
        ],
        [
            InlineKeyboardButton(
                "🔇 Мут 1 час", callback_data=f"{prefix}_mute1h"
            ),
            InlineKeyboardButton(
                "🔇 Мут навсегда", callback_data=f"{prefix}_muteforever"
            ),
        ],
    ])


def _unban_button(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    prefix = f"mod_{chat_id}_{user_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ Разбанить", callback_data=f"{prefix}_unban"
        )],
    ])


# ================================================================
# ЛОГИ
# ================================================================


async def log_failed_verification(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    user_id: int,
    full_name: str,
    username: str | None,
):
    if not LOG_GROUP_ID:
        return

    text = (
        f"<b>Модерация чата</b>\n"
        f"⚠️ #НеПрошелПроверку Пользователь не прошел проверку и "
        f"удален из чата (Приветствие)\n\n"
        f"{_chat_info(chat_id, chat_title, chat_username)}\n"
        f"  <b>Пользователь:</b> {_user_link(user_id, full_name, username)}\n"
        f"  <b>Действие:</b> Удаление из группы #kicked\n"
        f"  🕐 Время: {_now_msk()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=_unban_button(chat_id, user_id),
        )
    except Exception as e:
        logger.error("Лог failed_verification: %s", e)


async def log_new_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    user_id: int,
    full_name: str,
    username: str | None,
    is_returning: bool = False,
    first_registered: str | None = None,
):
    if not LOG_GROUP_ID:
        return

    if is_returning:
        tag = "🆔 #НовыйПользователь #Вернулся"
        desc = "Пользователь вернулся в чат"
        if first_registered:
            desc += f", первая регистрация {first_registered}"
    else:
        tag = "🆔 #НовыйПользователь"
        desc = "Новый пользователь зашёл в чат"

    text = (
        f"<b>Модерация чата</b>\n"
        f"{tag}\n\n"
        f"{desc}\n\n"
        f"{_chat_info(chat_id, chat_title, chat_username)}\n"
        f"  <b>Пользователь:</b> {_user_link(user_id, full_name, username)}\n"
        f"  🕐 Время: {_now_msk()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=_action_buttons(chat_id, user_id),
        )
    except Exception as e:
        logger.error("Лог new_user: %s", e)


async def log_verified(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    user_id: int,
    full_name: str,
    username: str | None,
):
    if not LOG_GROUP_ID:
        return

    text = (
        f"<b>Модерация чата</b>\n"
        f"✅ #ПрошелПроверку Пользователь прошёл проверку\n\n"
        f"{_chat_info(chat_id, chat_title, chat_username)}\n"
        f"  <b>Пользователь:</b> {_user_link(user_id, full_name, username)}\n"
        f"  🕐 Время: {_now_msk()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Лог verified: %s", e)


async def log_toxic_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    user_id: int,
    full_name: str,
    username: str | None,
    message_text: str,
    scores: dict[str, float],
    violations: list[tuple[str, float, float]],
    action_taken: str,
):
    if not LOG_GROUP_ID:
        return

    # Показываем только категории с score > 0.01
    scores_text = "\n".join(
        f"    {CATEGORY_NAMES.get(cat, cat)}: <b>{score:.1%}</b>"
        for cat, score in sorted(scores.items(), key=lambda x: -x[1])
        if score > 0.01
    )

    violations_text = "\n".join(
        f"    ⛔ {CATEGORY_NAMES.get(cat, cat)}: {score:.1%} (порог: {thr:.0%})"
        for cat, score, thr in violations
    )

    short_msg = message_text[:200] + ("…" if len(message_text) > 200 else "")

    action_map = {
        "delete": "🗑 Сообщение удалено",
        "mute_1h": "🔇 Мут 1 час + удаление",
        "ban": "🚫 Бан + удаление",
        "warn": "⚠️ Предупреждение",
    }

    text = (
        f"<b>Модерация чата</b>\n"
        f"🤬 #ТоксичноеСообщение Обнаружено нарушение\n\n"
        f"{_chat_info(chat_id, chat_title, chat_username)}\n"
        f"  <b>Пользователь:</b> {_user_link(user_id, full_name, username)}\n\n"
        f"  💬 <b>Сообщение:</b>\n"
        f"  <i>{short_msg}</i>\n\n"
        f"  📊 <b>Оценки OpenAI Moderation:</b>\n{scores_text}\n\n"
        f"  🚨 <b>Превышения:</b>\n{violations_text}\n\n"
        f"  <b>Действие:</b> {action_map.get(action_taken, action_taken)}\n"
        f"  🕐 Время: {_now_msk()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=_action_buttons(chat_id, user_id),
        )
    except Exception as e:
        logger.error("Лог toxic_message: %s", e)


async def log_name_change(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    user_id: int,
    changes: dict,
):
    if not LOG_GROUP_ID:
        return

    field_names = {
        "first_name": "Имя",
        "last_name": "Фамилия",
        "username": "Username",
    }

    lines = []
    for field, (old_val, new_val) in changes.items():
        name = field_names.get(field, field)
        old_d = old_val if old_val else "<i>пусто</i>"
        new_d = new_val if new_val else "<i>пусто</i>"
        lines.append(f"    {name}: {old_d} → <b>{new_d}</b>")

    text = (
        f"<b>Модерация чата</b>\n"
        f"✏️ #ИзменениеПрофиля Пользователь изменил профиль\n\n"
        f"{_chat_info(chat_id, chat_title, chat_username)}\n"
        f"  #user{user_id}\n\n"
        f"  <b>Изменения:</b>\n" + "\n".join(lines) + "\n\n"
        f"  🕐 Время: {_now_msk()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Лог name_change: %s", e)


async def log_manual_action(
    context: ContextTypes.DEFAULT_TYPE,
    admin_name: str,
    action: str,
    chat_id: int,
    user_id: int,
):
    if not LOG_GROUP_ID:
        return

    action_names = {
        "ban": "🚫 Забанен",
        "kick": "🗑 Удалён из группы",
        "mute1h": "🔇 Заглушён на 1 час",
        "muteforever": "🔇 Заглушён навсегда",
        "unban": "✅ Разбанен",
    }

    text = (
        f"<b>Модерация чата</b>\n"
        f"👮 #ДействиеАдмина\n\n"
        f"  <b>Админ:</b> {admin_name}\n"
        f"  <b>Действие:</b> {action_names.get(action, action)}\n"
        f"  <b>Чат:</b> <code>{chat_id}</code>\n"
        f"  <b>Пользователь:</b> #user{user_id}\n"
        f"  🕐 Время: {_now_msk()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Лог manual_action: %s", e)