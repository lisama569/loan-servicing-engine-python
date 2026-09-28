"""还款计划生成与提前还本的利息减免。

采用**等额本金**：
- 每期应还本金 = 本金 / 期数（余数并入末期）；
- 每期利息 = 期初剩余本金 × 年利率 × 周期天数 / 360（ROUND_HALF_UP 到分）；
- 提前结清（EARLY_SETTLEMENT）时，所有未到期账单（due_date > 结清日）的
  应计利息全额减免；已到期利息仍需支付。减免规则确定性、可复现。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models import Account, Installment
from app.money import quantize_cents


def generate_installments(
    principal_cents: int,
    annual_rate: str,
    term_periods: int,
    first_due_date: date,
    period_days: int = 30,
) -> list[Installment]:
    """生成等额本金还款计划。"""
    if principal_cents <= 0:
        raise ValueError("本金必须为正")
    if term_periods <= 0:
        raise ValueError("期数必须为正")
    if period_days <= 0:
        raise ValueError("周期天数必须为正")

    rate = Decimal(annual_rate)
    if rate < 0:
        raise ValueError("利率不能为负")

    base = principal_cents // term_periods
    remainder = principal_cents % term_periods  # 余数并入末期，保证本金守恒

    schedule: list[Installment] = []
    remaining = principal_cents
    due = first_due_date
    for period_no in range(1, term_periods + 1):
        principal = base + (remainder if period_no == term_periods else 0)
        interest = quantize_cents(
            (Decimal(remaining) * rate * Decimal(period_days) / Decimal(360))
            / Decimal(100)
        )
        schedule.append(
            Installment(
                period_no=period_no,
                due_date=due,
                principal_due=principal,
                interest_due=interest,
            )
        )
        remaining -= principal
        due = due + timedelta(days=period_days)

    assert remaining == 0, "计划本金之和必须等于放款本金"
    return schedule


def future_interest_waive(account: Account, as_of: date) -> int:
    """提前结清时可减免的利息（分）：所有未到期账单的未付利息。"""
    return sum(
        inst.interest_balance
        for inst in account.installments
        if inst.due_date > as_of
    )
