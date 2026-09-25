import logging

from aiogram import Router, Bot, F
from aiogram.types import ChatJoinRequest

import database as db
from config import PRIVATE_CHANNEL_ID

logger = logging.getLogger(__name__)
router = Router()


@router.chat_join_request(F.chat.id == PRIVATE_CHANNEL_ID)
async def handle_join_request(request: ChatJoinRequest, bot: Bot) -> None:
    invite = request.invite_link
    link_str = invite.invite_link if invite else None

    record = await db.get_invite(link_str) if link_str else None

    # Ссылка не наша / уже использована / принадлежит другому пользователю — отклоняем.
    if not record or record["status"] != "issued" or record["user_id"] != request.from_user.id:
        await bot.decline_chat_join_request(request.chat.id, request.from_user.id)
        logger.info(
            "Отклонена заявка на вступление от %s (ссылка не подтверждена)",
            request.from_user.id,
        )
        return

    await bot.approve_chat_join_request(request.chat.id, request.from_user.id)
    await db.set_invite_status(link_str, "used")

    # ссылка одноразовая — сразу отзываем, чтобы никто больше не мог ей воспользоваться
    try:
        await bot.revoke_chat_invite_link(request.chat.id, link_str)
    except Exception:
        logger.exception("Не удалось отозвать инвайт-ссылку %s", link_str)

    await bot.send_message(request.from_user.id, "Заявка одобрена, добро пожаловать в приватку 🎉")
