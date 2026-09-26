from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import WHAT_INSIDE_URL, REVIEWS_URL, QUESTION_URL


def main_menu(is_open: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_open:
        builder.row(
            InlineKeyboardButton(text="войти в приватку", callback_data="enter_private")
        )
    builder.row(InlineKeyboardButton(text="что внутри?", url=WHAT_INSIDE_URL))
    builder.row(InlineKeyboardButton(text="отзывы", url=REVIEWS_URL))
    builder.row(InlineKeyboardButton(text="задать вопрос", url=QUESTION_URL))
    return builder.as_markup()


def method_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="СБП",
            callback_data="pay_method:sbp",
            icon_custom_emoji_id="5190779568503412204",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="CryptoBot",
            callback_data="pay_method:cryptobot",
            icon_custom_emoji_id="5361914370068613491",
        )
    )
    return builder.as_markup()


def pay_button(confirmation_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Оплатить", url=confirmation_url))
    return builder.as_markup()


def pay_button_with_confirm(pay_url: str, provider: str, payment_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Оплатить", url=pay_url))
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"cp:{provider}:{payment_id}")
    )
    return builder.as_markup()


def admin_menu(is_open: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_open:
        builder.row(
            InlineKeyboardButton(text="🔴 Закрыть набор", callback_data="admin_close")
        )
    else:
        builder.row(
            InlineKeyboardButton(text="🟢 Открыть набор", callback_data="admin_open")
        )
    return builder.as_markup()
