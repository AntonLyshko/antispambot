import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
DB_PATH = "bot_data.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS name_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            old_first_name TEXT,
            old_last_name TEXT,
            old_username TEXT,
            new_first_name TEXT,
            new_last_name TEXT,
            new_username TEXT,
            changed_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            reason TEXT,
            scores TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS verifications (
            user_id INTEGER,
            chat_id INTEGER,
            verified INTEGER DEFAULT 0,
            first_registered TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    """)

    conn.commit()
    conn.close()
    logger.info("БД инициализирована")


def upsert_user(
    user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()

    existing = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO users "
            "(user_id, first_name, last_name, username, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, first_name, last_name or "", username or "", now, now),
        )
        conn.commit()
        conn.close()
        return None

    existing = dict(existing)
    changes = {}

    if existing["first_name"] != first_name:
        changes["first_name"] = (existing["first_name"], first_name)
    if (existing["last_name"] or "") != (last_name or ""):
        changes["last_name"] = (existing["last_name"], last_name or "")
    if (existing["username"] or "") != (username or ""):
        changes["username"] = (existing["username"], username or "")

    conn.execute(
        "UPDATE users SET first_name=?, last_name=?, username=?, last_seen=? "
        "WHERE user_id=?",
        (first_name, last_name or "", username or "", now, user_id),
    )

    if changes:
        conn.execute(
            "INSERT INTO name_history "
            "(user_id, old_first_name, old_last_name, old_username, "
            "new_first_name, new_last_name, new_username, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                existing["first_name"],
                existing["last_name"],
                existing["username"],
                first_name,
                last_name or "",
                username or "",
                now,
            ),
        )

    conn.commit()
    conn.close()
    return changes if changes else None


def set_verified(user_id: int, chat_id: int):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO verifications "
        "(user_id, chat_id, verified, first_registered) "
        "VALUES (?, ?, 1, COALESCE("
        "  (SELECT first_registered FROM verifications "
        "   WHERE user_id=? AND chat_id=?), ?"
        "))",
        (user_id, chat_id, user_id, chat_id, now),
    )
    conn.commit()
    conn.close()


def get_verification(user_id: int, chat_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM verifications WHERE user_id=? AND chat_id=?",
        (user_id, chat_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def add_warning(
    user_id: int, chat_id: int, reason: str, scores: str = ""
):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO warnings "
        "(user_id, chat_id, reason, scores, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, reason, scores, now),
    )
    conn.commit()
    conn.close()


def count_warnings(user_id: int, chat_id: int) -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM warnings "
        "WHERE user_id=? AND chat_id=?",
        (user_id, chat_id),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


init_db()