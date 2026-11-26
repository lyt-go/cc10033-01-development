"""Import compatibility alias for attempt identity service."""
from app.services.attempt_receipt import (
    AttemptReceiptService,
    AttemptService,
    attempt_receipt_service,
    attempt_service,
)

__all__ = [
    "AttemptReceiptService",
    "AttemptService",
    "attempt_receipt_service",
    "attempt_service",
]
