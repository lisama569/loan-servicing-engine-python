"""金额工具与还款计划的基础规则。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.errors import InvalidAmountError
from app.money import cents_to_yuan, quantize_cents, yuan_to_cents
from app.schedule import generate_installments


@pytest.mark.parametrize(
    "text,expected",
    [("1000.00", 100000), ("0.01", 1), ("1000", 100000), ("12.34", 1234)],
)
def test_yuan_to_cents_ok(text, expected):
    assert yuan_to_cents(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "-1.00", "0", "0.00", "1.234", "nan", "inf"])
def test_yuan_to_cents_rejects_bad_input(text):
    with pytest.raises(InvalidAmountError) as ei:
        yuan_to_cents(text)
    assert ei.value.code == "INVALID_AMOUNT"


def test_cents_to_yuan_roundtrip():
    assert cents_to_yuan(100005) == "1000.05"
    assert cents_to_yuan(0) == "0.00"


def test_quantize_half_up():
    assert quantize_cents(Decimal("0.005")) == 1
    assert quantize_cents(Decimal("0.004")) == 0


def test_schedule_principal_conservation_and_interest():
    insts = generate_installments(100000, "0.12", 3, date(2026, 11, 1))
    assert [i.principal_due for i in insts] == [33333, 33333, 33334]
    assert sum(i.principal_due for i in insts) == 100000
    # 利息：剩余本金 * 12% * 30/360
    assert insts[0].interest_due == 1000
    assert insts[1].interest_due == 667
    assert insts[2].interest_due == 333
    assert [i.due_date for i in insts] == [
        date(2026, 11, 1), date(2026, 12, 1), date(2026, 12, 31)
    ]


def test_schedule_remainder_folds_into_last_period():
    insts = generate_installments(100, "0", 3, date(2026, 11, 1))
    assert [i.principal_due for i in insts] == [33, 33, 34]
