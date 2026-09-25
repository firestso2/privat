import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# --- Ссылки ---
WHAT_INSIDE_URL = "https://telegra.ph/CHto-vnutri-privatki-09-20"
REVIEWS_URL = "https://t.me/+mSXcTee7324yNjU0"
QUESTION_URL = "https://t.me/fabezo"

# --- Приватка ---
# ID канала/группы с приватным контентом. Бот должен быть в нём админом с
# правом приглашать пользователей по ссылке (can_invite_users), иначе не
# сможет ни создавать инвайт-ссылки, ни одобрять заявки на вступление.
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))
SUBSCRIPTION_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", "3499"))  # в рублях
SUBSCRIPTION_DESCRIPTION = "Доступ в приватку"
INVITE_LINK_TTL_HOURS = 24  # сколько живёт неиспользованная ссылка, если оплативший не кликнул
PAYMENT_POLL_INTERVAL_SEC = 15
PAYMENT_POLL_TIMEOUT_SEC = 900  # 15 минут — пока нет вебхука, статус проверяем поллингом

# Картинки по умолчанию для /start (file_id из Telegram). Админ может
# переопределить их командами /set_open_photo и /set_closed_photo.
DEFAULT_OPEN_PHOTO_ID = "AgACAgQAAxkBAAMCaq-pN5fIfEJBQMWKTNLtYBNmJrEAAqcOaxv1hYBRcWoTz8tHPQkBAAMCAAN5AAM9BA"
DEFAULT_CLOSED_PHOTO_ID = "AgACAgQAAxkBAAMDaq-p_Xrke8IaF4BmCqBCWFOi18IAAqsOaxv1hYBR2I6oQfzHlzMBAAMCAAN5AAM9BA"

# --- ЮKassa ---
# Заполняются после того, как аккаунт пройдёт проверку и будет одобрена комиссия.
# До этого момента ветка оплаты работает в "заглушечном" режиме — см. payments.py
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/")  # куда вернуть юзера после оплаты

# --- CryptoBot (Crypto Pay API, pay.crypt.bot) ---
CRYPTOBOT_API_TOKEN = os.getenv("CRYPTOBOT_API_TOKEN", "")
CRYPTOBOT_ACCEPTED_ASSETS = os.getenv("CRYPTOBOT_ACCEPTED_ASSETS", "USDT,TON,BTC")

# --- xRocket Pay API (pay.api.xrocket.exchange) ---
XROCKET_API_TOKEN = os.getenv("XROCKET_API_TOKEN", "")
XROCKET_ASSET = os.getenv("XROCKET_ASSET", "USDT")  # валюта инвойса; список доступных см. GET /api/v1/currencies

DB_PATH = os.getenv("DB_PATH", "bot.db")
