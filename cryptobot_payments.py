"""
Интеграция с CryptoBot (Crypto Pay API, https://help.crypt.bot/crypto-pay-api).

Токен выдаётся в @CryptoBot: команда /pay -> Create App -> API Token.
Пока CRYPTOBOT_API_TOKEN пуст в .env, функции бросают RuntimeError с понятным
сообщением, чтобы не уронить бота.
"""
import aiohttp

from config import CRYPTOBOT_API_TOKEN, CRYPTOBOT_ACCEPTED_ASSETS

API_BASE = "https://pay.crypt.bot/api"


def _ensure_configured() -> None:
    if not CRYPTOBOT_API_TOKEN:
        raise RuntimeError(
            "CryptoBot ещё не подключён: заполните CRYPTOBOT_API_TOKEN в .env "
            "(получить можно в @CryptoBot -> /pay -> Create App -> API Token)."
        )


async def _call(method: str, **params) -> dict:
    _ensure_configured()
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API_BASE}/{method}", json=params, headers=headers) as resp:
            data = await resp.json()

    if not data.get("ok"):
        raise RuntimeError(f"CryptoBot API вернул ошибку: {data.get('error')}")
    return data["result"]


async def create_invoice(amount_rub: float, description: str) -> tuple[str, str]:
    """
    Создаёт инвойс на сумму в рублях (пользователь платит в любом из
    accepted_assets по текущему курсу). Возвращает (invoice_id, pay_url).
    """
    result = await _call(
        "createInvoice",
        currency_type="fiat",
        fiat="RUB",
        accepted_assets=CRYPTOBOT_ACCEPTED_ASSETS,
        amount=f"{amount_rub:.2f}",
        description=description,
    )
    invoice_id = str(result["invoice_id"])
    pay_url = (
        result.get("bot_invoice_url")
        or result.get("pay_url")
        or result.get("mini_app_invoice_url")
        or result.get("web_app_invoice_url")
    )
    return invoice_id, pay_url


async def check_invoice_status(invoice_id: str) -> str:
    """Статус инвойса: active / paid / expired."""
    result = await _call("getInvoices", invoice_ids=invoice_id)
    items = result.get("items") or []
    if not items:
        return "unknown"
    return items[0]["status"]
