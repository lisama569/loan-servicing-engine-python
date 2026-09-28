"""Core domain models: loans, installments, ledger events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from .errors import LoanError  # noqa: F401  (re-export convenience)

ZERO = Decimal("0.00")


class LoanStatus(str, Enum):
    OPEN = "OPEN"                 # originated, not yet due
    NORMAL = "NORMAL"             # repayments on track
    OVERDUE = "OVERDUE"           # past due beyond grace period
    PAID_OFF = "PAID_OFF"         # fully settled
    CLOSED = "CLOSED"             # closed / written off (terminal)


@dataclass
class Installment:
    """A scheduled repayment installment with per-component running balances."""

    period: int
    due_date: date
    principal_due: Decimal
    interest_due: Decimal

    principal_paid: Decimal = ZERO
    interest_paid: Decimal = ZERO
    penalty_paid: Decimal = ZERO          # late fees actually paid
    penalty_accrued: Decimal = ZERO       # accrued but not yet paid
    accrued_until: date | None = None     # last accrual value date
    paid_date: date | None = None         # fully settled date

    @property
    def principal_balance(self) -> Decimal:
        return self.principal_due - self.principal_paid

    @property
    def interest_balance(self) -> Decimal:
        return self.interest_due - self.interest_paid

    @property
    def is_paid(self) -> bool:
        return (
            self.principal_balance <= ZERO
            and self.interest_balance <= ZERO
            and self.penalty_accrued <= ZERO
        )


@dataclass
class LedgerEvent:
    """An immutable record of an applied business event."""

    event_id: str
    loan_id: str
    event_type: str            # DISBURSEMENT | PAYMENT | ACCRUAL
    value_date: date
    amount: Decimal = ZERO
    detail: dict = field(default_factory=dict)
    # resulting balances snapshot, for traceability
    principal_balance: Decimal = ZERO
    interest_balance: Decimal = ZERO
    penalty_balance: Decimal = ZERO
    status: str = ""


@dataclass
class Loan:
    loan_id: str
    principal: Decimal
    annual_rate: Decimal              # e.g. 0.12
    term_months: int
    disbursement_date: date
    grace_days: int = 3
    installments: list[Installment] = field(default_factory=list)
    status: LoanStatus = LoanStatus.OPEN
    # bookkeeping
    total_principal_paid: Decimal = ZERO
    total_interest_paid: Decimal = ZERO
    total_penalty_paid: Decimal = ZERO
    # monotonic clock for value-date ordering of applied events
    last_event_date: date | None = None
    events: list[LedgerEvent] = field(default_factory=list)

    # ----- derived balances -----
    @property
    def principal_balance(self) -> Decimal:
        return self.principal - self.total_principal_paid

    @property
    def interest_balance(self) -> Decimal:
        return sum(
            (i.interest_balance for i in self.installments), ZERO
        )

    @property
    def penalty_balance(self) -> Decimal:
        return sum(
            (i.penalty_accrued for i in self.installments), ZERO
        )

    @property
    def total_due(self) -> Decimal:
        return self.principal_balance + self.interest_balance + self.penalty_balance


@dataclass
class AllocationLine:
    """How one payment is split inside one installment."""

    period: int
    to_penalty: Decimal = ZERO
    to_interest: Decimal = ZERO
    to_principal: Decimal = ZERO
    interest_waived: Decimal = ZERO


@dataclass
class PaymentResult:
    """Full traceable outcome of a payment application."""

    payment_id: str
    loan_id: str
    kind: str                       # NORMAL | EARLY_SETTLE
    value_date: date
    requested_amount: Decimal
    lines: list[AllocationLine] = field(default_factory=list)
    before_status: str = ""
    after_status: str = ""
    principal_before: Decimal = ZERO
    interest_before: Decimal = ZERO
    penalty_before: Decimal = ZERO
    principal_after: Decimal = ZERO
    interest_after: Decimal = ZERO
    penalty_after: Decimal = ZERO
    settled: bool = False
    duplicate: bool = False

    @property
    def applied_amount(self) -> Decimal:
        return sum(
            (l.to_penalty + l.to_interest + l.to_principal for l in self.lines),
            ZERO,
        )
