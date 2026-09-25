import asyncio
import io
import logging

import qrcode
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, BufferedInputFile

import database as db
import keyboards as kb
import payments
import cryptobot_payments
import xrocket_payments
import access
from payment_dispatch import PAID_STATUS, DEAD_STATUSES, check_status
from config import (
    SUBSCRIPTION_PRICE,
    SUBSCRIPTION_DESCRIPTION,
    PAYMENT_POLL_INTERVAL_SEC,
    PAYMENT_POLL_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

router = Router()

FALLBACK_TEXT_OPEN = (
    "Добро пожаловать в JKG community.\n\n"
    "Нажми «войти в приватку», чтобы оформить доступ, "
    "или посмотри, что внутри"
)
FALLBACK_TEXT_CLOSED = "Набор в приватку сейчас закрыт. Следующий набор начнется 3 октября."


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    is_open = await db.is_enrollment_open()
    markup = kb.main_menu(is_open)

    photo_id = await db.get_open_photo() if is_open else await db.get_closed_photo()
    text = FALLBACK_TEXT_OPEN if is_open else FALLBACK_TEXT_CLOSED

    if photo_id:
        await message.answer_photo(photo_id, caption=text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "enter_private")
async def enter_private(callback: CallbackQuery) -> None:
    if not await db.is_enrollment_open():
        await callback.answer("Набор сейчас закрыт", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        f"Стоимость доступа: {SUBSCRIPTION_PRICE:.0f} ₽\nВыбери способ оплаты:",
        reply_markup=kb.method_menu(),
    )


@router.callback_query(F.data.startswith("pay_method:"))
async def choose_payment_method(callback: CallbackQuery) -> None:
    provider = callback.data.split(":", 1)[1]
    await callback.answer()
    payment_id = None

    try:
        if provider == "card":
            payment_id, confirmation_url = payments.create_payment(
                callback.from_user.id, SUBSCRIPTION_PRICE, SUBSCRIPTION_DESCRIPTION
            )
            await db.create_payment_record(payment_id, callback.from_user.id, SUBSCRIPTION_PRICE, provider)
            await callback.message.answer(
                "Оплати по кнопке ниже — доступ откроется автоматически после подтверждения оплаты.",
                reply_markup=kb.pay_button(confirmation_url),
            )

        elif provider == "sbp":
            payment_id, qr_payload = payments.create_sbp_payment(
                callback.from_user.id, SUBSCRIPTION_PRICE, SUBSCRIPTION_DESCRIPTION
            )
            await db.create_payment_record(payment_id, callback.from_user.id, SUBSCRIPTION_PRICE, provider)
            await callback.message.answer_photo(
                _qr_image(qr_payload),
                caption="Отсканируй QR в приложении банка, которое поддерживает СБП. "
                        "Доступ откроется автоматически после подтверждения оплаты.",
            )

        elif provider == "cryptobot":
            invoice_id, pay_url = await cryptobot_payments.create_invoice(
                SUBSCRIPTION_PRICE, SUBSCRIPTION_DESCRIPTION
            )
            await db.create_payment_record(invoice_id, callback.from_user.id, SUBSCRIPTION_PRICE, provider)
            await callback.message.answer(
                "Оплати через CryptoBot по кнопке ниже.",
                reply_markup=kb.pay_button(pay_url),
            )
            payment_id = invoice_id

        elif provider == "xrocket":
            invoice_id, pay_url = await xrocket_payments.create_invoice(
                SUBSCRIPTION_PRICE, SUBSCRIPTION_DESCRIPTION
            )
            await db.create_payment_record(invoice_id, callback.from_user.id, SUBSCRIPTION_PRICE, provider)
            await callback.message.answer(
                "Оплати через xRocket по кнопке ниже.",
                reply_markup=kb.pay_button(pay_url),
            )
            payment_id = invoice_id

        else:
            return
    except RuntimeError as e:
        # соответствующий провайдер ещё не подключён — см. payments.py /
        # cryptobot_payments.py / xrocket_payments.py
        await callback.message.answer(str(e))
        return

    asyncio.create_task(
        _poll_payment_and_grant(callback.bot, provider, payment_id, callback.from_user.id)
    )


def _qr_image(payload: str) -> BufferedInputFile:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return BufferedInputFile(buf.read(), filename="sbp_qr.png")


async def _poll_payment_and_grant(bot: Bot, provider: str, payment_id: str, user_id: int) -> None:
    """
    Пока не настроены вебхуки — проверяем статус оплаты поллингом (раз в
    PAYMENT_POLL_INTERVAL_SEC, до PAYMENT_POLL_TIMEOUT_SEC), и как только
    видим "оплачено", сразу выдаём инвайт в приватку. Админ также может
    форсировать проверку командой /check_payment.
    """
    elapsed = 0
    while elapsed < PAYMENT_POLL_TIMEOUT_SEC:
        await asyncio.sleep(PAYMENT_POLL_INTERVAL_SEC)
        elapsed += PAYMENT_POLL_INTERVAL_SEC
        try:
            status = await check_status(provider, payment_id)
        except RuntimeError:
            return  # провайдер ещё не подключён — нечего поллить
        except Exception:
            logger.exception("Ошибка проверки статуса платежа %s (%s)", payment_id, provider)
            continue

        if status == PAID_STATUS[provider]:
            await db.update_payment_status(payment_id, status)
            await access.grant_access(bot, user_id, payment_id)
            return
        if status in DEAD_STATUSES[provider]:
            await db.update_payment_status(payment_id, status)
            return
