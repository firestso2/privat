"""
promo.py — система промокодов.

Возможности:
- скидка в % или в ₽;
- ограничение по времени (таймер с момента создания или до даты), бессрочный;
- ограничение по числу использований: счётчик уменьшается ТОЛЬКО при успешной
  покупке (применять код может сколько угодно людей), либо без лимита;
- время и лимит можно комбинировать (сработает то, что наступит раньше);
- код вводится в главном меню оплаты (рядом с СБП и CryptoBot), цена меняется сразу;
- если код стал неактивен (время, лимит, выключен, удалён) — цена автоматически
  возвращается к обычной, пользователь получает предупреждение;
- админка: создание по шагам, список, карточка со статистикой, вкл/выкл,
  продление, добавление использований, удаление;
- защита от подбора кодов, уведомления админу о покупках и об окончании кода.

Подключение — см. README_PROMO.md.
"""
import asyncio
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from config import ADMIN_IDS, DB_PATH

log = logging.getLogger("promo")

MIN_PRICE = 1.0                 # ниже этой цены скидка не опустит
MSK = timezone(timedelta(hours=3))
NOTIFY_ON_PURCHASE = True       # писать админу о каждой покупке по промокоду
MAX_FAILS, FAIL_WINDOW, FAIL_BLOCK = 5, 600, 600   # защита от подбора

user_router = Router()
adm = Router()
adm.message.filter(F.from_user.id.in_(set(ADMIN_IDS)))
adm.callback_query.filter(F.from_user.id.in_(set(ADMIN_IDS)))

_tasks: set[asyncio.Task] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promos (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- percent | fixed
    value REAL NOT NULL,
    expires_at INTEGER,                 -- unix UTC, NULL = бессрочный
    max_uses INTEGER,                   -- NULL = без лимита
    used INTEGER NOT NULL DEFAULT 0,    -- растёт только при покупке
    active INTEGER NOT NULL DEFAULT 1,
    expiry_notified INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_promo (
    user_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_payments (
    payment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    base REAL NOT NULL,
    price REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""
_COLS = "code, kind, value, expires_at, max_uses, used, active"


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


# ───────────────────────── чистая логика ─────────────────────────

def base_price() -> float:
    return float(config.SUBSCRIPTION_PRICE)


def _row(r) -> dict | None:
    if r is None:
        return None
    return dict(zip(_COLS.replace(" ", "").split(","), r))


def promo_status(p: dict, now: int | None = None) -> str:
    """ok | disabled | expired | exhausted"""
    now = int(time.time()) if now is None else now
    if not p["active"]:
        return "disabled"
    if p["expires_at"] is not None and p["expires_at"] <= now:
        return "expired"
    if p["max_uses"] is not None and p["used"] >= p["max_uses"]:
        return "exhausted"
    return "ok"


def apply_discount(base: float, kind: str, value: float) -> float:
    price = base * (1 - value / 100) if kind == "percent" else base - value
    price = math.floor(price + 0.5)  # до целых рублей
    return float(max(MIN_PRICE, min(price, base)))


def fmt_money(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def fmt_left(sec: int) -> str:
    if sec < 60:
        return "меньше минуты"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def fmt_disc(kind: str, value: float) -> str:
    return f"−{fmt_money(value)}%" if kind == "percent" else f"−{fmt_money(value)} ₽"


_UNIT = {"m": 60, "min": 60, "мин": 60, "h": 3600, "ч": 3600,
         "d": 86400, "д": 86400, "w": 604800, "н": 604800}
_TOKEN = re.compile(r"(\d+)\s*(мин|min|m|ч|h|д|d|н|w)", re.I)
_FULL = re.compile(r"^\s*(?:\d+\s*(?:мин|min|m|ч|h|д|d|н|w)\s*)+$", re.I)


def parse_ttl(text: str, now: int | None = None):
    """
    '90m', '12h', '3d', '1d12h', '2н' -> (секунды, None)
    '31.12.2026 23:59' (МСК)          -> (None, unix_ts)
    Невалидно или в прошлом -> None.
    """
    now = int(time.time()) if now is None else now
    text = text.strip()
    if _FULL.match(text):
        sec = sum(int(n) * _UNIT[u.lower()] for n, u in _TOKEN.findall(text))
        return (sec, None) if sec > 0 else None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            ts = int(datetime.strptime(text, fmt).replace(tzinfo=MSK).timestamp())
        except ValueError:
            continue
        return (None, ts) if ts > now else None
    return None


# ───────────────────────── работа с БД ─────────────────────────

async def get_promo(code: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT {_COLS} FROM promos WHERE code = ?", (code,))
        return _row(await cur.fetchone())


async def create_promo(code, kind, value, expires_at, max_uses) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM promos WHERE code = ?", (code,))
        if await cur.fetchone():
            return False
        await db.execute(
            "INSERT INTO promos (code, kind, value, expires_at, max_uses, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, kind, value, expires_at, max_uses, int(time.time())),
        )
        await db.commit()
        return True


async def list_promos(limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"SELECT {_COLS} FROM promos ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [_row(r) for r in await cur.fetchall()]


async def toggle_promo(code: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE promos SET active = 1 - active WHERE code = ?", (code,))
        await db.commit()


async def extend_promo(code: str, seconds: int) -> None:
    p = await get_promo(code)
    if not p or p["expires_at"] is None:
        return
    new_exp = max(p["expires_at"], int(time.time())) + seconds
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE promos SET expires_at = ?, expiry_notified = 0 WHERE code = ?",
            (new_exp, code),
        )
        await db.commit()


async def add_uses(code: str, n: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE promos SET max_uses = max_uses + ? WHERE code = ? AND max_uses IS NOT NULL",
            (n, code),
        )
        await db.commit()


async def delete_promo(code: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promos WHERE code = ?", (code,))
        await db.execute("DELETE FROM user_promo WHERE code = ?", (code,))
        await db.commit()


async def promo_stats(code: str) -> tuple[int, float, float]:
    """(покупок, выручка по этому коду, сколько скидок выдано)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(price), 0), COALESCE(SUM(base - price), 0) "
            "FROM promo_payments WHERE code = ? AND consumed = 1",
            (code,),
        )
        n, rev, saved = await cur.fetchone()
        return n, rev, saved


# ───────────────────────── цена для пользователя ─────────────────────────

@dataclass
class PriceInfo:
    base: float
    final: float
    code: str | None = None
    kind: str | None = None
    value: float | None = None
    left_sec: int | None = None      # сколько осталось по времени (None = бессрочно)
    uses_left: int | None = None     # сколько осталось использований (None = без лимита)
    dropped: str | None = None       # код, который только что перестал действовать
    dropped_reason: str | None = None


async def get_price(user_id: int) -> PriceInfo:
    """Актуальная цена для пользователя. Если его промокод уже неактивен — снимает его."""
    base = base_price()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT u.code, p.kind, p.value, p.expires_at, p.max_uses, p.used, p.active "
            "FROM user_promo u LEFT JOIN promos p ON p.code = u.code WHERE u.user_id = ?",
            (user_id,),
        )
        r = await cur.fetchone()
        if not r:
            return PriceInfo(base, base)
        code = r[0]
        if r[1] is None:  # промокод удалён
            status, p = "deleted", None
        else:
            p = dict(zip(_COLS.replace(" ", "").split(","), r))
            status = promo_status(p)
        if status != "ok":
            await db.execute("DELETE FROM user_promo WHERE user_id = ?", (user_id,))
            await db.commit()
            return PriceInfo(base, base, dropped=code, dropped_reason=status)
    now = int(time.time())
    return PriceInfo(
        base=base,
        final=apply_discount(base, p["kind"], p["value"]),
        code=code,
        kind=p["kind"],
        value=p["value"],
        left_sec=None if p["expires_at"] is None else max(0, p["expires_at"] - now),
        uses_left=None if p["max_uses"] is None else max(0, p["max_uses"] - p["used"]),
    )


_REASON = {
    "disabled": "отключён",
    "expired": "истёк по времени",
    "exhausted": "закончился (исчерпан лимит использований)",
    "deleted": "больше не существует",
}


def price_text(info: PriceInfo) -> str:
    lines = []
    if info.dropped:
        why = _REASON.get(info.dropped_reason, "больше не действует")
        lines.append(f"⚠️ Промокод <b>{info.dropped}</b> {why}. Цена обновлена.\n")
    if info.code:
        lines.append(
            f"Стоимость доступа: <s>{fmt_money(info.base)} ₽</s> <b>{fmt_money(info.final)} ₽</b>"
        )
        lines.append(f"🎟 Промокод <b>{info.code}</b>: {fmt_disc(info.kind, info.value)}")
        if info.left_sec is not None:
            lines.append(f"⏳ Действует ещё {fmt_left(info.left_sec)}")
        if info.uses_left is not None:
            lines.append(f"🔥 Осталось использований: {info.uses_left}")
    else:
        lines.append(f"Стоимость доступа: <b>{fmt_money(info.base)} ₽</b>")
    lines.append("Выбери способ оплаты:")
    return "\n".join(lines)


def with_button(markup: InlineKeyboardMarkup, info: PriceInfo) -> InlineKeyboardMarkup:
    """Добавляет под СБП/CryptoBot кнопку промокода."""
    if info.code:
        btn = InlineKeyboardButton(text=f"🎟 {info.code} · убрать", callback_data="promo_clear")
    else:
        btn = InlineKeyboardButton(text="🎟 Промокод", callback_data="promo_enter")
    return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, [btn]])


async def show_price_menu(message: Message, user_id: int, info: PriceInfo | None = None) -> None:
    import keyboards as kb  # меню способов оплаты
    info = info or await get_price(user_id)
    await message.answer(
        price_text(info),
        reply_markup=with_button(kb.method_menu(), info),
        parse_mode="HTML",
    )


# ───────────────── привязка к платежу и списание при покупке ─────────────────

async def attach_to_payment(payment_id: str, user_id: int) -> None:
    """Вызывается при создании платежа: запоминает, с каким промокодом он создан."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT u.code, p.kind, p.value, p.expires_at, p.max_uses, p.used, p.active "
            "FROM user_promo u JOIN promos p ON p.code = u.code WHERE u.user_id = ?",
            (user_id,),
        )
        r = await cur.fetchone()
        if not r:
            return
        p = dict(zip(_COLS.replace(" ", "").split(","), r))
        if promo_status(p) != "ok":
            return
        base = base_price()
        await db.execute(
            "INSERT OR IGNORE INTO promo_payments "
            "(payment_id, user_id, code, base, price, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (payment_id, user_id, p["code"], base,
             apply_discount(base, p["kind"], p["value"]), int(time.time())),
        )
        await db.commit()


async def on_paid(bot: Bot, payment_id: str) -> None:
    """Вызывается при успешной оплате. Идемпотентно: списывает использование один раз."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE promo_payments SET consumed = 1 WHERE payment_id = ? AND consumed = 0",
            (payment_id,),
        )
        if cur.rowcount != 1:
            await db.commit()
            return
        cur = await db.execute(
            "SELECT user_id, code, price FROM promo_payments WHERE payment_id = ?",
            (payment_id,),
        )
        user_id, code, price = await cur.fetchone()
        await db.execute("UPDATE promos SET used = used + 1 WHERE code = ?", (code,))
        await db.execute("DELETE FROM user_promo WHERE user_id = ?", (user_id,))
        await db.commit()
    p = await get_promo(code)
    if NOTIFY_ON_PURCHASE:
        await _notify(bot, f"💸 Покупка по промокоду {code}: {fmt_money(price)} ₽ (id {user_id})")
    if p and p["max_uses"] is not None and p["used"] >= p["max_uses"]:
        await _notify(bot, f"🔴 Промокод {code} исчерпан: {p['used']}/{p['max_uses']} использований.")


async def _notify(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("notify admin %s failed", admin_id)


# ───────────────────────── защита от подбора ─────────────────────────

_fails: dict[int, list[float]] = {}
_blocked_until: dict[int, float] = {}


def rate_left(user_id: int) -> int:
    return max(0, int(_blocked_until.get(user_id, 0) - time.time()))


def register_fail(user_id: int) -> None:
    now = time.time()
    fails = [t for t in _fails.get(user_id, []) if now - t < FAIL_WINDOW] + [now]
    _fails[user_id] = fails
    if len(fails) >= MAX_FAILS:
        _blocked_until[user_id] = now + FAIL_BLOCK
        _fails[user_id] = []


def reset_fails(user_id: int) -> None:
    _fails.pop(user_id, None)


# ───────────────────────── пользователь: ввод промокода ─────────────────────────

class PromoUser(StatesGroup):
    code = State()


_CODE_RE = re.compile(r"^[A-Z0-9_-]{2,32}$")


def _norm(text: str) -> str:
    return text.strip().upper()


def _cancel_user_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="promo_cancel")]]
    )


@user_router.callback_query(F.data == "promo_enter")
async def promo_enter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoUser.code)
    await callback.message.answer("Отправь промокод сообщением.", reply_markup=_cancel_user_kb())
    await callback.answer()


@user_router.callback_query(F.data == "promo_cancel")
async def promo_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")


@user_router.callback_query(F.data == "promo_clear")
async def promo_clear(callback: CallbackQuery) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_promo WHERE user_id = ?", (callback.from_user.id,))
        await db.commit()
    await callback.answer("Промокод убран")
    await show_price_menu(callback.message, callback.from_user.id)


@user_router.message(PromoUser.code, F.text.startswith("/"))
async def promo_command_in_state(message: Message, state: FSMContext) -> None:
    await state.clear()      # команда важнее ввода кода
    raise SkipHandler()


@user_router.message(PromoUser.code, F.text)
async def promo_code_entered(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    wait = rate_left(uid)
    if wait:
        await message.answer(f"Слишком много неверных попыток. Попробуй через {wait // 60 + 1} мин.")
        return

    code = _norm(message.text)
    p = await get_promo(code) if _CODE_RE.match(code) else None
    if p is None:
        register_fail(uid)
        await message.answer(
            "❌ Такого промокода нет. Проверь написание и отправь ещё раз.",
            reply_markup=_cancel_user_kb(),
        )
        return

    status = promo_status(p)
    if status != "ok":
        await state.clear()
        await message.answer(f"⌛ Промокод {code} {_REASON[status]}.")
        await show_price_menu(message, uid)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_promo (user_id, code, applied_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET code = excluded.code, applied_at = excluded.applied_at",
            (uid, code, int(time.time())),
        )
        await db.commit()
    reset_fails(uid)
    await state.clear()
    await show_price_menu(message, uid)


@user_router.message(PromoUser.code)
async def promo_not_text(message: Message) -> None:
    await message.answer("Пришли промокод текстом.", reply_markup=_cancel_user_kb())


# ───────────────────────── админка ─────────────────────────

class PromoNew(StatesGroup):
    value = State()
    code = State()
    ttl = State()
    uses = State()
    confirm = State()


_ICON = {"ok": "🟢", "disabled": "⏸", "expired": "⌛", "exhausted": "🔴"}
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _cancel_row() -> list:
    return [_btn("❌ Отмена", "pnew:cancel")]


async def _edit_or_answer(message: Message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=markup)


async def _list_view() -> tuple[str, InlineKeyboardMarkup]:
    promos = await list_promos()
    rows = []
    for p in promos:
        lim = "∞" if p["max_uses"] is None else p["max_uses"]
        rows.append([_btn(
            f"{_ICON[promo_status(p)]} {p['code']} · {fmt_disc(p['kind'], p['value'])} · {p['used']}/{lim}",
            f"padm:view:{p['code']}",
        )])
    rows.append([_btn("➕ Создать промокод", "pnew:start")])
    text = "🎟 Промокоды\n(покупки/лимит; 🟢 активен, ⏸ выключен, ⌛ истёк, 🔴 исчерпан)"
    if not promos:
        text = "🎟 Промокодов пока нет."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _card_view(code: str) -> tuple[str, InlineKeyboardMarkup] | None:
    p = await get_promo(code)
    if not p:
        return None
    st = promo_status(p)
    now = int(time.time())
    n, revenue, saved = await promo_stats(code)
    if p["expires_at"] is None:
        term = "бессрочный"
    elif p["expires_at"] > now:
        term = f"до {fmt_dt(p['expires_at'])} (осталось {fmt_left(p['expires_at'] - now)})"
    else:
        term = f"истёк {fmt_dt(p['expires_at'])}"
    if p["max_uses"] is None:
        uses = f"без лимита (куплено {p['used']})"
    else:
        uses = f"{p['used']} из {p['max_uses']} (осталось {max(0, p['max_uses'] - p['used'])})"
    status_txt = {"ok": "активен", "disabled": "выключен", "expired": "истёк по времени",
                  "exhausted": "исчерпан"}[st]
    text = (
        f"🎟 {code} — {_ICON[st]} {status_txt}\n\n"
        f"Скидка: {fmt_disc(p['kind'], p['value'])}\n"
        f"Срок: {term}\n"
        f"Использований: {uses}\n\n"
        f"Покупок: {n} · выручка по коду: {fmt_money(revenue)} ₽ · скидок выдано: {fmt_money(saved)} ₽"
    )
    rows = [[_btn("⏸ Выключить" if p["active"] else "▶️ Включить", f"padm:tog:{code}")]]
    if p["expires_at"] is not None:
        rows.append([_btn("⏱ +1 день", f"padm:ext:{code}:86400"),
                     _btn("⏱ +7 дней", f"padm:ext:{code}:604800")])
    if p["max_uses"] is not None:
        rows.append([_btn("➕ +5 использований", f"padm:addu:{code}:5"),
                     _btn("➕ +10", f"padm:addu:{code}:10")])
    rows.append([_btn("🗑 Удалить", f"padm:del:{code}")])
    rows.append([_btn("◀️ К списку", "padm:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@adm.message(Command("promo"))
async def adm_promo_cmd(message: Message) -> None:
    text, markup = await _list_view()
    await message.answer(text, reply_markup=markup)


@adm.callback_query(F.data.in_({"admin_promos", "padm:list"}))
async def adm_promo_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _list_view()
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer()


@adm.callback_query(F.data.startswith("padm:view:"))
async def adm_view(callback: CallbackQuery) -> None:
    view = await _card_view(callback.data.split(":", 2)[2])
    if not view:
        await callback.answer("Не найден", show_alert=True)
        return
    await _edit_or_answer(callback.message, *view)
    await callback.answer()


async def _refresh_card(callback: CallbackQuery, code: str) -> None:
    view = await _card_view(code)
    if view:
        await _edit_or_answer(callback.message, *view)


@adm.callback_query(F.data.startswith("padm:tog:"))
async def adm_toggle(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 2)[2]
    await toggle_promo(code)
    await _refresh_card(callback, code)
    await callback.answer("Готово")


@adm.callback_query(F.data.startswith("padm:ext:"))
async def adm_extend(callback: CallbackQuery) -> None:
    _, _, code, sec = callback.data.split(":")
    await extend_promo(code, int(sec))
    await _refresh_card(callback, code)
    await callback.answer("Продлён")


@adm.callback_query(F.data.startswith("padm:addu:"))
async def adm_add_uses(callback: CallbackQuery) -> None:
    _, _, code, n = callback.data.split(":")
    await add_uses(code, int(n))
    await _refresh_card(callback, code)
    await callback.answer("Добавлено")


@adm.callback_query(F.data.startswith("padm:del:"))
async def adm_delete_ask(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 2)[2]
    await _edit_or_answer(
        callback.message,
        f"Удалить промокод {code}? У тех, кто его применил, цена вернётся к обычной.",
        InlineKeyboardMarkup(inline_keyboard=[[
            _btn("🗑 Да, удалить", f"padm:delc:{code}"), _btn("Нет", f"padm:view:{code}"),
        ]]),
    )
    await callback.answer()


@adm.callback_query(F.data.startswith("padm:delc:"))
async def adm_delete(callback: CallbackQuery) -> None:
    await delete_promo(callback.data.split(":", 2)[2])
    text, markup = await _list_view()
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer("Удалён")


# ── создание по шагам ──

@adm.callback_query(F.data == "pnew:cancel")
async def new_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit_or_answer(callback.message, "Создание промокода отменено.")
    await callback.answer()


@adm.message(Command("cancel"), StateFilter(PromoNew))
async def new_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Создание промокода отменено.")


@adm.callback_query(F.data == "pnew:start")
async def new_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit_or_answer(
        callback.message,
        "Тип скидки:",
        InlineKeyboardMarkup(inline_keyboard=[
            [_btn("% Процент", "pnew:kind:percent"), _btn("₽ Рубли", "pnew:kind:fixed")],
            _cancel_row(),
        ]),
    )
    await callback.answer()


@adm.callback_query(F.data.startswith("pnew:kind:"))
async def new_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[2]
    await state.update_data(kind=kind)
    await state.set_state(PromoNew.value)
    ask = ("Размер скидки в процентах (от 1 до 99):" if kind == "percent"
           else f"Размер скидки в рублях (меньше {fmt_money(base_price())}):")
    await _edit_or_answer(callback.message, ask, InlineKeyboardMarkup(inline_keyboard=[_cancel_row()]))
    await callback.answer()


@adm.message(PromoNew.value, F.text & ~F.text.startswith("/"))
async def new_value(message: Message, state: FSMContext) -> None:
    try:
        v = float(message.text.replace(",", ".").strip())
    except ValueError:
        await message.answer("Нужно число.")
        return
    kind = (await state.get_data())["kind"]
    ok = 0 < v < 100 if kind == "percent" else 0 < v < base_price()
    if not ok:
        await message.answer("Значение вне допустимого диапазона, попробуй ещё раз.")
        return
    await state.update_data(value=v)
    await state.set_state(PromoNew.code)
    await message.answer(
        "Пришли код (латиница, цифры, - и _, 2–32 символа) или сгенерируй случайный.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🎲 Случайный", "pnew:auto")], _cancel_row()]),
    )


async def _ask_ttl(message: Message, state: FSMContext, code: str) -> None:
    await state.update_data(code=code)
    await state.set_state(PromoNew.ttl)
    await message.answer(
        f"Код: {code}\n\nСколько действует? Таймер пойдёт с момента создания.\n"
        "Или пришли своё: 90m, 12h, 3d, 1d12h либо дату 31.12.2026 23:59 (МСК).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("♾ Бессрочный", "pnew:ttl:0")],
            [_btn("1 час", "pnew:ttl:3600"), _btn("24 часа", "pnew:ttl:86400")],
            [_btn("7 дней", "pnew:ttl:604800"), _btn("30 дней", "pnew:ttl:2592000")],
            _cancel_row(),
        ]),
    )


@adm.callback_query(F.data == "pnew:auto", PromoNew.code)
async def new_auto(callback: CallbackQuery, state: FSMContext) -> None:
    while True:
        code = "".join(random.choice(_ALPHABET) for _ in range(8))
        if not await get_promo(code):
            break
    await _ask_ttl(callback.message, state, code)
    await callback.answer()


@adm.message(PromoNew.code, F.text & ~F.text.startswith("/"))
async def new_code(message: Message, state: FSMContext) -> None:
    code = _norm(message.text)
    if not _CODE_RE.match(code):
        await message.answer("Допустимо: латиница, цифры, - и _, длина 2–32.")
        return
    if await get_promo(code):
        await message.answer("Такой код уже есть, придумай другой.")
        return
    await _ask_ttl(message, state, code)


async def _ask_uses(message: Message, state: FSMContext, dur, abs_ts) -> None:
    await state.update_data(dur=dur, abs_ts=abs_ts)
    await state.set_state(PromoNew.uses)
    await message.answer(
        "Лимит использований. Уменьшается только при покупке — "
        "применять код могут сколько угодно людей.\nМожно прислать своё число.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("♾ Без лимита", "pnew:uses:0")],
            [_btn("1", "pnew:uses:1"), _btn("5", "pnew:uses:5"),
             _btn("10", "pnew:uses:10"), _btn("50", "pnew:uses:50")],
            _cancel_row(),
        ]),
    )


@adm.callback_query(F.data.startswith("pnew:ttl:"), PromoNew.ttl)
async def new_ttl_btn(callback: CallbackQuery, state: FSMContext) -> None:
    sec = int(callback.data.split(":")[2])
    await _ask_uses(callback.message, state, sec or None, None)
    await callback.answer()


@adm.message(PromoNew.ttl, F.text & ~F.text.startswith("/"))
async def new_ttl_text(message: Message, state: FSMContext) -> None:
    parsed = parse_ttl(message.text)
    if not parsed:
        await message.answer("Не понял. Примеры: 90m, 12h, 3d, 1d12h или 31.12.2026 23:59 (дата должна быть в будущем).")
        return
    await _ask_uses(message, state, *parsed)


async def _ask_confirm(message: Message, state: FSMContext, max_uses) -> None:
    await state.update_data(max_uses=max_uses)
    d = await state.get_data()
    now = int(time.time())
    expires = now + d["dur"] if d.get("dur") else d.get("abs_ts")
    term = "бессрочный" if expires is None else f"до {fmt_dt(expires)}"
    uses = "без лимита" if max_uses is None else str(max_uses)
    await state.set_state(PromoNew.confirm)
    await message.answer(
        "Проверь промокод:\n\n"
        f"Код: {d['code']}\n"
        f"Скидка: {fmt_disc(d['kind'], d['value'])}\n"
        f"Срок: {term}\n"
        f"Использований: {uses}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Создать", "pnew:ok")], _cancel_row()]),
    )


@adm.callback_query(F.data.startswith("pnew:uses:"), PromoNew.uses)
async def new_uses_btn(callback: CallbackQuery, state: FSMContext) -> None:
    n = int(callback.data.split(":")[2])
    await _ask_confirm(callback.message, state, n or None)
    await callback.answer()


@adm.message(PromoNew.uses, F.text & ~F.text.startswith("/"))
async def new_uses_text(message: Message, state: FSMContext) -> None:
    t = message.text.strip()
    if not t.isdigit() or int(t) < 1:
        await message.answer("Нужно целое число от 1.")
        return
    await _ask_confirm(message, state, int(t))


@adm.callback_query(F.data == "pnew:ok", PromoNew.confirm)
async def new_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    now = int(time.time())
    expires = now + d["dur"] if d.get("dur") else d.get("abs_ts")
    if not await create_promo(d["code"], d["kind"], d["value"], expires, d.get("max_uses")):
        await _edit_or_answer(callback.message, "Такой код уже существует.")
        await callback.answer()
        return
    view = await _card_view(d["code"])
    await _edit_or_answer(callback.message, "✅ Создан\n\n" + view[0], view[1])
    await callback.answer()


# ───────────────────────── фон и подключение ─────────────────────────

async def _watch_expiry(bot: Bot) -> None:
    """Сообщает админу, когда промокод истёк по времени."""
    while True:
        await asyncio.sleep(30)
        try:
            now = int(time.time())
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT code FROM promos WHERE active = 1 AND expires_at IS NOT NULL "
                    "AND expires_at <= ? AND expiry_notified = 0",
                    (now,),
                )
                codes = [r[0] for r in await cur.fetchall()]
                for code in codes:
                    await db.execute("UPDATE promos SET expiry_notified = 1 WHERE code = ?", (code,))
                await db.commit()
            for code in codes:
                await _notify(bot, f"⌛ Промокод {code} истёк по времени.")
        except Exception:
            log.exception("expiry watcher error")


async def setup(dp: Dispatcher, bot: Bot) -> None:
    """Вызвать в bot.py сразу после extras.setup(dp, bot), до остальных include_router."""
    await init()
    dp.include_router(adm)
    dp.include_router(user_router)
    task = asyncio.create_task(_watch_expiry(bot))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.info("promo ready")
