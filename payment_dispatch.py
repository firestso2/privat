"""Общий код, которым пользуются и handlers/user.py, и handlers/admin.py,
чтобы не дублировать список провайдеров оплаты в двух местах."""
import payments
import cryptobot_payments
import xrocket_payments

# статус, который считается "оплачено", у каждого провайдера свой
PAID_STATUS = {
    "card": "succeeded",
    "sbp": "succeeded",
    "cryptobot": "paid",
    "xrocket": "paid",
}
# статусы, при которых можно прекратить поллинг, не дождавшись оплаты
DEAD_STATUSES = {
    "card": {"canceled"},
    "sbp": {"canceled"},
    "cryptobot": {"expired"},
    "xrocket": {"expired"},
}


async def check_status(provider: str, payment_id: str) -> str:
    if provider in ("card", "sbp"):
        return payments.check_payment_status(payment_id)
    if provider == "cryptobot":
        return await cryptobot_payments.check_invoice_status(payment_id)
    if provider == "xrocket":
        return await xrocket_payments.check_invoice_status(payment_id)
    raise ValueError(f"Неизвестный провайдер: {provider}")
