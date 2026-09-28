"""Idempotency: duplicate submissions apply exactly once."""

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


def test_duplicate_same_id_returns_original_and_applies_once(svc, log):
    log.info("input: same payment_id P1 submitted twice with identical params")
    r1 = svc.submit_payment("L1", "P1", Decimal("412.00"), date(2026, 2, 15))
    r2 = svc.submit_payment("L1", "P1", Decimal("412.00"), date(2026, 2, 15))

    assert r1.duplicate is False and r2.duplicate is True
    assert r2.applied_amount == r1.applied_amount
    loan = svc.get_loan("L1")
    payment_events = [e for e in loan.events if e.event_type == "PAYMENT"]
    log.info("payment event count after duplicate=%s (expect 1)", len(payment_events))
    assert len(payment_events) == 1
    assert loan.total_principal_paid == Decimal("400.00")
    assert loan.total_interest_paid == Decimal("12.00")


def test_reuse_id_with_different_params_is_conflict(svc, log):
    svc.submit_payment("L1", "P1", Decimal("412.00"), date(2026, 2, 15))
    for amount, vdate, kind in [
        (Decimal("10.00"), date(2026, 2, 15), "NORMAL"),       # diff amount
        (Decimal("412.00"), date(2026, 2, 16), "NORMAL"),      # diff date
        (Decimal("412.00"), date(2026, 2, 15), "EARLY_SETTLE"),# diff kind
    ]:
        log.info("conflict input id=P1 amount=%s date=%s kind=%s", amount, vdate, kind)
        with pytest.raises(LoanError) as ei:
            svc.submit_payment("L1", "P1", amount, vdate, kind)
        assert ei.value.code == "IDEMPOTENCY_CONFLICT", ei.value.code
    # original still intact
    loan = svc.get_loan("L1")
    assert len([e for e in loan.events if e.event_type == "PAYMENT"]) == 1


def test_duplicate_id_different_loan_is_independent():
    svc = LoanService()
    svc.originate("A", Decimal("100.00"), Decimal("0.12"), 1, date(2026, 1, 1))
    svc.originate("B", Decimal("100.00"), Decimal("0.12"), 1, date(2026, 1, 1))
    svc.submit_payment("A", "X", Decimal("101.00"), date(2026, 2, 1))
    svc.submit_payment("B", "X", Decimal("101.00"), date(2026, 2, 1))
    assert svc.get_loan("A").total_principal_paid == Decimal("100.00")
    assert svc.get_loan("B").total_principal_paid == Decimal("100.00")
