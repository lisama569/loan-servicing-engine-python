"""FastAPI 接口层：把领域服务暴露为本地 HTTP API。

所有金额入参为元的十进制字符串；业务错误统一返回
``{"error": {"code", "message", "details"}}``，失败原因可区分。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.errors import LoanEngineError
from app.repository import AccountRepository
from app.schemas import DisburseRequest, RepayRequest
from app.service import LoanService


def create_app(db_path: str = ":memory:") -> FastAPI:
    app = FastAPI(title="本地贷款还款与逾期处理引擎", version="0.1.0")
    service = LoanService(AccountRepository(db_path))
    app.state.service = service

    def _account_dict(account) -> dict:
        return {
            "account_no": account.account_no,
            "status": account.status.value,
            "principal_cents": account.principal_amount,
            "disbursement_date": account.disbursement_date.isoformat(),
            "last_event_date": account.last_event_date.isoformat()
            if account.last_event_date
            else None,
            "installments": [
                {
                    "period_no": i.period_no,
                    "due_date": i.due_date.isoformat(),
                    "principal_due": i.principal_due,
                    "interest_due": i.interest_due,
                    "principal_paid": i.principal_paid,
                    "interest_paid": i.interest_paid,
                    "penalty_paid": i.penalty_paid,
                }
                for i in account.installments
            ],
        }

    @app.exception_handler(LoanEngineError)
    async def engine_error_handler(_request, exc: LoanEngineError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.post("/accounts/disburse")
    def disburse(req: DisburseRequest):
        account = service.disburse(**req.model_dump())
        return {"account": _account_dict(account)}

    @app.post("/payments")
    def repay(req: RepayRequest):
        record = service.repay(
            account_no=req.account_no,
            reference_no=req.reference_no,
            amount_yuan=req.amount_yuan,
            event_date=req.event_date,
            payment_type=req.payment_type,
        )
        return {
            "payment": {
                "reference_no": record.reference_no,
                "payment_type": record.payment_type.value,
                "amount_cents": record.amount,
                "event_date": record.event_date.isoformat(),
                "waived_interest_cents": record.waived_interest,
                "resulting_status": record.resulting_status.value,
                "allocations": [vars(x) for x in record.allocations],
            }
        }

    @app.get("/accounts/{account_no}")
    def get_account(account_no: str):
        return {"account": _account_dict(service.get_account(account_no))}

    @app.get("/accounts/{account_no}/view")
    def account_view(account_no: str, as_of: str):
        from datetime import date

        view = service.account_view(account_no, date.fromisoformat(as_of))
        return {"view": view}

    return app


app = create_app()
