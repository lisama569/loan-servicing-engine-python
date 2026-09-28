"""HTTP 请求/响应 schema（需定义在模块顶层供 FastAPI 解析）。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.models import PaymentType


class DisburseRequest(BaseModel):
    account_no: str
    principal_yuan: str
    annual_rate: str = "0.0"
    term_periods: int = Field(gt=0)
    disbursement_date: date
    first_due_date: date
    grace_days: int = Field(default=0, ge=0)
    penalty_daily_rate: str = "0.0"
    period_days: int = Field(default=30, gt=0)


class RepayRequest(BaseModel):
    account_no: str
    reference_no: str
    amount_yuan: str
    event_date: date
    payment_type: PaymentType = PaymentType.REPAY
