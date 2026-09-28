"""Full lifecycle: disbursement, on-time, partial, overdue recovery, payoff."""

from datetime import date
from decimal import Decimal

import pytest

from loan_engine.service import LoanService

D = date(2026, 1, 15)
PRINCIPAL = Decimal("1200.00")
RATE = Decimal("0.12")
TERM = 3
# installments: (400,12) due 02-15; (400,8) due 03-15; (400,4) due 04-15


@pytest.fixture
def svc():
    s = LoanService()
    s.originate("L1", PRINCIPAL, RATE, TERM, D, grace_days=3)
    return s


def test_on_time_installments_to_paid_off(svc, log):
    log.info("input: pay 412@02-15, 408@03-15, 404@04-15 on-time")
    r1 = svc.submit_payment("L1", "P1", Decimal("412.00"), date(2026, 2, 15))
    log.info("transition=%s alloc=%s", r1.after_status,
             [(l.period, str(l.to_penalty), str(l.to_interest), str(l.to_principal)) for l in r1.lines])
    assert r1.after_status == "NORMAL"
    assert svc.get_loan("L1").installments[0].is_paid

    r2 = svc.submit_payment("L1", "P2", Decimal("408.00"), date(2026, 3, 15))
    assert r2.after_status == "NORMAL"
    r3 = svc.submit_payment("L1", "P3", Decimal("404.00"), date(2026, 4, 15))
    log.info("final transition=%s settled=%s", r3.after_status, r3.settled)
    assert r3.after_status == "PAID_OFF" and r3.settled

    loan = svc.get_loan("L1")
    # conservation: paid principal equals disbursed principal
    assert loan.total_principal_paid == PRINCIPAL
    assert loan.total_interest_paid == Decimal("24.00")
    assert loan.total_due == 0


def test_partial_payment_waterfall_penalty_interest_principal(svc, log):
    # within grace: no penalty; 400 covers interest 12 then 388 principal
    r = svc.submit_payment("L1", "P1", Decimal("400.00"), date(2026, 2, 15))
    inst = svc.get_loan("L1").installments[0]
    log.info("basis: penalty=0, interest_due=12, principal_due=400; "
             "got interest_paid=%s principal_paid=%s", inst.interest_paid, inst.principal_paid)
    assert (inst.penalty_paid, inst.interest_paid, inst.principal_paid) == (
        Decimal("0.00"), Decimal("12.00"), Decimal("388.00"))
    assert not inst.is_paid
    assert r.settled is False


def test_overdue_recovery_after_late_full_payment(svc, log):
    # before paying, the account is overdue as of 02-25
    pre = svc.snapshot("L1", date(2026, 2, 25))
    assert pre["status"] == "OVERDUE"
    assert pre["penalty_balance"] == "1.40"

    # full payment clears penalty+interest+principal at once; resulting
    # status is NORMAL because remaining periods are not yet due
    r = svc.submit_payment("L1", "P1", Decimal("413.40"), date(2026, 2, 25))
    log.info("penalty basis: 400 * 0.0005 * 7 = 1.40; transition on payment %s->%s",
             r.before_status, r.after_status)
    assert r.before_status == "OPEN" and r.after_status == "NORMAL"
    loan = svc.get_loan("L1")
    assert loan.total_penalty_paid == Decimal("1.40")
    assert loan.installments[0].is_paid

    # remaining installments not yet due -> status recovers, never stuck OVERDUE
    snap = svc.snapshot("L1", date(2026, 2, 25))
    assert snap["status"] == "NORMAL"

    # late full lifecycle continues deterministically
    r2 = svc.submit_payment("L1", "P2", Decimal("408.00"), date(2026, 3, 15))
    assert r2.after_status == "NORMAL"
    r3 = svc.submit_payment("L1", "P3", Decimal("404.00"), date(2026, 4, 15))
    assert r3.after_status == "PAID_OFF"


def test_early_settlement_waives_future_interest(svc, log):
    # settle on 02-01 before any due date: pay principal only, all interest waived
    r = svc.submit_payment("L1", "P1", Decimal("1200.00"), date(2026, 2, 1),
                          kind="EARLY_SETTLE")
    waived = sum((l.interest_waived for l in r.lines), Decimal("0.00"))
    log.info("early settle: quote=1200.00, future interest waived=%s", waived)
    assert r.settled and r.after_status == "PAID_OFF"
    assert waived == Decimal("24.00")
    assert svc.get_loan("L1").total_interest_paid == 0
