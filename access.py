"""
Выдача доступа в приватку после оплаты.

Логика:
1. Создаём инвайт-ссылку с creates_join_request=True — по ней нельзя войти
   сразу, только отправить заявку на вступление.
2. Отправляем ссылку оплатившему пользователю.
3. Когда он жмёт на ссылку и отправляет заявку, Telegram шлёт боту апдейт
   chat_join_request (см. handlers/join_request.py) — там сверяем, что
   заявку прислал именно тот, кто оплатил, и именно по этой ссылке.
4. Одобряем заявку и сразу отзываем (revoke) ссылку — второй раз ей
   воспользоваться нельзя.

Бот должен быть добавлен в канал/группу приватки администратором с правом
приглашать пользователей (can_invite_users), иначе Telegram вернёт ошибку
прав при создании ссылки или одобрении заявки.
"""
import logging
import time

from aiogram import Bot

import database as db
from config import PRIVATE_CHANNEL_ID, INVITE_LINK_TTL_HOURS

logger = logging.getLogger(__name__)


async def grant_access(bot: Bot, user_id: int, payment_id: str) -> str | None:
    """Создаёт одноразовую ссылку-заявку и отправляет её оплатившему пользователю."""
    if not PRIVATE_CHANNEL_ID:
        logger.error("PRIVATE_CHANNEL_ID не задан в .env — не могу выдать доступ")
        await bot.send_message(
            user_id,
            "Оплата прошла, но доступ пока не настроен админом. Напиши в поддержку.",
        )
        return None

    if await db.has_granted_invite(payment_id):
        return None  # доступ по этой оплате уже выдавали, не дублируем

    expire_date = int(time.time()) + INVITE_LINK_TTL_HOURS * 3600
    link = await bot.create_chat_invite_link(
        chat_id=PRIVATE_CHANNEL_ID,
        name=f"pay:{payment_id}"[:32],
        creates_join_request=True,
        expire_date=expire_date,
    )

    await db.create_invite_record(link.invite_link, payment_id, user_id)

    await bot.send_message(
        user_id,
        "Оплата подтверждена ✅\n\n"
        f"Вот твоя ссылка на вступление в приватку (одноразовая, действует {INVITE_LINK_TTL_HOURS} ч):\n"
        f"{link.invite_link}\n\n"
        "Перейди по ней и отправь заявку на вступление — я одобрю её автоматически.",
    )
    return link.invite_link
