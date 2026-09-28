"""Payment validation and waterfall allocation (pure, replayable).

Allocation rules
----------------
* Installments are consumed in period order (earliest due first).
* Inside one installment the waterfall is
  **accrued penalty -> interest -> principal**.
* A NORMAL payment can only cover amounts already due/billed: accrued
  penalty plus principal & interest of installments due on/before
  ``value_date``. Paying more than collectible is rejected (no
  unallocated cash) with code ``PAYMENT_EXCEEDS_BALANCE``; an exact
  payoff settles the loan.
* An EARLY_SETTLE payment must equal :func:`early_settle_quote` exactly;
  interest of installments due after ``value_date`` is waived.
* Penalty is always accrued to ``value_date`` first, so late posting
  produces the same numbers as on-time daily accrual.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .errors import (
    EventOrderError,
    InvalidStateError,
    PaymentAmountError,
    PaymentExceedsBalanceError,
)
from .money import ZERO, cap, q2
from .models import AllocationLine, LedgerEvent, Loan, LoanStatus, PaymentResult
from .overdue import accrue_penalties, evaluate_status

# Allocation waterfall inside an installment: penalty -> interest -> principal.
# Installments are consumed from earliest due date.
PAYMENT_KIND_NORMAL = "NORMAL"
PAYMENT_KIND_EARLY_SETTLE = "EARLY_SETTLE"

_PAYMENT_KINDS = frozenset({PAYMENT_KIND_NORMAL, PAYMENT_KIND_EARLY_SETTLE})


def billed_due(loan: Loan, as_of: date) -> Decimal:
    """Amount collectible by a normal payment (accrued penalty + billed periods)."""
    total = loan.penalty_balance
    for inst in loan.installments:
        if inst.due_date <= as_of:
            total += inst.principal_balance + inst.interest_balance
    return q2(total)


def early_settle_quote(loan: Loan, as_of: date) -> Decimal:
    """Payoff amount today: outstanding principal + billed interest + penalty.

    Interest on installments due after ``as_of`` is waived.
    """
    total = loan.penalty_balance
    for inst in loan.installments:
        total += inst.principal_balance
        if inst.due_date <= as_of:
            total += inst.interest_balance
    return q2(total)


def _validate(loan: Loan, amount: Decimal, value_date: date, kind: str) -> None:
    if kind not in _PAYMENT_KINDS:
        raise PaymentAmountError(f"unknown payment kind: {kind}")
    if loan.status in (LoanStatus.PAID_OFF, LoanStatus.CLOSED):
        raise InvalidStateError(
            f"loan is {loan.status.value}; no payment accepted",
            code="LOAN_ALREADY_SETTLED",
        )
    if amount is None or amount <= ZERO:
        raise PaymentAmountError("payment amount must be positive")
    # value dates of applied events must be non-decreasing
    if loan.last_event_date is not None and value_date < loan.last_event_date:
        raise EventOrderError(
            f"event value_date {value_date} is earlier than last applied "
            f"event date {loan.last_event_date}"
        )


def _waterfall(
    loan: Loan,
    pending: Decimal,
    value_date: date,
    *,
    early_settle: bool,
) -> tuple[list[AllocationLine], Decimal, Decimal]:
    """Distribute ``pending`` across installments.

    Returns (lines, total_waived_interest, leftover).
    """
    lines: list[AllocationLine] = []
    waived_total = ZERO

    for inst in loan.installments:
        line = AllocationLine(period=inst.period)
        billed = inst.due_date <= value_date

        # 1) accrued penalty first
        to_penalty = cap(pending, inst.penalty_accrued)
        inst.penalty_accrued -= to_penalty
        inst.penalty_paid += to_penalty
        loan.total_penalty_paid += to_penalty
        pending -= to_penalty
        line.to_penalty = to_penalty

        # interest component
        if billed:
            to_interest = cap(pending, inst.interest_balance)
        elif early_settle:
            # future installment: interest is waived, never charged
            to_interest = ZERO
            line.interest_waived = inst.interest_balance
            waived_total += inst.interest_balance
            inst.interest_due = inst.interest_paid  # write off future interest
        else:
            to_interest = ZERO
        if to_interest:
            inst.interest_paid += to_interest
            loan.total_interest_paid += to_interest
            pending -= to_interest
            line.to_interest = to_interest

        # principal component: future installments only reachable on early settle
        if billed or early_settle:
            to_principal = cap(pending, inst.principal_balance)
            if to_principal:
                inst.principal_paid += to_principal
                loan.total_principal_paid += to_principal
                pending -= to_principal
                line.to_principal = to_principal

        if inst.is_paid and inst.paid_date is None:
            inst.paid_date = value_date

        # keep a line even if empty, except skip empty lines of future
        # installments in a normal payment (they were never touched)
        if (
            line.to_penalty
            or line.to_interest
            or line.to_principal
            or line.interest_waived
        ):
            lines.append(line)

    return lines, waived_total, pending


def apply_payment(
    loan: Loan,
    payment_id: str,
    amount: Decimal,
    value_date: date,
    kind: str = PAYMENT_KIND_NORMAL,
) -> PaymentResult:
    """Validate, accrue, allocate and record one payment on ``loan``.

    Mutates the passed loan and appends an immutable ledger event. Raises a
    :class:`~loan_engine.errors.LoanError` subclass on any rule violation;
    no partial mutation survives a validation failure.
    """
    amount = q2(amount)
    _validate(loan, amount, value_date, kind)

    before_status = loan.status
    p_before, i_before, f_before = (
        loan.principal_balance,
        loan.interest_balance,
        loan.penalty_balance,
    )

    # 1) bring penalty accrual to the payment's value date (idempotent)
    accrue_penalties(loan, value_date)

    # 2) determine collectible amount and reject excess / mismatch
    if kind == PAYMENT_KIND_EARLY_SETTLE:
        required = early_settle_quote(loan, value_date)
        if loan.total_due <= ZERO:
            raise InvalidStateError(
                "nothing to settle; loan balance is zero",
                code="LOAN_ALREADY_SETTLED",
            )
        if amount != required:
            raise PaymentExceedsBalanceError(
                f"early settle requires exactly {required}, got {amount}",
                code="EARLY_SETTLE_AMOUNT_MISMATCH",
            )
    else:
        required = billed_due(loan, value_date)
        if required <= ZERO:
            raise InvalidStateError(
                "no billed amount due on this date", code="NOTHING_DUE"
            )
        if amount > required:
            raise PaymentExceedsBalanceError(
                f"payment {amount} exceeds collectible {required} on {value_date}; "
                "overpayment is rejected (use EARLY_SETTLE for full prepayment)"
            )

    # 3) waterfall allocation
    lines, waived, leftover = _waterfall(
        loan, amount, value_date, early_settle=(kind == PAYMENT_KIND_EARLY_SETTLE)
    )

    # exact-amount checks guard the conservation invariant
    if leftover != ZERO:
        raise PaymentExceedsBalanceError(
            f"unallocated remainder {leftover}; allocation invariant violated"
        )
    settled = loan.total_due <= ZERO and all(i.is_paid for i in loan.installments)

    # 4) re-evaluate status (OVERDUE can recover to NORMAL; payoff is terminal)
    after_status = evaluate_status(loan, value_date)

    loan.last_event_date = value_date
    event = LedgerEvent(
        event_id=payment_id,
        loan_id=loan.loan_id,
        event_type="PAYMENT",
        value_date=value_date,
        amount=amount,
        detail={
            "kind": kind,
            "lines": [
                {
                    "period": l.period,
                    "penalty": str(l.to_penalty),
                    "interest": str(l.to_interest),
                    "principal": str(l.to_principal),
                    "interest_waived": str(l.interest_waived),
                }
                for l in lines
            ],
            "interest_waived_total": str(waived),
            "before_status": before_status.value,
        },
        principal_balance=loan.principal_balance,
        interest_balance=loan.interest_balance,
        penalty_balance=loan.penalty_balance,
        status=after_status.value,
    )
    loan.events.append(event)

    return PaymentResult(
        payment_id=payment_id,
        loan_id=loan.loan_id,
        kind=kind,
        value_date=value_date,
        requested_amount=amount,
        lines=lines,
        before_status=before_status.value,
        after_status=after_status.value,
        principal_before=p_before,
        interest_before=i_before,
        penalty_before=f_before,
        principal_after=loan.principal_balance,
        interest_after=loan.interest_balance,
        penalty_after=loan.penalty_balance,
        settled=settled,
    )
