"""Schedule generation and money rounding invariants."""

from datetime import date
from decimal import Decimal

from loan_engine.money import add_months, q2
from loan_engine.schedule import build_schedule


def test_add_months_clamps_month_end(log):
    log.info("input add_months(2026-01-31, 1) expect 2026-02-28")
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)


def test_q2_half_up(log):
    assert q2("1.005") == Decimal("1.01")
    assert q2("1.004") == Decimal("1.00")


def test_equal_principal_schedule_conserves_principal(log):
    principal = Decimal("1000.00")
    sched = build_schedule(principal, Decimal("0.24"), 6, date(2026, 1, 10))
    log.info("schedule rows: %s", [(i.period, str(i.due_date), str(i.principal_due),
                                    str(i.interest_due)) for i in sched])
    assert sum((i.principal_due for i in sched), Decimal("0")) == principal
    # rounding remainder absorbed by last period
    assert sched[-1].principal_due == principal - sum(
        (i.principal_due for i in sched[:-1]), Decimal("0")
    )
    # interest strictly decreases as outstanding principal drops
    interests = [i.interest_due for i in sched]
    assert interests == sorted(interests, reverse=True)
    assert interests[0] == q2(Decimal("1000.00") * Decimal("0.24") / 12)
