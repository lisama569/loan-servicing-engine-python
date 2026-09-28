"""In-memory repository with per-loan locking for concurrency control.

Guarantees
----------
* Every mutation for a given loan serializes on one RLock, so concurrent
  payments cannot interleave their allocation (balance conservation).
* Successful payment results are keyed by ``(loan_id, payment_id)`` and
  returned as-is on repeat submission (idempotency, first-write-wins).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .errors import LoanAlreadyExistsError, LoanNotFoundError
from .models import Loan, PaymentResult


class LoanRepository:
    def __init__(self) -> None:
        self._loans: dict[str, Loan] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._payments: dict[tuple[str, str], PaymentResult] = {}
        self._guard = threading.RLock()

    def _lock_for(self, loan_id: str) -> threading.RLock:
        # registry itself is protected; the per-loan lock is created once.
        with self._guard:
            lock = self._locks.get(loan_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[loan_id] = lock
            return lock

    @contextmanager
    def lock(self, loan_id: str) -> Iterator[threading.RLock]:
        lock = self._lock_for(loan_id)
        lock.acquire()
        try:
            yield lock
        finally:
            lock.release()

    def save(self, loan: Loan) -> None:
        with self._guard:
            if loan.loan_id in self._loans:
                raise LoanAlreadyExistsError(f"loan already exists: {loan.loan_id}")
            self._loans[loan.loan_id] = loan

    def get(self, loan_id: str) -> Loan:
        with self._guard:
            loan = self._loans.get(loan_id)
        if loan is None:
            raise LoanNotFoundError(f"loan not found: {loan_id}")
        return loan

    def has(self, loan_id: str) -> bool:
        with self._guard:
            return loan_id in self._loans

    def store_payment_result(
        self, loan_id: str, payment_id: str, result: PaymentResult
    ) -> None:
        with self._guard:
            key = (loan_id, payment_id)
            existing = self._payments.get(key)
            if existing is not None:
                # first-write-wins; repeat submission must not overwrite
                return
            self._payments[key] = result

    def get_payment_result(self, loan_id: str, payment_id: str) -> PaymentResult | None:
        with self._guard:
            return self._payments.get((loan_id, payment_id))
