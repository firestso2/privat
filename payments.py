"""
Интеграция с ЮKassa.

Пока магазин не прошёл проверку и не выдан секретный ключ, YOOKASSA_SHOP_ID /
YOOKASSA_SECRET_KEY в .env пустые — в этом случае create_payment() работает в
заглушечном режиме и просто бросает RuntimeError с понятным сообщением, чтобы
не уронить бота. Как только реквизиты будут получены — просто впишите их в
.env, ничего в коде менять не нужно.
"""
import uuid

from config import (
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_RETURN_URL,
)

_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError(
            "ЮKassa ещё не подключена: заполните YOOKASSA_SHOP_ID и "
            "YOOKASSA_SECRET_KEY в .env после одобрения аккаунта."
        )
    from yookassa import Configuration

    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY
    _configured = True


def create_payment(user_id: int, amount: float, description: str) -> tuple[str, str]:
    """
    Создаёт обычный платёж в ЮKassa (карта, экран выбора способа оплаты).
    Возвращает (payment_id, confirmation_url) — ссылку, на которую нужно
    отправить пользователя для оплаты.
    """
    _ensure_configured()
    from yookassa import Payment

    idempotence_key = str(uuid.uuid4())
    payment = Payment.create(
        {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": YOOKASSA_RETURN_URL,
            },
            "capture": True,
            "description": description,
            "metadata": {"user_id": user_id},
        },
        idempotence_key,
    )
    return payment.id, payment.confirmation.confirmation_url


def create_sbp_payment(user_id: int, amount: float, description: str) -> tuple[str, str]:
    """
    Создаёт платёж в ЮKassa напрямую через СБП — без экрана выбора способа
    оплаты. Возвращает (payment_id, qr_payload); qr_payload — не ссылка, а
    данные, которые нужно самим отрисовать в QR-код (см. access/qrcode в
    handlers/user.py). Пользователь сканирует QR в приложении банка.
    """
    _ensure_configured()
    from yookassa import Payment

    idempotence_key = str(uuid.uuid4())
    payment = Payment.create(
        {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "payment_method_data": {"type": "sbp"},
            "confirmation": {"type": "qr"},
            "capture": True,
            "description": description,
            "metadata": {"user_id": user_id},
        },
        idempotence_key,
    )
    return payment.id, payment.confirmation.confirmation_data


def check_payment_status(payment_id: str) -> str:
    """Возвращает статус платежа: pending / waiting_for_capture / succeeded / canceled."""
    _ensure_configured()
    from yookassa import Payment

    payment = Payment.find_one(payment_id)
    return payment.status


def parse_webhook_event(data: dict) -> tuple[str, str] | None:
    """
    Разбирает событие вебхука ЮKassa.
    Возвращает (payment_id, status) либо None, если событие не про оплату.
    Подключается позже, когда будет настроен приём вебхуков (см. README).
    """
    event = data.get("event")
    obj = data.get("object", {})
    if event in ("payment.succeeded", "payment.canceled"):
        return obj.get("id"), obj.get("status")
    return None
