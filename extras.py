"""
extras.py — починка статистики и рассылки.

Что решает:
1. БД (bot.db) на BotHost стирается при рестарте -> пользователи пропадают.
   Теперь копия БД лежит закреплённым файлом в личке с админом и сама
   восстанавливается при старте, если локальная БД пустая.
2. Пользователи учитываются на ЛЮБОЕ действие (сообщение/кнопка), а не только /start.
3. /stats — новые и активные за сутки/неделю/месяц/год.
4. Рассылка (/broadcast или кнопка admin_broadcast): копия сообщения 1-в-1,
   без пометок, с предпросмотром, обходом лимитов Telegram и отчётом.

Подключение: две строки в bot.py (см. README_FIX.md).
"""
import asyncio
import logging
import os
import sqlite3
import tempfile

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Message,
)

from config import ADMIN_IDS, DB_PATH

log = logging.getLogger("extras")

BACKUP_NAME = "bot_backup.db"
BACKUP_INTERVAL_SEC = 120
SEND_DELAY_SEC = 0.05  # ~20 сообщений/сек, лимит Telegram ~30

router = Router()
router.message.filter(F.from_user.id.in_(set(ADMIN_IDS)))
router.callback_query.filter(F.from_user.id.in_(set(ADMIN_IDS)))

_tasks: set[asyncio.Task] = set()
_backup_msg_id: int | None = None
_last_sig: tuple | None = None


# ───────────────────────── пользователи ─────────────────────────

async def ensure_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "user_id INTEGER PRIMARY KEY, "
            "first_seen TEXT DEFAULT (datetime('now')), "
            "last_seen TEXT DEFAULT (datetime('now')))"
        )
        cur = await db.execute("PRAGMA table_info(users)")
        cols = {r[1] for r in await cur.fetchall()}
        if "blocked" not in cols:
            await db.execute(
                "ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
            )
        # все, кто хоть раз платил, обязаны быть в рассылке
        try:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, first_seen, last_seen) "
                "SELECT user_id, MIN(created_at), MAX(created_at) "
                "FROM payments GROUP BY user_id"
            )
        except sqlite3.OperationalError:
            pass  # таблицы payments ещё нет
        await db.commit()


async def touch_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id) VALUES (?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "last_seen = datetime('now'), blocked = 0",
            (user_id,),
        )
        await db.commit()


class SeenMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not user.is_bot:
            try:
                await touch_user(user.id)
            except Exception:
                log.exception("touch_user failed")
        return await handler(event, data)


async def mark_blocked(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def broadcast_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE blocked = 0")
        return [r[0] for r in await cur.fetchall()]


PERIODS = (("За сутки", 1), ("За неделю", 7), ("За месяц", 30), ("За год", 365))


async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async def one(sql: str, *args) -> int:
            cur = await db.execute(sql, args)
            return (await cur.fetchone())[0]

        total = await one("SELECT COUNT(*) FROM users")
        blocked = await one("SELECT COUNT(*) FROM users WHERE blocked = 1")
        rows = []
        for label, days in PERIODS:
            mod = f"-{days} days"
            new = await one(
                "SELECT COUNT(*) FROM users WHERE first_seen >= datetime('now', ?)", mod
            )
            active = await one(
                "SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now', ?)", mod
            )
            rows.append((label, new, active))
    return total, blocked, rows


def format_stats(total, blocked, rows) -> str:
    lines = [
        "📊 Статистика",
        "",
        f"Всего за всё время: {total}",
        f"Из них заблокировали бота: {blocked}",
        "",
        "Период — новых / заходили:",
    ]
    for label, new, active in rows:
        lines.append(f"{label}: {new} / {active}")
    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await message.answer(format_stats(*await get_stats()))


@router.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery) -> None:
    await callback.message.answer(format_stats(*await get_stats()))
    await callback.answer()


# ───────────────────────── рассылка ─────────────────────────

class Bc(StatesGroup):
    waiting = State()
    confirm = State()


def _confirm_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"✅ Отправить ({n})", callback_data="bc_go"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel"),
            ]
        ]
    )


async def _start_broadcast(target: Message, state: FSMContext) -> None:
    await state.set_state(Bc.waiting)
    await target.answer(
        "Пришли сообщение для рассылки (текст, фото, видео — любое).\n"
        "Оно уйдёт всем один в один, без пометок.\n"
        "Отмена — /cancel"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await _start_broadcast(message, state)


@router.callback_query(F.data == "admin_broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_broadcast(callback.message, state)
    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


def _not_command(m: Message) -> bool:
    return not (m.text or "").startswith("/")


@router.message(Bc.waiting, _not_command)
async def got_broadcast_message(message: Message, state: FSMContext) -> None:
    ids = await broadcast_ids()
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Bc.confirm)
    await message.answer("Так это увидят получатели:")
    await message.bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    await message.answer(
        f"Отправить {len(ids)} пользователям?", reply_markup=_confirm_kb(len(ids))
    )


@router.callback_query(F.data == "bc_cancel")
async def bc_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "bc_go", Bc.confirm)
async def bc_go(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text("Рассылка запущена, пришлю отчёт.")
    await callback.answer()
    task = asyncio.create_task(
        run_broadcast(
            callback.bot, callback.from_user.id, data["chat_id"], data["message_id"]
        )
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _copy_one(bot: Bot, uid: int, from_chat: int, msg_id: int) -> str:
    """-> 'ok' | 'blocked' | 'failed'"""
    for _ in range(3):
        try:
            await bot.copy_message(uid, from_chat, msg_id)
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            await mark_blocked(uid)
            return "blocked"
        except TelegramBadRequest as e:
            text = str(e).lower()
            if "chat not found" in text or "deactivated" in text:
                await mark_blocked(uid)
                return "blocked"
            log.warning("broadcast to %s failed: %s", uid, e)
            return "failed"
        except Exception:
            log.exception("broadcast to %s failed", uid)
            return "failed"
    return "failed"


async def run_broadcast(
    bot: Bot, admin_id: int, from_chat: int, msg_id: int, exclude: set[int] | None = None
) -> dict:
    ids = [i for i in await broadcast_ids() if not exclude or i not in exclude]
    result = {"ok": 0, "blocked": 0, "failed": 0}
    for uid in ids:
        result[await _copy_one(bot, uid, from_chat, msg_id)] += 1
        await asyncio.sleep(SEND_DELAY_SEC)
    await bot.send_message(
        admin_id,
        "✅ Рассылка завершена\n\n"
        f"Доставлено: {result['ok']}\n"
        f"Заблокировали бота: {result['blocked']}\n"
        f"Ошибок: {result['failed']}\n"
        f"Всего в базе: {len(ids)}",
    )
    return result


# ───────────────── бэкап БД в Telegram (переживает рестарты) ─────────────────

def _has_data() -> bool:
    if not os.path.exists(DB_PATH):
        return False
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            for table in ("users", "payments"):
                try:
                    if con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0:
                        return True
                except sqlite3.OperationalError:
                    continue
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return False


def _sig() -> tuple:
    st = os.stat(DB_PATH)
    return (st.st_mtime_ns, st.st_size)


def _snapshot(dst: str) -> None:
    src = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dst)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()


async def _pinned_backup(bot: Bot):
    """Закреплённое сообщение-бэкап в личке с первым админом (или None)."""
    if not ADMIN_IDS:
        return None
    try:
        chat = await bot.get_chat(ADMIN_IDS[0])
    except Exception:
        log.exception("get_chat(admin) failed")
        return None
    pm = chat.pinned_message
    if pm and pm.document and pm.document.file_name == BACKUP_NAME:
        return pm
    return None


async def restore_if_empty(bot: Bot) -> bool:
    global _backup_msg_id, _last_sig
    pm = await _pinned_backup(bot)
    if pm:
        _backup_msg_id = pm.message_id
    if _has_data() or not pm:
        return False
    tmp = DB_PATH + ".restore"
    try:
        await bot.download(pm.document, destination=tmp)
        con = sqlite3.connect(tmp)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
        if not ok:
            log.error("backup failed integrity check")
            return False
        os.replace(tmp, DB_PATH)
        _last_sig = _sig()
        log.info("DB restored from Telegram backup")
        return True
    except Exception:
        log.exception("restore failed")
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def save_backup(bot: Bot, force: bool = False) -> bool:
    global _backup_msg_id, _last_sig
    if not ADMIN_IDS or not _has_data():  # пустую БД поверх хорошей копии не пишем
        return False
    sig = _sig()
    if not force and sig == _last_sig:
        return False
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await asyncio.to_thread(_snapshot, tmp)
        admin = ADMIN_IDS[0]
        if _backup_msg_id:
            try:
                await bot.edit_message_media(
                    chat_id=admin,
                    message_id=_backup_msg_id,
                    media=InputMediaDocument(
                        media=FSInputFile(tmp, filename=BACKUP_NAME),
                        caption="🗄 Резервная копия базы. Не удаляй и не откреплять.",
                    ),
                )
                _last_sig = sig
                return True
            except TelegramBadRequest as e:
                if "not modified" in str(e).lower():
                    _last_sig = sig
                    return True
                log.warning("edit backup failed, sending new: %s", e)
        msg = await bot.send_document(
            admin,
            FSInputFile(tmp, filename=BACKUP_NAME),
            caption="🗄 Резервная копия базы. Не удаляй и не откреплять.",
            disable_notification=True,
        )
        await bot.pin_chat_message(admin, msg.message_id, disable_notification=True)
        _backup_msg_id = msg.message_id
        _last_sig = sig
        return True
    finally:
        os.remove(tmp)


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    ok = await save_backup(message.bot, force=True)
    await message.answer("🗄 Копия базы обновлена." if ok else "Нечего сохранять: база пуста.")


async def _backup_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SEC)
        try:
            await save_backup(bot)
        except Exception:
            log.exception("backup loop error")


# ───────────────────────── подключение ─────────────────────────

async def setup(dp: Dispatcher, bot: Bot) -> None:
    """Вызвать в bot.py ДО dp.include_router(admin.router)."""
    restored = await restore_if_empty(bot)
    await ensure_schema()
    dp.message.outer_middleware(SeenMiddleware())
    dp.callback_query.outer_middleware(SeenMiddleware())
    dp.include_router(router)
    task = asyncio.create_task(_backup_loop(bot))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    try:
        await save_backup(bot)
    except Exception:
        log.exception("initial backup failed")
    log.info("extras ready (restored=%s)", restored)
