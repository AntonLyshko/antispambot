import time
import logging

logger = logging.getLogger(__name__)

# Настройки
MAX_MESSAGES = 5       # максимум сообщений
TIME_WINDOW = 10       # за сколько секунд
MUTE_DURATION = 300    # мут на 5 минут (секунды)

# {(chat_id, user_id): [timestamp, timestamp, ...]}
message_history: dict[tuple[int, int], list[float]] = {}


def check_flood(chat_id: int, user_id: int) -> bool:
    """
    Возвращает True если пользователь флудит.
    """
    key = (chat_id, user_id)
    now = time.time()

    if key not in message_history:
        message_history[key] = []

    # Убираем старые записи
    message_history[key] = [
        t for t in message_history[key]
        if now - t < TIME_WINDOW
    ]

    # Добавляем текущее
    message_history[key].append(now)

    count = len(message_history[key])

    if count > MAX_MESSAGES:
        logger.info(
            "🚨 Флуд: user %d в чате %d — %d сообщений за %d сек",
            user_id, chat_id, count, TIME_WINDOW,
        )
        # Сбрасываем чтобы не срабатывал каждое сообщение
        message_history[key] = []
        return True

    return False


def reset_user(chat_id: int, user_id: int):
    key = (chat_id, user_id)
    message_history.pop(key, None)