"""逾期判定（基于到期日 + 宽限期的纯派生状态）。

逾期不设独立账户状态、不落增量标记：对任意 as_of 都由账单到期日、
宽限期与未偿余额确定性计算，因此状态不会倒退、跨日查询可重复、
补缴/迟到/重复事件不会造成重复罚息。
"""

from __future__ import annotations

from datetime import date, timedelta

from app.models import Account, Installment


def installment_overdue(inst: Installment, as_of: date, grace_days: int) -> bool:
    """该期在 as_of 是否逾期（已超过宽限期且本金或利息未清）。"""
    if inst.due_date > as_of:
        return False
    threshold = inst.due_date + timedelta(days=grace_days)
    if as_of <= threshold:
        return False
    return inst.principal_balance > 0 or inst.interest_balance > 0


def is_overdue(account: Account, as_of: date) -> bool:
    """账户是否存在已跨宽限期且未清偿（本金或利息）的期数。"""
    return any(
        installment_overdue(i, as_of, account.grace_days)
        for i in account.installments
    )


def apply_overdue_marks(account: Account, as_of: date) -> list[str]:
    """逾期推进说明（兼容保留）。

    新实现中罚息起算日为纯派生值，无需推进；这里返回可解释的逾期观察说明，
    供服务层日志使用，重复调用结果一致、无副作用。
    """
    notes: list[str] = []
    for inst in account.installments:
        threshold = inst.due_date + timedelta(days=account.grace_days)
        if as_of > threshold and not inst.is_paid:
            notes.append(
                f"第{inst.period_no}期到期日{inst.due_date}，"
                f"宽限期{account.grace_days}天至{threshold}，"
                f"自{threshold + timedelta(days=1)}起计罚息"
            )
    return notes
