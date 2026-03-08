import aiohttp
import logging

logger = logging.getLogger(__name__)


async def check_cas(user_id: int) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.cas.chat/check?user_id={user_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                is_banned = data.get("ok", False)
                if is_banned:
                    logger.info("🚫 CAS: user %d — спамер", user_id)
                return is_banned
    except Exception as e:
        logger.error("CAS ошибка: %s", e)
        return False