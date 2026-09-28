"""Decimal money helpers and date arithmetic."""

from __future__ import annotations

import calendar

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def q2(amount: Decimal | int | str | float) -> Decimal:
    """Quantize to cents, half-up (deterministic money rounding)."""
    return Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping to month end (e.g. Jan 31 -> Feb 28)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day)
    return date(year, month, day)


def days_between(start: date, end: date) -> int:
    """Inclusive-day counter used for daily accrual: end - start."""
    return (end - start).days


def cap(value: Decimal, ceiling: Decimal) -> Decimal:
    if value <= ZERO:
        return ZERO
    return min(value, ceiling)
