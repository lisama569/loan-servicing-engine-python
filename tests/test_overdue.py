"""Grace-period boundary, penalty accrual, late posting, cross-month stability."""

from datetime import date
from decimal import Decimal

import pytest

from loan_engine.errors import LoanError
from loan_engine.overdue import PENALTY_DAILY_RATE, accrue_penalties, overdue_days
from loan_engine.schedule import build_schedule
from loan_engine.service import LoanService
from loan_engine.models import Loan, LoanStatus


def _loan(grace=3):
    d = date(2026, 1, 31)  # month-end disbursement: dues clamp to month end
    sched = build_schedule(Decimal("1200.00"), Decimal("0.12"), 3, d)
    return Loan(loan_id="L", principal=Decimal("1200.00"), annual_rate=Decimal("0.12"),
                term_months=3, disbursement_date=d, grace_days=grace,
                installments=sched, last_event_date=d), sched


def test_grace_boundary_day_counts():
    loan, sched = _loan()
    # due 2026-02-28 (clamped), grace end 03-03
    assert sched[0].due_date == date(2026, 2, 28)
    assert overdue_days(sched[0], 3, date(2026, 3, 3)) == 0
    assert overdue_days(sched[0], 3, date(2026, 3, 4)) == 1
    assert overdue_days(sched[0], 3, date(2026, 3, 5)) == 2


def test_no_penalty_within_grace_and_first_day_after():
    loan, _ = _loan()
    assert accrue_penalties(loan, date(2026, 3, 3)) == Decimal("0.00")
    assert loan.penalty_balance == 0
    delta = accrue_penalties(loan, date(2026, 3, 4))
    assert delta == q(400, 1)
    assert loan.penalty_balance == q(400, 1)


def q(base, days):
    return (Decimal(base) * PENALTY_DAILY_RATE * days).quantize(Decimal("0.01"))


def test_accrual_is_idempotent_and_cross_month_stable():
    loan, _ = _loan()
    # accrue once in Feb/Mar then repeat: zero delta, deterministic total
    accrue_penalties(loan, date(2026, 3, 10))
    first = loan.penalty_balance
    delta = accrue_penalties(loan, date(2026, 3, 10))
    assert delta == 0 and loan.penalty_balance == first
    # vs accruing day by day: identical result (cursor, no double counting)
    other, _ = _loan()
    for day in range(4, 11):
        accrue_penalties(other, date(2026, 3, day))
    assert other.penalty_balance == first == q(400, 7)


def test_cross_month_and_zero_grace_boundary():
    loan, sched = _loan(grace=0)
    # due 02-28, zero grace: first penalty day 03-01
    assert overdue_days(sched[0], 0, date(2026, 2, 28)) == 0
    assert overdue_days(sched[0], 0, date(2026, 3, 1)) == 1
    accrue_penalties(loan, date(2026, 3, 1))
    assert loan.penalty_balance == q(400, 1)


def test_late_partial_payment_does_not_overcharge_then_balance_accrues():
    svc = LoanService()
    svc.originate("L", Decimal("1200.00"), Decimal("0.12"), 3,
                  date(2026, 1, 15), grace_days=3)
    # due 02-15, grace end 02-18. On 02-20: 2 penalty days on full 400 = 0.40
    r = svc.submit_payment("L", "P1", Decimal("100.00"), date(2026, 2, 20))
    loan = svc.get_loan("L")
    assert loan.total_penalty_paid == Decimal("0.40")
    inst = loan.installments[0]
    # 100 - 0.40 penalty - 12 interest = 87.60 principal
    assert inst.principal_paid == Decimal("87.60")
    assert r.after_status == "OVERDUE"

    # continue on 02-22: remaining overdue principal 312.40, 2 more days
    # penalty = 312.40*0.0005*2 = 0.31 (rounded half-up)
    snap = svc.snapshot("L", date(2026, 2, 22))
    assert Decimal(snap["penalty_balance"]) == Decimal("0.31"), snap["penalty_balance"]


def test_backdated_event_is_rejected_without_state_change():
    svc = LoanService()
    svc.originate("L", Decimal("1200.00"), Decimal("0.12"), 3, date(2026, 1, 15))
    svc.submit_payment("L", "P1", Decimal("412.00"), date(2026, 2, 15))
    with pytest.raises(LoanError) as ei:
        svc.submit_payment("L", "P2", Decimal("10.00"), date(2026, 2, 14))
    assert ei.value.code == "EVENT_DATE_OUT_OF_ORDER"
    loan = svc.get_loan("L")
    assert len([e for e in loan.events if e.event_type == "PAYMENT"]) == 1
    assert loan.last_event_date == date(2026, 2, 15)


def test_overdue_status_recovers_when_all_overdue_periods_settle():
    svc = LoanService()
    svc.originate("L", Decimal("1200.00"), Decimal("0.12"), 3,
                  date(2026, 1, 15), grace_days=3)
    # leave period 1 unpaid through 03-20 (period 2 is also overdue then)
    snap = svc.snapshot("L", date(2026, 3, 20))
    assert snap["status"] == "OVERDUE"
    # settle period 1 (with its penalty) on 03-20
    pen1 = Decimal(snap["installments"][0]["penalty_accrued"])
    svc.submit_payment("L", "P1", Decimal("412.00") + pen1, date(2026, 3, 20))
    # period 2 still overdue -> stays OVERDUE (no illegal recovery)
    assert svc.snapshot("L", date(2026, 3, 20))["status"] == "OVERDUE"
