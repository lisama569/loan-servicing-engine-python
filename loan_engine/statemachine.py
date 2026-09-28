"""Loan status state machine."""

from __future__ import annotations

from .errors import InvalidStateError
from .models import LoanStatus

# allowed transitions
TRANSITIONS: dict[LoanStatus, frozenset[LoanStatus]] = {
    LoanStatus.OPEN: frozenset({LoanStatus.NORMAL, LoanStatus.OVERDUE, LoanStatus.PAID_OFF}),
    LoanStatus.NORMAL: frozenset({LoanStatus.OVERDUE, LoanStatus.PAID_OFF}),
    LoanStatus.OVERDUE: frozenset({LoanStatus.NORMAL, LoanStatus.PAID_OFF}),
    LoanStatus.PAID_OFF: frozenset(),
    LoanStatus.CLOSED: frozenset(),
}


def can_transition(src: LoanStatus, dst: LoanStatus) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())


def ensure_transition(src: LoanStatus, dst: LoanStatus) -> None:
    if src == dst:
        return
    if not can_transition(src, dst):
        raise InvalidStateError(
            f"illegal status transition: {src.value} -> {dst.value}",
            code="ILLEGAL_TRANSITION",
        )
