"""Installment schedule generation (equal principal + simple monthly interest)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .money import ZERO, add_months, q2
from .models import Installment


def build_schedule(
    principal: Decimal,
    annual_rate: Decimal,
    term_months: int,
    disbursement_date: date,
) -> list[Installment]:
    """Equal-principal installments; monthly interest on outstanding principal.

    * Each period repays ``principal / term`` (cents rounding absorbed by the
      final installment so scheduled principal always equals the loan amount).
    * Period interest = outstanding principal at period start * annual / 12,
      quantized to cents. Hence later periods carry less interest.
    * Due date of period k is disbursement date + k calendar months.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if principal <= ZERO:
        raise ValueError("principal must be positive")

    principal = q2(principal)
    per = q2(principal / term_months)
    monthly_rate = annual_rate / Decimal("12")

    installments: list[Installment] = []
    outstanding = principal
    for period in range(1, term_months + 1):
        if period == term_months:
            principal_part = outstanding  # absorb rounding remainder
        else:
            principal_part = per
        interest_part = q2(outstanding * monthly_rate)
        installments.append(
            Installment(
                period=period,
                due_date=add_months(disbursement_date, period),
                principal_due=principal_part,
                interest_due=interest_part,
            )
        )
        outstanding -= principal_part

    scheduled = sum((i.principal_due for i in installments), ZERO)
    assert scheduled == principal, (scheduled, principal)
    return installments
