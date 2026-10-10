"""
planner.py — отложенные действия из админ-панели:
  • рассылка в заданное время;
  • открытие набора в заданное время;
  • закрытие набора в заданное время.

Время вводится как «через сколько» (90m, 3h, 2d, 1d12h) или датой по МСК
(31.12.2026 23:59). Расписание хранится в той же bot.db, поэтому переживает
перезапуск (вместе с бэкапом из extras.py). Если время наступило, пока бот был
выключен, действие выполнится сразу после запуска.

Для отложенной рассылки исходное сообщение должно оставаться в чате с ботом
(бот копирует его в момент отправки) — не удаляй его до рассылки.
"""
import asyncio
import json
import logging
import time

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database as db
import extras
from config import ADMIN_IDS, DB_PATH
from promo import fmt_dt, fmt_left, parse_ttl

log = logging.getLogger("planner")

CHECK_INTERVAL_SEC = 10
LATE_NOTE_AFTER_SEC = 120

router = Router()
router.message.filter(F.from_user.id.in_(set(ADMIN_IDS)))
router.callback_query.filter(F.from_user.id.in_(set(ADMIN_IDS)))

_tasks: set[asyncio.Task] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                       -- broadcast | open | close
    run_at INTEGER NOT NULL,                  -- unix UTC
    payload TEXT,                             -- JSON
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | running | done | failed | cancelled
    created_by INTEGER,
    created_at INTEGER NOT NULL,
    result TEXT
);
"""

LABEL = {"broadcast": "📢 Рассылка", "open": "🟢 Открыть набор", "close": "🔴 Закрыть набор"}


# ───────────────────────── БД ─────────────────────────

async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()


def _task(r) -> dict | None:
    if r is None:
        return None
    return {"id": r[0], "kind": r[1], "run_at": r[2],
            "payload": json.loads(r[3]) if r[3] else {}, "status": r[4], "created_by": r[5]}


_SEL = "SELECT id, kind, run_at, payload, status, created_by FROM scheduled"


async def add_task(kind: str, run_at: int, payload: dict | None, created_by: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO scheduled (kind, run_at, payload, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, run_at, json.dumps(payload) if payload else None, created_by, int(time.time())),
        )
        await conn.commit()
        return cur.lastrowid


async def list_pending() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(f"{_SEL} WHERE status = 'pending' ORDER BY run_at")
        return [_task(r) for r in await cur.fetchall()]


async def get_task(task_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(f"{_SEL} WHERE id = ?", (task_id,))
        return _task(await cur.fetchone())


async def cancel_task(task_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE scheduled SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (task_id,),
        )
        await conn.commit()
        return cur.rowcount == 1


async def run_now(task_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE scheduled SET run_at = ? WHERE id = ? AND status = 'pending'",
            (int(time.time()), task_id),
        )
        await conn.commit()
        return cur.rowcount == 1


async def claim_due(now: int | None = None) -> list[dict]:
    """Забирает наступившие задачи; каждая достаётся ровно одному исполнителю."""
    now = int(time.time()) if now is None else now
    claimed = []
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            f"{_SEL} WHERE status = 'pending' AND run_at <= ? ORDER BY run_at", (now,)
        )
        for r in await cur.fetchall():
            upd = await conn.execute(
                "UPDATE scheduled SET status = 'running' WHERE id = ? AND status = 'pending'",
                (r[0],),
            )
            if upd.rowcount == 1:
                claimed.append(_task(r))
        await conn.commit()
    return claimed


async def finish(task_id: int, status: str, result: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE scheduled SET status = ?, result = ? WHERE id = ?", (status, result, task_id)
        )
        await conn.commit()


# ───────────────────────── исполнение ─────────────────────────

async def _notify(bot: Bot, admin_ids: list[int], text: str) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("notify %s failed", admin_id)


async def execute(bot: Bot, t: dict, now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    late = now - t["run_at"]
    late_note = f" (с опозданием на {fmt_left(late)})" if late > LATE_NOTE_AFTER_SEC else ""
    creator = t["created_by"]
    admins = list(dict.fromkeys([creator, *ADMIN_IDS])) if creator else list(ADMIN_IDS)
    try:
        if t["kind"] in ("open", "close"):
            is_open = t["kind"] == "open"
            await db.set_enrollment_open(is_open)
            word = "🟢 Набор открыт" if is_open else "🔴 Набор закрыт"
            await _notify(bot, admins, f"⏰ {word} по расписанию{late_note}.")
            await finish(t["id"], "done")
        elif t["kind"] == "broadcast":
            p = t["payload"]
            result = await extras.run_broadcast(bot, creator, p["chat_id"], p["message_id"])
            if result["ok"] == 0:
                await _notify(
                    bot, [creator],
                    "⚠️ Отложенная рассылка никому не доставлена. Возможно, исходное сообщение "
                    "удалено из чата с ботом. Не удаляй его до отправки.",
                )
            elif late_note:
                await _notify(bot, [creator], f"⏰ Рассылка #{t['id']} ушла{late_note}.")
            await finish(t["id"], "done", json.dumps(result))
        else:
            await finish(t["id"], "failed", "unknown kind")
    except Exception as e:
        log.exception("task %s failed", t["id"])
        await finish(t["id"], "failed", str(e)[:200])
        await _notify(bot, admins, f"❌ Отложенное действие #{t['id']} ({LABEL.get(t['kind'], t['kind'])}) "
                                   f"не выполнено: {e}")


async def _loop(bot: Bot) -> None:
    while True:
        try:
            for t in await claim_due():
                await execute(bot, t)
        except Exception:
            log.exception("planner loop error")
        await asyncio.sleep(CHECK_INTERVAL_SEC)


# ───────────────────────── интерфейс ─────────────────────────

class Sched(StatesGroup):
    content = State()
    when = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_or_answer(message: Message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=markup)


async def _menu_view() -> tuple[str, InlineKeyboardMarkup]:
    tasks = await list_pending()
    rows = [[_btn(f"#{t['id']} {LABEL[t['kind']]} · {fmt_dt(t['run_at'])}", f"psch:view:{t['id']}")]
            for t in tasks]
    rows += [
        [_btn("📢 Рассылка по таймеру", "psch:new:broadcast")],
        [_btn("🟢 Открыть набор по таймеру", "psch:new:open")],
        [_btn("🔴 Закрыть набор по таймеру", "psch:new:close")],
    ]
    text = "⏰ Отложенные действия\n\n" + (
        f"Запланировано: {len(tasks)}" if tasks else "Пока ничего не запланировано."
    )
    return text, _kb(rows)


async def _task_card(task_id: int):
    t = await get_task(task_id)
    if not t or t["status"] != "pending":
        return None
    left = max(0, t["run_at"] - int(time.time()))
    text = (f"#{t['id']} {LABEL[t['kind']]}\n\n"
            f"Когда: {fmt_dt(t['run_at'])}\n"
            f"Осталось: {fmt_left(left) if left else 'выполняется сейчас'}")
    if t["kind"] == "broadcast":
        text += "\nПолучателей сейчас: " + str(len(await extras.broadcast_ids()))
    return text, _kb([
        [_btn("▶️ Выполнить сейчас", f"psch:now:{t['id']}"), _btn("❌ Отменить", f"psch:del:{t['id']}")],
        [_btn("◀️ К списку", "psch:menu")],
    ])


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _menu_view()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.in_({"admin_sched", "psch:menu"}))
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _menu_view()
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer()


@router.callback_query(F.data == "psch:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit_or_answer(callback.message, "Отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("psch:view:"))
async def cb_view(callback: CallbackQuery) -> None:
    card = await _task_card(int(callback.data.split(":")[2]))
    if not card:
        await callback.answer("Уже выполнено или отменено", show_alert=True)
        return
    await _edit_or_answer(callback.message, *card)
    await callback.answer()


@router.callback_query(F.data.startswith("psch:del:"))
async def cb_del(callback: CallbackQuery) -> None:
    ok = await cancel_task(int(callback.data.split(":")[2]))
    text, markup = await _menu_view()
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer("Отменено" if ok else "Уже выполнено")


@router.callback_query(F.data.startswith("psch:now:"))
async def cb_now(callback: CallbackQuery) -> None:
    ok = await run_now(int(callback.data.split(":")[2]))
    await callback.answer("Запускаю" if ok else "Уже выполнено", show_alert=not ok)
    text, markup = await _menu_view()
    await _edit_or_answer(callback.message, text, markup)


_CANCEL_ROW = [_btn("❌ Отмена", "psch:cancel")]

_WHEN_TEXT = (
    "Когда выполнить?\n"
    "Нажми кнопку или пришли: через сколько — 90m, 3h, 2d, 1d12h — "
    "либо дату по МСК: 31.12.2026 23:59"
)


def _when_kb() -> InlineKeyboardMarkup:
    return _kb([
        [_btn("Через 1 ч", "psch:in:3600"), _btn("Через 3 ч", "psch:in:10800")],
        [_btn("Через 12 ч", "psch:in:43200"), _btn("Через 24 ч", "psch:in:86400")],
        _CANCEL_ROW,
    ])


@router.callback_query(F.data.startswith("psch:new:"))
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[2]
    await state.clear()
    await state.update_data(kind=kind)
    if kind == "broadcast":
        await state.set_state(Sched.content)
        await callback.message.answer(
            "Пришли сообщение для рассылки (текст, фото, видео — любое). "
            "Не удаляй его из чата до момента отправки.",
            reply_markup=_kb([_CANCEL_ROW]),
        )
    else:
        await state.set_state(Sched.when)
        word = "Открыть" if kind == "open" else "Закрыть"
        await callback.message.answer(f"{word} набор.\n\n{_WHEN_TEXT}", reply_markup=_when_kb())
    await callback.answer()


@router.message(Sched.content)
async def got_content(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler()
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Sched.when)
    await message.answer("Так это увидят получатели:")
    await message.bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    await message.answer(_WHEN_TEXT, reply_markup=_when_kb())


async def _create(message: Message, state: FSMContext, run_at: int, admin_id: int) -> None:
    d = await state.get_data()
    await state.clear()
    payload = None
    if d["kind"] == "broadcast":
        payload = {"chat_id": d["chat_id"], "message_id": d["message_id"]}
    task_id = await add_task(d["kind"], run_at, payload, admin_id)
    extra = ""
    if d["kind"] == "broadcast":
        extra = f"\nПолучателей сейчас: {len(await extras.broadcast_ids())}"
    await message.answer(
        f"✅ Запланировано #{task_id}: {LABEL[d['kind']]}\n"
        f"Когда: {fmt_dt(run_at)} (через {fmt_left(max(0, run_at - int(time.time())))}){extra}",
        reply_markup=_kb([[_btn("❌ Отменить", f"psch:del:{task_id}"), _btn("⏰ К списку", "psch:menu")]]),
    )


@router.callback_query(F.data.startswith("psch:in:"), Sched.when)
async def when_button(callback: CallbackQuery, state: FSMContext) -> None:
    sec = int(callback.data.split(":")[2])
    await callback.answer()
    await _create(callback.message, state, int(time.time()) + sec, callback.from_user.id)


@router.message(Sched.when)
async def when_text(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler()
    now = int(time.time())
    parsed = parse_ttl(message.text or "", now)
    if not parsed:
        await message.answer("Не понял. Примеры: 90m, 3h, 2d, 1d12h или 31.12.2026 23:59 (дата в будущем).")
        return
    dur, abs_ts = parsed
    await _create(message, state, now + dur if dur else abs_ts, message.from_user.id)


# ───────────────────────── подключение ─────────────────────────

async def setup(dp: Dispatcher, bot: Bot) -> None:
    """Вызвать в bot.py после extras.setup и promo.setup."""
    await init()
    dp.include_router(router)
    task = asyncio.create_task(_loop(bot))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.info("planner ready")
