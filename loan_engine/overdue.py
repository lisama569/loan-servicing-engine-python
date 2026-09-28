"""Overdue determination and penalty accrual / recovery.

Timeline for an installment with due date D and grace G days::

    D            D+1 ... D+G (grace, no penalty)     D+G+1 (first penalty day) ...

* ``as_of <= D+G``            -> not overdue, zero penalty
* ``as_of  > D+G``            -> overdue; penalty days = as_of - (D+G)

Penalty accrues daily on the installment's *remaining principal* at
``PENALTY_DAILY_RATE``. Accrual uses an ``accrued_until`` cursor so
repeated calls for the same/earlier date never double count (idempotent).
Events are required in non-decreasing value-date order, so the remaining
principal seen by each forward accrual is the historically correct base.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .money import ZERO, q2
from .models import Installment, Loan, LoanStatus
from .statemachine import ensure_transition

PENALTY_DAILY_RATE = Decimal("0.0005")  # 0.05% per overdue day on overdue principal


def grace_end(inst: Installment, grace_days: int) -> date:
    """Last non-penalized day: due date + grace days."""
    return inst.due_date + timedelta(days=grace_days)


def overdue_days(inst: Installment, grace_days: int, as_of: date) -> int:
    """Number of penalty-bearing days elapsed up to ``as_of`` (>= 0)."""
    end = grace_end(inst, grace_days)
    return max(0, (as_of - end).days)


def _accrue_one(inst: Installment, grace_days: int, as_of: date) -> Decimal:
    """Advance one installment's accrual cursor to ``as_of``; return delta."""
    end = grace_end(inst, grace_days)
    cursor = inst.accrued_until if inst.accrued_until is not None else end

    # Idempotent / out-of-order guard: only forward movement counts.
    if as_of <= cursor:
        return ZERO

    if inst.principal_balance <= ZERO:
        # Principal settled: no further penalty base; just advance the cursor.
        inst.accrued_until = as_of
        return ZERO

    days = (as_of - cursor).days
    delta = q2(inst.principal_balance * PENALTY_DAILY_RATE * Decimal(days))
    inst.penalty_accrued += delta
    inst.accrued_until = as_of
    return delta


def is_overdue(loan: Loan, as_of: date) -> bool:
    """True if any unsettled installment's due date + grace has passed as_of."""
    for inst in loan.installments:
        if inst.is_paid:
            continue
        if overdue_days(inst, loan.grace_days, as_of) > 0:
            return True
    return False


def accrue_penalties(loan: Loan, as_of: date) -> Decimal:
    """Accrue daily penalty on every installment up to ``as_of``.

    Idempotent: the total delta of a repeated call for the same date is 0.
    Returns the penalty newly accrued by *this* call.
    """
    total_delta = ZERO
    for inst in loan.installments:
        total_delta += _accrue_one(inst, loan.grace_days, as_of)
    return total_delta


def evaluate_status(loan: Loan, as_of: date) -> LoanStatus:
    """Recompute status from balances and due dates, honoring the state machine.

    * PAID_OFF is terminal and is never reversed.
    * Zero total due with every installment settled -> PAID_OFF.
    * Any unpaid installment past due + grace -> OVERDUE.
    * Otherwise NORMAL once the first due date arrived or money moved;
      a freshly originated loan stays OPEN. NORMAL never regresses to OPEN.
    """
    if loan.status == LoanStatus.PAID_OFF:
        return LoanStatus.PAID_OFF

    if loan.total_due <= ZERO and all(i.is_paid for i in loan.installments):
        target = LoanStatus.PAID_OFF
    elif is_overdue(loan, as_of):
        target = LoanStatus.OVERDUE
    else:
        first_due = loan.installments[0].due_date
        any_money = loan.total_principal_paid > ZERO
        if as_of >= first_due or any_money:
            target = LoanStatus.NORMAL
        else:
            target = LoanStatus.OPEN

    ensure_transition(loan.status, target)
    loan.status = target
    return target
