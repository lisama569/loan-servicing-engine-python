"""Domain error types with distinguishable failure reasons."""

from __future__ import annotations


class LoanError(Exception):
    """Base domain error carrying a stable error code."""

    code: str = "DOMAIN_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class LoanNotFoundError(LoanError):
    code = "LOAN_NOT_FOUND"


class InvalidStateError(LoanError):
    code = "INVALID_STATE"


class PaymentExceedsBalanceError(LoanError):
    code = "PAYMENT_EXCEEDS_BALANCE"


class PaymentAmountError(LoanError):
    code = "PAYMENT_AMOUNT_INVALID"


class EventOrderError(LoanError):
    code = "EVENT_DATE_OUT_OF_ORDER"


class IdempotencyConflictError(LoanError):
    code = "IDEMPOTENCY_CONFLICT"


class LoanAlreadyExistsError(LoanError):
    code = "LOAN_ALREADY_EXISTS"
