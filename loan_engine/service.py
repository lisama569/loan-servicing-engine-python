"""Application service: originations, payments, idempotency, late posting.

Concurrency / consistency contract
----------------------------------
* ``submit_payment`` serializes on a per-loan lock.
* A repeat submission with the same ``(loan_id, payment_id)`` and the same
  parameters returns the original result (``duplicate=True``); the payment
  is applied exactly once. Reusing an id with different parameters raises
  ``IDEMPOTENCY_CONFLICT``.
* Validation runs before commit; on failure the loan is restored to its
  prior snapshot, so rejected operations leave no partial state (no event,
  no allocation, no moved accrual cursor).
* Query helpers (``snapshot`` / ``payoff_quote``) accrue on a throwaway
  copy: reads never move the live accrual cursor.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal

from .allocation import (
    PAYMENT_KIND_EARLY_SETTLE,
    PAYMENT_KIND_NORMAL,
    apply_payment,
    early_settle_quote,
)
from .errors import (
    IdempotencyConflictError,
    LoanNotFoundError,
    PaymentAmountError,
)
from .models import LedgerEvent, Loan, LoanStatus, PaymentResult
from .overdue import accrue_penalties, evaluate_status
from .repository import LoanRepository
from .schedule import build_schedule

log = logging.getLogger("loan_engine")


class LoanService:
    def __init__(self, repo: LoanRepository | None = None) -> None:
        self.repo = repo or LoanRepository()

    # ------------------------------------------------------------------ origination
    def originate(
        self,
        loan_id: str,
        principal: Decimal,
        annual_rate: Decimal,
        term_months: int,
        disbursement_date: date,
        grace_days: int = 3,
    ) -> Loan:
        principal = Decimal(str(principal))
        annual_rate = Decimal(str(annual_rate))
        if principal <= 0:
            raise PaymentAmountError("principal must be positive")
        if annual_rate < 0 or grace_days < 0 or term_months <= 0:
            raise PaymentAmountError("invalid loan parameters")

        installments = build_schedule(
            principal, annual_rate, term_months, disbursement_date
        )
        loan = Loan(
            loan_id=loan_id,
            principal=principal,
            annual_rate=annual_rate,
            term_months=term_months,
            disbursement_date=disbursement_date,
            grace_days=grace_days,
            installments=installments,
            status=LoanStatus.OPEN,
            last_event_date=disbursement_date,
        )
        loan.events.append(
            LedgerEvent(
                event_id=f"{loan_id}:DISBURSEMENT",
                loan_id=loan_id,
                event_type="DISBURSEMENT",
                value_date=disbursement_date,
                amount=principal,
                detail={"term_months": term_months, "annual_rate": str(annual_rate)},
                principal_balance=principal,
                interest_balance=sum(i.interest_due for i in installments),
                status=LoanStatus.OPEN.value,
            )
        )
        self.repo.save(loan)
        log.info(
            "originate loan=%s principal=%s rate=%s term=%s due1=%s",
            loan_id, principal, annual_rate, term_months,
            installments[0].due_date,
        )
        return loan

    # -------------------------------------------------------------------- payments
    def submit_payment(
        self,
        loan_id: str,
        payment_id: str,
        amount: Decimal,
        value_date: date,
        kind: str = PAYMENT_KIND_NORMAL,
    ) -> PaymentResult:
        if not payment_id:
            raise PaymentAmountError("payment_id is required")
        amount = Decimal(str(amount))

        with self.repo.lock(loan_id):
            loan = self.repo.get(loan_id)  # raises LOAN_NOT_FOUND if absent

            existing = self.repo.get_payment_result(loan_id, payment_id)
            if existing is not None:
                return self._handle_duplicate(existing, amount, value_date, kind)

            # snapshot for full rollback on any validation failure
            backup = copy.deepcopy(vars(loan))
            try:
                result = apply_payment(loan, payment_id, amount, value_date, kind)
            except Exception:
                loan.__dict__.clear()
                loan.__dict__.update(backup)
                log.warning(
                    "payment rejected loan=%s id=%s amount=%s date=%s -> rolled back",
                    loan_id, payment_id, amount, value_date,
                )
                raise

            self.repo.store_payment_result(loan_id, payment_id, result)
            self._log_payment(result)
            return result

    def _handle_duplicate(
        self,
        existing: PaymentResult,
        amount: Decimal,
        value_date: date,
        kind: str,
    ) -> PaymentResult:
        if (
            existing.requested_amount != amount
            or existing.value_date != value_date
            or existing.kind != kind
        ):
            raise IdempotencyConflictError(
                f"payment_id {existing.payment_id} already applied with "
                f"amount={existing.requested_amount}, date={existing.value_date}, "
                f"kind={existing.kind}"
            )
        log.info(
            "duplicate payment suppressed loan=%s id=%s amount=%s",
            existing.loan_id, existing.payment_id, existing.requested_amount,
        )
        return replace(existing, duplicate=True)

    @staticmethod
    def _log_payment(r: PaymentResult) -> None:
        log.info(
            "payment applied loan=%s id=%s kind=%s date=%s amount=%s "
            "status %s->%s principal %s->%s interest %s->%s penalty %s->%s settled=%s",
            r.loan_id, r.payment_id, r.kind, r.value_date, r.requested_amount,
            r.before_status, r.after_status,
            r.principal_before, r.principal_after,
            r.interest_before, r.interest_after,
            r.penalty_before, r.penalty_after, r.settled,
        )
        for line in r.lines:
            log.info(
                "  allocate period=%s penalty=%s interest=%s principal=%s waived=%s",
                line.period, line.to_penalty, line.to_interest,
                line.to_principal, line.interest_waived,
            )

    # --------------------------------------------------------------------- queries
    def get_loan(self, loan_id: str) -> Loan:
        return self.repo.get(loan_id)

    def _view_at(self, loan: Loan, as_of: date) -> Loan:
        """A copy accrued/evaluated to ``as_of``; live state untouched."""
        view = copy.deepcopy(loan)
        if loan.last_event_date is None or as_of >= loan.last_event_date:
            accrue_penalties(view, as_of)
            evaluate_status(view, as_of)
        return view

    def snapshot(self, loan_id: str, as_of: date | None = None) -> dict:
        loan = self.repo.get(loan_id)
        with self.repo.lock(loan_id):
            as_of = as_of or loan.last_event_date
            view = self._view_at(loan, as_of)
            return {
                "loan_id": loan.loan_id,
                "as_of": as_of.isoformat(),
                "status": view.status.value,
                "principal_balance": str(view.principal_balance),
                "interest_balance": str(view.interest_balance),
                "penalty_balance": str(view.penalty_balance),
                "total_due": str(view.total_due),
                "total_principal_paid": str(view.total_principal_paid),
                "total_interest_paid": str(view.total_interest_paid),
                "total_penalty_paid": str(view.total_penalty_paid),
                "installments": [
                    {
                        "period": i.period,
                        "due_date": i.due_date.isoformat(),
                        "principal_due": str(i.principal_due),
                        "interest_due": str(i.interest_due),
                        "principal_paid": str(i.principal_paid),
                        "interest_paid": str(i.interest_paid),
                        "penalty_paid": str(i.penalty_paid),
                        "penalty_accrued": str(i.penalty_accrued),
                        "paid_date": i.paid_date.isoformat() if i.paid_date else None,
                    }
                    for i in view.installments
                ],
            }

    def payoff_quote(self, loan_id: str, as_of: date) -> Decimal:
        loan = self.repo.get(loan_id)
        with self.repo.lock(loan_id):
            view = self._view_at(loan, as_of)
            return early_settle_quote(view, as_of)
