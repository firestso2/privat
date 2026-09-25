"""
Интеграция с xRocket Pay API
(https://docs.xrocket.exchange/api/pay/pay-api-overview).

Токен приложения выдаётся в @xRocket: Pay API -> Create an app -> Settings ->
API Token (убедись, что у приложения выбран "API Version: Current" — это
новый Pay API, а не устаревший Legacy).

Используется официально задокументированный REST-эндпоинт напрямую (через
aiohttp), без сторонних непроверенных библиотек:
  POST /api/v1/invoices — создать инвойс
  GET  /api/v1/invoice  — получить статус по ID

Пока XROCKET_API_TOKEN пуст в .env, функции бросают RuntimeError, чтобы не
уронить бота.

Важно: перед первым реальным запуском стоит один раз проверить в Swagger
приложения (https://pay.api.xrocket.exchange/api/docs/), что поля запроса и
ответа ниже совпадают с актуальной версией API — на момент написания это
самый свежий публично задокументированный контракт, но у xRocket Pay API
недавно поменялась версия (Legacy -> Pay API), и мелкие детали могли
измениться уже после написания этого кода.
"""
import aiohttp

from config import XROCKET_API_TOKEN, XROCKET_ASSET

API_BASE = "https://pay.api.xrocket.exchange/api/v1"


def _ensure_configured() -> None:
    if not XROCKET_API_TOKEN:
        raise RuntimeError(
            "xRocket ещё не подключён: заполните XROCKET_API_TOKEN в .env "
            "(получить можно в @xRocket -> Pay API -> Create an app -> API Token)."
        )


def _headers() -> dict:
    return {"Authorization": f"Bearer {XROCKET_API_TOKEN}"}


async def create_invoice(amount: float, description: str) -> tuple[str, str]:
    """
    Создаёт инвойс в xRocket на сумму в валюте XROCKET_ASSET (по умолчанию
    USDT — сумма в рублях у тебя привязана к фиату, так что при желании
    можно завести отдельную цену в .env специально под xRocket).
    Возвращает (invoice_id, pay_url).
    """
    _ensure_configured()
    payload = {
        "amount": amount,
        "currency": XROCKET_ASSET,
        "description": description,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_BASE}/invoices", json=payload, headers=_headers()
        ) as resp:
            data = await resp.json()
            if resp.status >= 300:
                raise RuntimeError(f"xRocket API вернул ошибку: {data}")

    invoice_id = str(data.get("id") or data.get("invoiceId") or data.get("invoice_id"))
    pay_url = (
        data.get("link")
        or (data.get("links") or {}).get("webLink")
        or (data.get("links") or {}).get("web_link")
    )
    return invoice_id, pay_url


async def check_invoice_status(invoice_id: str) -> str:
    """Статус инвойса: active / paid / expired (в терминах API xRocket)."""
    _ensure_configured()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_BASE}/invoice", params={"id": invoice_id}, headers=_headers()
        ) as resp:
            data = await resp.json()
            if resp.status >= 300:
                raise RuntimeError(f"xRocket API вернул ошибку: {data}")

    return data.get("status", "unknown")
