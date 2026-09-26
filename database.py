import aiosqlite
from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'yookassa', -- yookassa / yookassa_sbp / cryptobot / xrocket
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invites (
    invite_link TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued', -- issued / used / revoked
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # набор закрыт по умолчанию, пока админ явно не откроет
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('enrollment_open', '0')"
        )
        await db.commit()


async def get_setting(key: str, default: str | None = None) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def is_enrollment_open() -> bool:
    value = await get_setting("enrollment_open", "0")
    return value == "1"


async def set_enrollment_open(is_open: bool) -> None:
    await set_setting("enrollment_open", "1" if is_open else "0")


# --- фото для приветственного и "набор закрыт" сообщений (file_id) ---

async def get_open_photo() -> str | None:
    from config import DEFAULT_OPEN_PHOTO_ID
    return await get_setting("open_photo_id", DEFAULT_OPEN_PHOTO_ID)


async def set_open_photo(file_id: str) -> None:
    await set_setting("open_photo_id", file_id)


async def get_closed_photo() -> str | None:
    from config import DEFAULT_CLOSED_PHOTO_ID
    return await get_setting("closed_photo_id", DEFAULT_CLOSED_PHOTO_ID)


async def set_closed_photo(file_id: str) -> None:
    await set_setting("closed_photo_id", file_id)


# --- платежи ---

async def create_payment_record(payment_id: str, user_id: int, amount: float, provider: str = "yookassa") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (payment_id, provider, user_id, amount, status) VALUES (?, ?, ?, ?, 'pending')",
            (payment_id, provider, user_id, amount),
        )
        await db.commit()


async def update_payment_status(payment_id: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id)
        )
        await db.commit()


async def get_payment(payment_id: str) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,))
        return await cur.fetchone()


# --- инвайты в приватку ---

async def create_invite_record(invite_link: str, payment_id: str, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO invites (invite_link, payment_id, user_id, status) VALUES (?, ?, ?, 'issued')",
            (invite_link, payment_id, user_id),
        )
        await db.commit()


async def get_invite(invite_link: str) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM invites WHERE invite_link = ?", (invite_link,))
        return await cur.fetchone()


async def set_invite_status(invite_link: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE invites SET status = ? WHERE invite_link = ?", (status, invite_link)
        )
        await db.commit()


async def has_granted_invite(payment_id: str) -> bool:
    """Уже выпускали инвайт по этой оплате? (чтобы не выдать доступ дважды)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM invites WHERE payment_id = ?", (payment_id,)
        )
        return await cur.fetchone() is not None


# --- статистика пользователей ---

async def record_user_seen(user_id: int) -> None:
    """Отмечает визит пользователя: заводит запись при первом визите, иначе обновляет last_seen."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, first_seen, last_seen) "
            "VALUES (?, datetime('now'), datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = datetime('now')",
            (user_id,),
        )
        await db.commit()


async def get_user_stats() -> dict:
    """
    Возвращает total (уникальных пользователей за всё время) и число тех,
    кто заходил в бота за последний год/месяц/неделю/день (по last_seen).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]

        async def _since(days: int) -> int:
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now', ?)",
                (f"-{days} days",),
            )
            return (await cur.fetchone())[0]

        return {
            "total": total,
            "year": await _since(365),
            "month": await _since(30),
            "week": await _since(7),
            "day": await _since(1),
        }
