"""FastAPI HTTP layer.

Endpoints
---------
* ``POST /loans``                     originate a loan
* ``POST /loans/{id}/payments``       submit (idempotent) payment
* ``GET  /loans/{id}``                loan detail / current snapshot
* ``GET  /loans/{id}/snapshot``       balances/status as of a date
* ``GET  /loans/{id}/payoff-quote``   early settlement quote
* ``GET  /loans/{id}/events``         immutable ledger / audit trail
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import LoanError
from .models import PaymentResult
from .service import LoanService


class OriginateRequest(BaseModel):
    loan_id: str
    principal: Decimal
    annual_rate: Decimal
    term_months: int
    disbursement_date: date
    grace_days: int = 3


class PaymentRequest(BaseModel):
    payment_id: str
    amount: Decimal
    value_date: date
    kind: str = "NORMAL"


def _result_dict(r: PaymentResult) -> dict:
    return {
        "payment_id": r.payment_id,
        "loan_id": r.loan_id,
        "kind": r.kind,
        "value_date": r.value_date.isoformat(),
        "requested_amount": str(r.requested_amount),
        "applied_amount": str(r.applied_amount),
        "duplicate": r.duplicate,
        "settled": r.settled,
        "status_transition": f"{r.before_status} -> {r.after_status}",
        "balances": {
            "principal": [str(r.principal_before), str(r.principal_after)],
            "interest": [str(r.interest_before), str(r.interest_after)],
            "penalty": [str(r.penalty_before), str(r.penalty_after)],
        },
        "allocations": [
            {
                "period": l.period,
                "to_penalty": str(l.to_penalty),
                "to_interest": str(l.to_interest),
                "to_principal": str(l.to_principal),
                "interest_waived": str(l.interest_waived),
            }
            for l in r.lines
        ],
    }


def create_app(service: LoanService | None = None) -> FastAPI:
    service = service or LoanService()
    app = FastAPI(title="Local Loan Servicing Engine")

    @app.exception_handler(LoanError)
    async def _loan_error_handler(_: Request, exc: LoanError) -> JSONResponse:
        status = 404 if exc.code == "LOAN_NOT_FOUND" else 409
        return JSONResponse(
            status_code=status,
            content={"error": exc.code, "message": str(exc)},
        )

    @app.post("/loans", status_code=201)
    def originate(req: OriginateRequest):
        loan = service.originate(
            req.loan_id,
            req.principal,
            req.annual_rate,
            req.term_months,
            req.disbursement_date,
            req.grace_days,
        )
        return {"loan_id": loan.loan_id, "status": loan.status.value}

    @app.post("/loans/{loan_id}/payments")
    def submit_payment(loan_id: str, req: PaymentRequest):
        result = service.submit_payment(
            loan_id,
            req.payment_id,
            req.amount,
            req.value_date,
            req.kind,
        )
        return _result_dict(result)

    @app.get("/loans/{loan_id}")
    def get_loan(loan_id: str):
        return service.snapshot(loan_id)

    @app.get("/loans/{loan_id}/snapshot")
    def snapshot(loan_id: str, as_of: date | None = None):
        return service.snapshot(loan_id, as_of)

    @app.get("/loans/{loan_id}/payoff-quote")
    def payoff_quote(loan_id: str, as_of: date):
        return {
            "loan_id": loan_id,
            "as_of": as_of.isoformat(),
            "payoff_amount": str(service.payoff_quote(loan_id, as_of)),
        }

    @app.get("/loans/{loan_id}/events")
    def events(loan_id: str):
        loan = service.get_loan(loan_id)
        return {
            "loan_id": loan_id,
            "events": [
                {
                    "event_id": e.event_id,
                    "type": e.event_type,
                    "value_date": e.value_date.isoformat(),
                    "amount": str(e.amount),
                    "detail": e.detail,
                    "resulting_status": e.status,
                    "resulting_balances": {
                        "principal": str(e.principal_balance),
                        "interest": str(e.interest_balance),
                        "penalty": str(e.penalty_balance),
                    },
                }
                for e in loan.events
            ],
        }

    return app


app = create_app()
