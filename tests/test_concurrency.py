"""Concurrent submissions: serialization, conservation, no partial updates."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

from loan_engine.errors import LoanError
from loan_engine.service import LoanService


def _originate(loan_id="L1"):
    svc = LoanService()
    # 3 equal-principal installments: (400,12),(400,8),(400,4)
    svc.originate(loan_id, Decimal("1200.00"), Decimal("0.12"), 3,
                  date(2026, 1, 15), grace_days=3)
    return svc


def test_concurrent_partials_sum_exactly_to_billed():
    svc = _originate()
    jobs = [("A", "100"), ("B", "100"), ("C", "100"), ("D", "100"), ("E", "12")]

    def run(job):
        pid, amt = job
        return svc.submit_payment("L1", pid, Decimal(amt), date(2026, 2, 15))

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run, jobs))

    loan = svc.get_loan("L1")
    assert len(results) == 5
    assert loan.total_principal_paid == Decimal("400.00")
    assert loan.total_interest_paid == Decimal("12.00")
    assert loan.penalty_balance == 0
    assert loan.installments[0].is_paid
    # ledger contains exactly one event per successful payment
    assert len([e for e in loan.events if e.event_type == "PAYMENT"]) == 5


def test_concurrent_duplicate_and_overpay_race_conserves():
    svc = _originate()

    def run(job):
        pid, amt = job
        try:
            return ("ok", svc.submit_payment("L1", pid, Decimal(amt),
                                             date(2026, 2, 15)))
        except LoanError as exc:
            return ("fail:" + exc.code, None)

    jobs = [("DUP", "412"), ("DUP", "412"), ("DUP", "412"),
            ("X1", "412"), ("X2", "5")]
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(run, jobs))

    oks = [o for o in outcomes if o[0] == "ok"]
    dup_flags = [o[1].duplicate for o in oks if o[1] is not None]
    assert len(oks) == 3                 # 1 original + 2 duplicate replays
    assert dup_flags.count(True) == 2
    assert sorted(o[0] for o in outcomes).count("fail:NOTHING_DUE") == 2

    loan = svc.get_loan("L1")
    # general ledger conservation across the whole account
    assert loan.total_principal_paid == Decimal("400.00")
    assert loan.total_interest_paid == Decimal("12.00")
    assert loan.principal_balance + loan.total_principal_paid == Decimal("1200.00")
    assert len([e for e in loan.events if e.event_type == "PAYMENT"]) == 1


def test_concurrent_payments_across_loans_do_not_block_or_mix():
    svc = LoanService()
    svc.originate("A", Decimal("1200.00"), Decimal("0.12"), 3, date(2026, 1, 15))
    svc.originate("B", Decimal("1200.00"), Decimal("0.12"), 3, date(2026, 1, 15))

    def run(job):
        loan_id, pid = job
        try:
            svc.submit_payment(loan_id, pid, Decimal("412.00"), date(2026, 2, 15))
            return "ok"
        except LoanError as exc:
            return "fail:" + exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(run, [("A", "PA1"), ("A", "PA2"),
                                       ("B", "PB1"), ("B", "PB2")]))

    # one effective payment per loan; the duplicate-billed extra is rejected
    assert outcomes.count("ok") == 2
    assert outcomes.count("fail:NOTHING_DUE") == 2

    # each loan gets exactly one effective 412 payment; second rejected
    assert svc.get_loan("A").total_principal_paid == Decimal("400.00")
    assert svc.get_loan("B").total_principal_paid == Decimal("400.00")
    assert svc.get_loan("A").total_interest_paid == Decimal("12.00")
    assert svc.get_loan("B").total_interest_paid == Decimal("12.00")
