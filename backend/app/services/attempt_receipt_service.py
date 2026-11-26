"""Naming alias for callers that expect the ``_service`` module suffix."""
from app.services.attempt_receipt import (
    AttemptReceiptService,
    ReceiptService,
    receipt_service,
    attempt_receipt_service,
)

attempt_receipt_service_instance = attempt_receipt_service

__all__ = [
    "AttemptReceiptService",
    "ReceiptService",
    "attempt_receipt_service",
    "attempt_receipt_service_instance",
    "receipt_service",
]
