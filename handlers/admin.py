from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

import database as db
import keyboards as kb
import access
from payment_dispatch import PAID_STATUS, check_status
from config import ADMIN_IDS

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("emoji_id"))
async def cmd_emoji_id(message: Message) -> None:
    """
    Достаёт emoji-id премиум-эмодзи. Пришли команду реплаем на сообщение с
    премиум-эмодзи, или вставь эмодзи прямо после команды в этом же
    сообщении — сработает и так, и так.
    """
    if not _is_admin(message.from_user.id):
        return

    target = message.reply_to_message or message
    text = target.text or target.caption or ""
    entities = target.entities or target.caption_entities or []
    custom = [e for e in entities if e.type == "custom_emoji"]

    if not custom:
        await message.answer(
            "Не нашёл премиум-эмодзи. Пришли /emoji_id реплаем на сообщение с "
            "нужным эмодзи, либо вставь эмодзи прямо после команды в этом же сообщении."
        )
        return

    lines = ["Найденные emoji-id:"]
    for e in custom:
        emoji_char = e.extract_from(text)
        lines.append(f"{emoji_char} → `{e.custom_emoji_id}`")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """Публичная команда — показывает Telegram ID, чтобы свериться с ADMIN_IDS в .env."""
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")


def _mask(value: str) -> str:
    if not value:
        return "❌ не задано"
    if len(value) <= 6:
        return "✅ задано (слишком короткое, чтобы замаскировать)"
    return f"✅ задано ({value[:4]}...{value[-4:]}, длина {len(value)})"


@router.message(Command("debug_env"))
async def cmd_debug_env(message: Message) -> None:
    """
    Только для админа: показывает, какие переменные окружения реально видит
    процесс бота (значения маскируются). Нужна, чтобы проверить, дошли ли
    переменные, заданные отдельно в панели хостинга, до самого бота.
    """
    if not _is_admin(message.from_user.id):
        return

    import config as cfg

    lines = [
        "Что видит бот в переменных окружения (значения замаскированы):",
        "",
        f"BOT_TOKEN: {_mask(cfg.BOT_TOKEN)}",
        f"ADMIN_IDS: {'✅ ' + str(cfg.ADMIN_IDS) if cfg.ADMIN_IDS else '❌ пусто'}",
        f"PRIVATE_CHANNEL_ID: {'✅ ' + str(cfg.PRIVATE_CHANNEL_ID) if cfg.PRIVATE_CHANNEL_ID else '❌ не задан (0)'}",
        f"YOOKASSA_SHOP_ID: {_mask(cfg.YOOKASSA_SHOP_ID)}",
        f"YOOKASSA_SECRET_KEY: {_mask(cfg.YOOKASSA_SECRET_KEY)}",
        f"CRYPTOBOT_API_TOKEN: {_mask(cfg.CRYPTOBOT_API_TOKEN)}",
        f"XROCKET_API_TOKEN: {_mask(cfg.XROCKET_API_TOKEN)}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    is_open = await db.is_enrollment_open()
    status = "🟢 открыт" if is_open else "🔴 закрыт"
    await message.answer(
        f"Админ-панель\nНабор сейчас: {status}",
        reply_markup=kb.admin_menu(is_open),
    )


@router.callback_query(F.data.in_({"admin_open", "admin_close"}))
async def toggle_enrollment(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    new_state = callback.data == "admin_open"
    await db.set_enrollment_open(new_state)
    status = "🟢 открыт" if new_state else "🔴 закрыт"
    await callback.message.edit_text(
        f"Админ-панель\nНабор сейчас: {status}",
        reply_markup=kb.admin_menu(new_state),
    )
    await callback.answer("Готово")


@router.message(Command("set_open_photo"))
async def set_open_photo(message: Message) -> None:
    """Ответь этой командой на фото — оно станет картинкой для открытого набора."""
    if not _is_admin(message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.answer("Ответь этой командой на сообщение с фото.")
        return
    file_id = message.reply_to_message.photo[-1].file_id
    await db.set_open_photo(file_id)
    await message.answer("Фото для открытого набора обновлено.")


@router.message(Command("set_closed_photo"))
async def set_closed_photo(message: Message) -> None:
    """Ответь этой командой на фото — оно станет картинкой для закрытого набора."""
    if not _is_admin(message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.answer("Ответь этой командой на сообщение с фото.")
        return
    file_id = message.reply_to_message.photo[-1].file_id
    await db.set_closed_photo(file_id)
    await message.answer("Фото для закрытого набора обновлено.")


@router.message(Command("check_payment"))
async def check_payment(message: Message) -> None:
    """/check_payment <payment_id> — ручная проверка статуса, пока нет вебхуков."""
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /check_payment <payment_id>")
        return
    payment_id = parts[1].strip()

    payment = await db.get_payment(payment_id)
    if not payment:
        await message.answer("Платёж с таким ID не найден в базе.")
        return
    provider = payment["provider"]

    try:
        status = await check_status(provider, payment_id)
    except RuntimeError as e:
        await message.answer(str(e))
        return

    await db.update_payment_status(payment_id, status)
    await message.answer(f"Провайдер: {provider}\nСтатус платежа: {status}")

    if status == PAID_STATUS[provider]:
        link = await access.grant_access(message.bot, payment["user_id"], payment_id)
        if link:
            await message.answer("Инвайт-ссылка выдана пользователю.")
