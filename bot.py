import asyncio
import logging

import extras
import promo
import planner
import automation

from aiogram import Bot, Dispatcher

from config import BOT_TOKEN
import database as db
from handlers import user, admin, join_request


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not BOT_TOKEN:
        raise RuntimeError("Заполни BOT_TOKEN в .env")

    await db.init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    await extras.setup(dp, bot)
    await promo.setup(dp, bot)
    await planner.setup(dp, bot)
    await automation.setup(dp, bot)

    dp.include_router(admin.router)  # админ-роутер выше, чтобы команды не перехватывались юзерскими
    dp.include_router(user.router)
    dp.include_router(join_request.router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
