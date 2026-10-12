"""
automation.py — автоматика для админа:

  🎯 Лимит мест: когда число оплат с момента старта счётчика достигает лимита,
     набор закрывается сам, админу приходит уведомление.
  📣 Анонс при открытии: при открытии набора (кнопкой или по таймеру) выбранное
     сообщение уходит всем, кто ещё не оплатил.
  📊 Ежедневный отчёт: в заданное время (МСК) админам приходит сводка за день.
  📝 Тексты /start: тексты «набор закрыт/открыт» меняются из бота; подстановки
     {next_dates}, {next_open}, {next_close} берут даты из запланированных
     открытия/закрытия набора (см. planner).

Подключение: две строки в bot.py (см. сообщение), кнопка «🤖 Автоматика»
в админ-меню уже есть в keyboards.py. Команды: /auto, /report.

Как это встроено без правок access.py/admin.py:
  • успешные оплаты ловятся через promo.PAID_HOOKS (их вызывает access.grant_access);
  • для анонса database.set_enrollment_open заменяется обёрткой, которая
    дополнительно запускает анонс, когда набор переходит из закрытого в открытый.
"""
import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone

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
import planner
import promo
from config import ADMIN_IDS, DB_PATH
from payment_dispatch import PAID_STATUS

log = logging.getLogger("automation")

MSK = timezone(timedelta(hours=3))
REPORT_LATE_LIMIT = timedelta(hours=6)   # если проспали время отчёта дольше — ждём завтра
DEFAULT_REPORT_TIME = "21:00"

PROVIDER_NAMES = {"yookassa": "Карта", "yookassa_sbp": "СБП", "cryptobot": "CryptoBot", "xrocket": "xRocket"}

router = Router()
router.message.filter(F.from_user.id.in_(set(ADMIN_IDS)))
router.callback_query.filter(F.from_user.id.in_(set(ADMIN_IDS)))

_tasks: set[asyncio.Task] = set()
_bot: Bot | None = None
_orig_set_open = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    payment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    provider TEXT,
    ts INTEGER NOT NULL            -- момент успешной оплаты, unix UTC
);
"""


# ───────────────────────── продажи ─────────────────────────

async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(_SCHEMA)
        # история: уже оплаченные платежи подтягиваем из таблицы payments
        if PAID_STATUS:
            cond = " OR ".join("(provider = ? AND status = ?)" for _ in PAID_STATUS)
            args = [x for p, st in PAID_STATUS.items() for x in (p, st)]
            await conn.execute(
                "INSERT OR IGNORE INTO sales (payment_id, user_id, amount, provider, ts) "
                "SELECT payment_id, user_id, amount, provider, "
                "CAST(strftime('%s', created_at) AS INTEGER) FROM payments WHERE " + cond,
                args,
            )
        await conn.commit()


async def _notify(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("notify %s failed", admin_id)


async def _on_paid(bot: Bot, payment_id: str) -> None:
    """Хук успешной оплаты: пишет продажу (идемпотентно) и проверяет лимит мест."""
    pay = await db.get_payment(payment_id)
    if not pay:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO sales (payment_id, user_id, amount, provider, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (payment_id, pay["user_id"], pay["amount"], pay["provider"], int(time.time())),
        )
        inserted = cur.rowcount == 1
        await conn.commit()
    if inserted:
        await check_seats(bot)


async def _paid_user_ids() -> set[int]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT DISTINCT user_id FROM sales")
        return {r[0] for r in await cur.fetchall()}


# ───────────────────────── лимит мест ─────────────────────────

async def _int_setting(key: str, default: int = 0) -> int:
    try:
        return int(await db.get_setting(key, str(default)) or default)
    except ValueError:
        return default


async def seats_state() -> tuple[int, int] | None:
    """-> (лимит, занято) или None, если лимит выключен."""
    limit = await _int_setting("seats_limit")
    if limit <= 0:
        return None
    since = await _int_setting("seats_from")
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM sales WHERE ts >= ?", (since,))
        taken = (await cur.fetchone())[0]
    return limit, taken


async def check_seats(bot: Bot) -> None:
    st = await seats_state()
    if not st:
        return
    limit, taken = st
    if taken >= limit and await db.is_enrollment_open():
        await db.set_enrollment_open(False)
        await _notify(
            bot,
            f"🔴 Набор закрыт автоматически: заняты все места ({taken}/{limit}).\n"
            "Чтобы открыть снова, задай новый лимит или сбрось счётчик в /auto, затем открой набор.",
        )


# ───────────────────────── анонс при открытии ─────────────────────────

async def _announce(bot: Bot) -> None:
    if await db.get_setting("announce_enabled", "0") != "1":
        return
    chat = await db.get_setting("announce_chat")
    msg = await db.get_setting("announce_msg")
    if not chat or not msg or not ADMIN_IDS:
        return
    await extras.run_broadcast(bot, ADMIN_IDS[0], int(chat), int(msg), exclude=await _paid_user_ids())


async def _set_open_wrapper(is_open: bool) -> None:
    was_open = await db.is_enrollment_open()
    await _orig_set_open(is_open)
    if is_open and not was_open and _bot is not None:
        task = asyncio.create_task(_announce(_bot))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


# ───────────────────────── ежедневный отчёт ─────────────────────────

_HHMM = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_hhmm(text: str) -> tuple[int, int] | None:
    m = _HHMM.match(text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if h < 24 and mi < 60 else None


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def day_start_ts(now: datetime) -> int:
    return int(now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def report_action(now: datetime, enabled: bool, last_date: str, hhmm: tuple[int, int]) -> str:
    """'wait' — рано/не нужно, 'send' — слать, 'skip' — время проспали, ждём завтра."""
    now = now.astimezone(MSK)
    if not enabled or last_date == now.strftime("%Y-%m-%d"):
        return "wait"
    due = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
    if now < due:
        return "wait"
    return "skip" if now - due > REPORT_LATE_LIMIT else "send"


async def build_report(now: datetime | None = None) -> str:
    now = (now or datetime.now(MSK)).astimezone(MSK)
    since = day_start_ts(now)
    async with aiosqlite.connect(DB_PATH) as conn:
        async def one(sql: str, *args):
            cur = await conn.execute(sql, args)
            return await cur.fetchone()

        users_total = (await one("SELECT COUNT(*) FROM users"))[0]
        new_users = (await one(
            "SELECT COUNT(*) FROM users WHERE first_seen >= datetime(?, 'unixepoch')", since))[0]
        active = (await one(
            "SELECT COUNT(*) FROM users WHERE last_seen >= datetime(?, 'unixepoch')", since))[0]
        n, revenue = await one(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM sales WHERE ts >= ?", since)
        total_n, total_rev = await one("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM sales")
        cur = await conn.execute(
            "SELECT provider, COUNT(*), SUM(amount) FROM sales WHERE ts >= ? "
            "GROUP BY provider ORDER BY COUNT(*) DESC", (since,))
        by_provider = await cur.fetchall()
        try:
            cur = await conn.execute(
                "SELECT pp.code, COUNT(*), SUM(pp.base - pp.price) FROM sales s "
                "JOIN promo_payments pp ON pp.payment_id = s.payment_id AND pp.consumed = 1 "
                "WHERE s.ts >= ? GROUP BY pp.code ORDER BY COUNT(*) DESC", (since,))
            by_promo = await cur.fetchall()
        except Exception:
            by_promo = []

    lines = [f"📊 Отчёт за {now.strftime('%d.%m.%Y')} (на {now.strftime('%H:%M')} МСК)", ""]
    lines.append(f"Новых пользователей: {new_users}")
    lines.append(f"Заходили в бота: {active}")
    lines.append(f"Оплат: {n} · выручка: {money(revenue)}")
    if by_provider:
        parts = [f"{PROVIDER_NAMES.get(p, p)} {c} ({money(a)})" for p, c, a in by_provider]
        lines.append("По способам: " + ", ".join(parts))
    if by_promo:
        saved = sum(r[2] for r in by_promo)
        parts = [f"{code} — {c}" for code, c, _ in by_promo]
        lines.append(f"По промокодам: {', '.join(parts)} (скидок на {money(saved)})")
    lines.append("")
    is_open = await db.is_enrollment_open()
    seats = await seats_state()
    status = "🟢 открыт" if is_open else "🔴 закрыт"
    if seats:
        status += f" · места: {seats[1]}/{seats[0]}"
    lines.append(f"Набор: {status}")
    lines.append(f"Всего: пользователей {users_total}, оплат {total_n}, выручка {money(total_rev)}")
    return "\n".join(lines)


async def _report_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            enabled = await db.get_setting("report_enabled", "0") == "1"
            if not enabled:
                continue
            now = datetime.now(MSK)
            hhmm = parse_hhmm(await db.get_setting("report_time", DEFAULT_REPORT_TIME)) or (21, 0)
            action = report_action(now, enabled, await db.get_setting("report_last", "") or "", hhmm)
            if action == "wait":
                continue
            await db.set_setting("report_last", now.strftime("%Y-%m-%d"))  # до отправки: без дублей
            if action == "send":
                await _notify(bot, await build_report(now))
        except Exception:
            log.exception("report loop error")


# ───────────────────────── тексты /start ─────────────────────────

_TXT = {
    "closed": ("text_closed", "🔴 Текст при закрытом наборе"),
    "open": ("text_open", "🟢 Текст при открытом наборе"),
}


async def next_dates() -> tuple[int | None, int | None]:
    """(ближайшее запланированное открытие, ближайшее закрытие после него)."""
    pending = await planner.list_pending()
    opens = [t for t in pending if t["kind"] == "open"]
    if not opens:
        return None, None
    o = opens[0]["run_at"]
    closes = [t["run_at"] for t in pending if t["kind"] == "close" and t["run_at"] > o]
    return o, (closes[0] if closes else None)


def _dm(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=MSK).strftime("%d.%m")


async def render_placeholders(text: str) -> str:
    if "{next_" not in text:
        return text
    o, c = await next_dates()
    if o and c:
        dates = f"{_dm(o)}–{_dm(c)}"
    elif o:
        dates = f"с {_dm(o)}"
    else:
        dates = "скоро"
    return (text.replace("{next_dates}", dates)
                .replace("{next_open}", _dm(o) if o else "скоро")
                .replace("{next_close}", _dm(c) if c else "скоро"))


async def get_start_text(is_open: bool, default: str) -> str:
    """Текст для /start (HTML). Свой текст из бота, иначе default из кода."""
    try:
        stored = await db.get_setting("text_open" if is_open else "text_closed", "")
        text = stored if stored else html.escape(default, quote=False)
        return await render_placeholders(text)
    except Exception:
        log.exception("get_start_text failed")
        return html.escape(default, quote=False)


# ───────────────────────── интерфейс ─────────────────────────

class Auto(StatesGroup):
    seats = State()
    announce = State()
    report_time = State()
    text = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


_BACK = [_btn("◀️ Назад", "aut:menu")]
_CANCEL = [_btn("❌ Отмена", "aut:cancel")]


async def _edit_or_answer(message: Message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=markup)


async def _menu_view():
    seats = await seats_state()
    seats_txt = "выключен" if not seats else f"{seats[0]} (занято {seats[1]})"
    ann = await db.get_setting("announce_enabled", "0") == "1"
    rep = await db.get_setting("report_enabled", "0") == "1"
    rep_time = await db.get_setting("report_time", DEFAULT_REPORT_TIME)
    has_txt = bool(await db.get_setting("text_closed")) or bool(await db.get_setting("text_open"))
    text = (
        "🤖 Автоматика\n\n"
        f"🎯 Лимит мест: {seats_txt}\n"
        f"📣 Анонс при открытии набора: {'включён' if ann else 'выключен'}\n"
        f"📊 Ежедневный отчёт: {'в ' + rep_time + ' МСК' if rep else 'выключен'}\n"
        f"📝 Тексты /start: {'свои' if has_txt else 'стандартные'}"
    )
    return text, _kb([
        [_btn("🎯 Лимит мест", "aut:seats")],
        [_btn("📣 Анонс при открытии", "aut:ann")],
        [_btn("📊 Ежедневный отчёт", "aut:rep")],
        [_btn("📝 Тексты /start", "aut:txt")],
        [_btn("◀️ В админ-панель", "admin_home")],
    ])


async def _seats_view():
    seats = await seats_state()
    if seats:
        limit, taken = seats
        text = (f"🎯 Лимит мест\n\nЛимит: {limit} · занято {taken} · осталось {max(0, limit - taken)}\n\n"
                "Когда места закончатся, набор закроется сам, и придёт уведомление.")
    else:
        text = ("🎯 Лимит мест выключен.\n\nЗадай число мест: набор закроется автоматически, "
                "когда оплат станет столько же. Счётчик стартует с нуля.")
    rows = [[_btn("✏️ Задать лимит", "aut:seats_set")]]
    if seats:
        rows.append([_btn("🔄 Сбросить счётчик", "aut:seats_reset"), _btn("🚫 Выключить", "aut:seats_off")])
    rows.append(_BACK)
    return text, _kb(rows)


async def _ann_view():
    enabled = await db.get_setting("announce_enabled", "0") == "1"
    has_msg = bool(await db.get_setting("announce_msg"))
    status = "⚠️ сообщение не задано" if not has_msg else ("✅ включён" if enabled else "⛔ выключен")
    text = (f"📣 Анонс при открытии набора\n\nСтатус: {status}\n\n"
            "Когда набор открывается (кнопкой или по таймеру), сообщение уходит всем, "
            "кто ещё не оплатил. Не удаляй исходное сообщение из чата с ботом.")
    rows = [[_btn("✏️ Задать сообщение", "aut:ann_set")]]
    if has_msg:
        rows.append([_btn("⏸ Выключить" if enabled else "▶️ Включить", "aut:ann_toggle"),
                     _btn("👁 Показать", "aut:ann_show")])
    rows.append(_BACK)
    return text, _kb(rows)


async def _rep_view():
    enabled = await db.get_setting("report_enabled", "0") == "1"
    rep_time = await db.get_setting("report_time", DEFAULT_REPORT_TIME)
    text = (f"📊 Ежедневный отчёт\n\nСтатус: {'✅ включён' if enabled else '⛔ выключен'}\n"
            f"Время: {rep_time} МСК\n\n"
            "В отчёте: новые и активные пользователи, оплаты и выручка за день, "
            "способы оплаты, промокоды, места.")
    return text, _kb([
        [_btn("⏸ Выключить" if enabled else "▶️ Включить", "aut:rep_toggle"),
         _btn("🕘 Время", "aut:rep_time")],
        [_btn("📨 Прислать сейчас", "aut:rep_now")],
        _BACK,
    ])


async def _show(callback: CallbackQuery, view) -> None:
    await _edit_or_answer(callback.message, *(await view()))
    await callback.answer()


@router.message(Command("auto"))
async def cmd_auto(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _menu_view()
    await message.answer(text, reply_markup=markup)


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    await message.answer(await build_report())


@router.callback_query(F.data.in_({"admin_auto", "aut:menu"}))
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(callback, _menu_view)


@router.callback_query(F.data == "aut:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(callback, _menu_view)


# ── лимит мест ──

@router.callback_query(F.data == "aut:seats")
async def cb_seats(callback: CallbackQuery) -> None:
    await _show(callback, _seats_view)


@router.callback_query(F.data == "aut:seats_set")
async def cb_seats_set(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Auto.seats)
    await callback.message.answer("Сколько мест в этом наборе? Пришли число.", reply_markup=_kb([_CANCEL]))
    await callback.answer()


@router.message(Auto.seats)
async def got_seats(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        raise SkipHandler()
    if not text.isdigit() or int(text) < 1:
        await message.answer("Нужно целое число от 1.")
        return
    await state.clear()
    await db.set_setting("seats_limit", text)
    await db.set_setting("seats_from", str(int(time.time())))
    view = await _seats_view()
    await message.answer("✅ Лимит задан, счётчик пошёл с нуля.\n\n" + view[0], reply_markup=view[1])


@router.callback_query(F.data == "aut:seats_reset")
async def cb_seats_reset(callback: CallbackQuery) -> None:
    await db.set_setting("seats_from", str(int(time.time())))
    await callback.answer("Счётчик сброшен")
    await _edit_or_answer(callback.message, *(await _seats_view()))


@router.callback_query(F.data == "aut:seats_off")
async def cb_seats_off(callback: CallbackQuery) -> None:
    await db.set_setting("seats_limit", "0")
    await callback.answer("Лимит выключен")
    await _edit_or_answer(callback.message, *(await _seats_view()))


# ── анонс ──

@router.callback_query(F.data == "aut:ann")
async def cb_ann(callback: CallbackQuery) -> None:
    await _show(callback, _ann_view)


@router.callback_query(F.data == "aut:ann_set")
async def cb_ann_set(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Auto.announce)
    await callback.message.answer(
        "Пришли сообщение-анонс (текст, фото — любое). Оно уйдёт при открытии набора. "
        "Не удаляй его из чата.",
        reply_markup=_kb([_CANCEL]),
    )
    await callback.answer()


@router.message(Auto.announce)
async def got_announce(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler()
    await state.clear()
    await db.set_setting("announce_chat", str(message.chat.id))
    await db.set_setting("announce_msg", str(message.message_id))
    await db.set_setting("announce_enabled", "1")
    await message.answer("Так это увидят получатели:")
    await message.bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    view = await _ann_view()
    await message.answer("✅ Анонс сохранён и включён.\n\n" + view[0], reply_markup=view[1])


@router.callback_query(F.data == "aut:ann_toggle")
async def cb_ann_toggle(callback: CallbackQuery) -> None:
    enabled = await db.get_setting("announce_enabled", "0") == "1"
    await db.set_setting("announce_enabled", "0" if enabled else "1")
    await callback.answer("Готово")
    await _edit_or_answer(callback.message, *(await _ann_view()))


@router.callback_query(F.data == "aut:ann_show")
async def cb_ann_show(callback: CallbackQuery) -> None:
    chat = await db.get_setting("announce_chat")
    msg = await db.get_setting("announce_msg")
    try:
        await callback.bot.copy_message(callback.from_user.id, int(chat), int(msg))
    except Exception:
        await callback.message.answer("Не удалось показать: исходное сообщение, похоже, удалено. Задай анонс заново.")
    await callback.answer()


# ── отчёт ──

@router.callback_query(F.data == "aut:rep")
async def cb_rep(callback: CallbackQuery) -> None:
    await _show(callback, _rep_view)


@router.callback_query(F.data == "aut:rep_toggle")
async def cb_rep_toggle(callback: CallbackQuery) -> None:
    enabled = await db.get_setting("report_enabled", "0") == "1"
    await db.set_setting("report_enabled", "0" if enabled else "1")
    if not enabled:  # включили: если время сегодня уже прошло, первый отчёт будет завтра
        now = datetime.now(MSK)
        hhmm = parse_hhmm(await db.get_setting("report_time", DEFAULT_REPORT_TIME)) or (21, 0)
        if report_action(now, True, "", hhmm) != "wait":
            await db.set_setting("report_last", now.strftime("%Y-%m-%d"))
    await callback.answer("Готово")
    await _edit_or_answer(callback.message, *(await _rep_view()))


@router.callback_query(F.data == "aut:rep_time")
async def cb_rep_time(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Auto.report_time)
    await callback.message.answer("Во сколько присылать отчёт (МСК)? Например: 21:00",
                                  reply_markup=_kb([_CANCEL]))
    await callback.answer()


@router.message(Auto.report_time)
async def got_report_time(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        raise SkipHandler()
    hhmm = parse_hhmm(text)
    if not hhmm:
        await message.answer("Формат ЧЧ:ММ, например 21:00.")
        return
    await state.clear()
    await db.set_setting("report_time", f"{hhmm[0]:02d}:{hhmm[1]:02d}")
    view = await _rep_view()
    await message.answer("✅ Время сохранено.\n\n" + view[0], reply_markup=view[1])


@router.callback_query(F.data == "aut:rep_now")
async def cb_rep_now(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(await build_report())


# ── тексты /start ──

async def _txt_menu_view():
    cl = bool(await db.get_setting("text_closed"))
    op = bool(await db.get_setting("text_open"))
    text = (
        "📝 Тексты на /start\n\n"
        f"🔴 Набор закрыт: {'свой текст' if cl else 'стандартный'}\n"
        f"🟢 Набор открыт: {'свой текст' if op else 'стандартный'}\n\n"
        "Тексты меняются сразу, без правок в GitHub."
    )
    return text, _kb([
        [_btn("🔴 Набор закрыт", "aut:txt:closed")],
        [_btn("🟢 Набор открыт", "aut:txt:open")],
        _BACK,
    ])


async def _txt_view(kind: str):
    key, title = _TXT[kind]
    custom = bool(await db.get_setting(key))
    o, c = await next_dates()
    now_dates = "скоро (ничего не запланировано)"
    if o and c:
        now_dates = f"{_dm(o)}–{_dm(c)}"
    elif o:
        now_dates = f"с {_dm(o)}"
    text = (
        f"{title}\n\n"
        f"Сейчас: {'свой текст' if custom else 'стандартный (зашит в user.py)'}\n\n"
        "Подстановки, которые меняются сами:\n"
        "{next_dates} — даты ближайшего набора, например 24.10–31.10\n"
        "{next_open} — дата открытия, {next_close} — дата закрытия\n"
        "Даты берутся из запланированных в «⏰ Отложенные» открытия и закрытия. "
        f"Сейчас {{next_dates}} = {now_dates}.\n\n"
        "Форматирование (жирный, курсив, ссылки) сохраняется."
    )
    rows = [[_btn("✏️ Изменить", f"aut:txt_set:{kind}")]]
    if custom:
        rows[0].append(_btn("👁 Предпросмотр", f"aut:txt_prev:{kind}"))
        rows.append([_btn("♻️ Сбросить на стандартный", f"aut:txt_reset:{kind}")])
    rows.append([_btn("◀️ Назад", "aut:txt")])
    return text, _kb(rows)


@router.callback_query(F.data == "aut:txt")
async def cb_txt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(callback, _txt_menu_view)


@router.callback_query(F.data.in_({"aut:txt:closed", "aut:txt:open"}))
async def cb_txt_item(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kind = callback.data.split(":")[2]
    await _edit_or_answer(callback.message, *(await _txt_view(kind)))
    await callback.answer()


@router.callback_query(F.data.startswith("aut:txt_set:"))
async def cb_txt_set(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[2]
    await state.set_state(Auto.text)
    await state.update_data(kind=kind)
    await callback.message.answer(
        "Пришли новый текст одним сообщением (до 1000 символов). "
        "Можно использовать форматирование и подстановки {next_dates}, {next_open}, {next_close}.",
        reply_markup=_kb([[_btn("❌ Отмена", f"aut:txt:{kind}")]]),
    )
    await callback.answer()


@router.message(Auto.text)
async def got_txt(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        raise SkipHandler()
    if not message.text:
        await message.answer("Пришли текст сообщением.")
        return
    if len(message.text) > 1000:
        await message.answer(f"Слишком длинно: {len(message.text)} из 1000 символов "
                             "(подпись к фото ограничена Telegram). Сократи.")
        return
    kind = (await state.get_data())["kind"]
    await state.clear()
    await db.set_setting(_TXT[kind][0], message.html_text)
    await message.answer(await render_placeholders(message.html_text), parse_mode="HTML")
    view = await _txt_view(kind)
    await message.answer("✅ Сохранено. Выше — как это увидит пользователь.\n\n" + view[0],
                         reply_markup=view[1])


@router.callback_query(F.data.startswith("aut:txt_prev:"))
async def cb_txt_prev(callback: CallbackQuery) -> None:
    kind = callback.data.split(":")[2]
    stored = await db.get_setting(_TXT[kind][0], "")
    if stored:
        await callback.message.answer(await render_placeholders(stored), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("aut:txt_reset:"))
async def cb_txt_reset(callback: CallbackQuery) -> None:
    kind = callback.data.split(":")[2]
    await db.set_setting(_TXT[kind][0], "")
    await callback.answer("Сброшено")
    await _edit_or_answer(callback.message, *(await _txt_view(kind)))


# ───────────────────────── подключение ─────────────────────────

async def setup(dp: Dispatcher, bot: Bot) -> None:
    """Вызвать в bot.py после planner.setup (всё ещё до остальных include_router)."""
    global _bot, _orig_set_open
    _bot = bot
    await init()
    if _orig_set_open is None:
        _orig_set_open = db.set_enrollment_open
        db.set_enrollment_open = _set_open_wrapper
    if _on_paid not in promo.PAID_HOOKS:
        promo.PAID_HOOKS.append(_on_paid)
    dp.include_router(router)
    task = asyncio.create_task(_report_loop(bot))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.info("automation ready")
