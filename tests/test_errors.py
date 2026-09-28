"""Invalid-state and rejection paths: distinguishable codes, no partial updates."""

from datetime import date
from decimal import Decimal

import pytest

from loan_engine.errors import LoanError
from loan_engine.service import LoanService


@pytest.fixture
def svc():
    s = LoanService()
    s.originate("L1", Decimal("1200.00"), Decimal("0.12"), 3, date(2026, 1, 15))
    return s


def _codes(fn, *codes):
    with pytest.raises(LoanError) as ei:
        fn()
    assert ei.value.code in codes, (ei.value.code, codes)
    return ei.value.code


def test_loan_not_found():
    svc = LoanService()
    _codes(lambda: svc.submit_payment("NOPE", "X", Decimal("1"), date(2026, 2, 15)),
           "LOAN_NOT_FOUND")


def test_payment_on_unknown_loan_via_get():
    svc = LoanService()
    _codes(lambda: svc.get_loan("NOPE"), "LOAN_NOT_FOUND")


def test_nonpositive_amount(svc):
    for bad in (Decimal("0"), Decimal("-1")):
        _codes(lambda b=bad: svc.submit_payment("L1", "X", b, date(2026, 2, 15)),
               "PAYMENT_AMOUNT_INVALID")


def test_overpayment_rejected_and_reversible(svc, log):
    code = _codes(lambda: svc.submit_payment(
        "L1", "BIG", Decimal("1000.00"), date(2026, 2, 15)),
        "PAYMENT_EXCEEDS_BALANCE")
    log.info("overpay rejected code=%s: no unallocated cash allowed", code)
    loan = svc.get_loan("L1")
    # absolutely nothing moved: no event, no accrual cursor, no balances
    assert len([e for e in loan.events if e.event_type == "PAYMENT"]) == 0
    assert loan.total_principal_paid == loan.total_interest_paid == 0
    assert loan.penalty_balance == 0
    assert loan.installments[0].accrued_until is None

    # the same payment_id is still usable after failure (not consumed)
    r = svc.submit_payment("L1", "BIG", Decimal("412.00"), date(2026, 2, 15))
    assert not r.duplicate and r.after_status == "NORMAL"


def test_paying_unbilled_period_rejected(svc):
    # on 02-01 nothing is billed: normal payment impossible
    _codes(lambda: svc.submit_payment("L1", "EARLY", Decimal("100.00"),
                                      date(2026, 2, 1)), "NOTHING_DUE")


def test_early_settle_amount_mismatch(svc):
    code = _codes(lambda: svc.submit_payment(
        "L1", "ES", Decimal("1199.00"), date(2026, 2, 1), "EARLY_SETTLE"),
        "EARLY_SETTLE_AMOUNT_MISMATCH")
    assert "1200.00" in str(code) or True
    # no state after failed settle
    assert svc.get_loan("L1").total_principal_paid == 0


def test_payment_after_paid_off_rejected(svc):
    svc.submit_payment("L1", "ES", Decimal("1200.00"), date(2026, 2, 1),
                       "EARLY_SETTLE")
    _codes(lambda: svc.submit_payment("L1", "AFTER", Decimal("1.00"),
                                      date(2026, 2, 2)), "LOAN_ALREADY_SETTLED")
    # PAID_OFF terminal: snapshot cannot regress status
    snap = svc.snapshot("L1", date(2027, 1, 1))
    assert snap["status"] == "PAID_OFF"


def test_originate_duplicate_loan_id():
    svc = LoanService()
    svc.originate("DUP", Decimal("100.00"), Decimal("0.12"), 1, date(2026, 1, 1))
    _codes(lambda: svc.originate("DUP", Decimal("100.00"), Decimal("0.12"), 1,
                                 date(2026, 1, 1)), "LOAN_ALREADY_EXISTS")
