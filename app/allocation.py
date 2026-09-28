"""还款金额分配（资金瀑布）。

普通还款（REPAY）分配顺序：

1. 截至 event_date 的全部应计未付**罚息**（按期号顺序）；
2. 最早已到期账单的**利息**，依次向后；
3. 最早已到期账单的**本金**，依次向后；
4. 仍有剩余时，按最早未到期账单顺序提前偿还**本金**
   （未到期利息按计划照常，不做减免；想免未来利息请走 EARLY_SETTLEMENT）。

约束：
- 金额 > 应还合计（罚息 + 已到期利息 + 全部剩余本金）即超额，拒绝；
- 提前结清（EARLY_SETTLEMENT）必须**恰好**等于
  罚息 + 已到期利息 + 全部剩余本金，未到期利息全额减免；
- 罚息在瀑布第一步处理：走到本金扣减时，该期截至当日的罚息必然已付清；
  扣减本金前把“截至当日的应计罚息总额”固化进 ``penalty_base``，
  之后只对剩余本金继续累计，部分还本不丢失也不重复历史罚息。
"""

from __future__ import annotations

from datetime import date

from app.errors import InsufficientPayoffError, OverpaymentError
from app.models import Account, Installment, LedgerEntry
from app.penalty import accrued_ever, installment_penalty
from app.schedule import future_interest_waive


def payable_summary(account: Account, as_of: date) -> dict[str, int]:
    """汇总当前应还（分）。"""
    penalty = sum(installment_penalty(i, as_of, account) for i in account.installments)
    matured_interest = sum(
        i.interest_balance for i in account.installments if i.due_date <= as_of
    )
    matured_principal = sum(
        i.principal_balance for i in account.installments if i.due_date <= as_of
    )
    future_principal = sum(
        i.principal_balance for i in account.installments if i.due_date > as_of
    )
    total_due = penalty + matured_interest + matured_principal + future_principal
    return {
        "penalty": penalty,
        "matured_interest": matured_interest,
        "matured_principal": matured_principal,
        "future_principal": future_principal,
        "repay_max": total_due,
        "payoff_amount": total_due,
        "waivable_interest": future_interest_waive(account, as_of),
    }


def _pay_penalty(
    inst: Installment, account: Account, budget: int, as_of: date
) -> tuple[int, LedgerEntry | None]:
    """从 budget 中支付该期应计未付罚息，返回 (支付额, 明细)。"""
    accrued = installment_penalty(inst, as_of, account)
    paid = min(budget, accrued)
    if paid <= 0:
        return 0, None
    inst.penalty_paid += paid
    return paid, LedgerEntry(
        period_no=inst.period_no,
        kind="PENALTY",
        amount=paid,
        reason=f"第{inst.period_no}期截至{as_of}应计未付罚息{accrued}分，本次付{paid}分",
    )


def _lock_before_principal(
    inst: Installment, account: Account, as_of: date
) -> None:
    """扣减本金前锁定历史罚息基线。

    只有该期截至 as_of 的罚息已被**全额付清**（应计未付为 0）才锁定：
    之后只对剩余本金继续累计；若罚息未付清则不允许减少本金的计息基础，
    保留全额本金继续计息（对债权人有利，且绝不出现负罚息）。
    重复锁定（已存在基线）只累加基线日之后的增量，不覆盖旧基线。
    """
    if inst.principal_balance <= 0:
        return
    if installment_penalty(inst, as_of, account) > 0:
        return
    inst.penalty_base = accrued_ever(inst, as_of, account)
    inst.penalty_base_date = as_of


def allocate_repayment(account: Account, amount: int, as_of: date) -> list[LedgerEntry]:
    """普通还款资金瀑布。超额抛 OverpaymentError。"""
    summary = payable_summary(account, as_of)
    if amount > summary["repay_max"]:
        raise OverpaymentError(
            "还款金额超过当前应还总额",
            amount=amount,
            repay_max=summary["repay_max"],
            excess=amount - summary["repay_max"],
            hint="请减少金额，或使用 EARLY_SETTLEMENT 提前结清",
        )

    allocations: list[LedgerEntry] = []
    remaining = amount

    # 1) 罚息（按期号）
    for inst in account.installments:
        if remaining <= 0:
            break
        paid, entry = _pay_penalty(inst, account, remaining, as_of)
        if entry is not None:
            allocations.append(entry)
            remaining -= paid

    # 2) 已到期利息（按期号）
    for inst in account.installments:
        if remaining <= 0:
            break
        if inst.due_date <= as_of and inst.interest_balance > 0:
            paid = min(remaining, inst.interest_balance)
            inst.interest_paid += paid
            remaining -= paid
            allocations.append(LedgerEntry(
                period_no=inst.period_no, kind="INTEREST", amount=paid,
                reason=f"第{inst.period_no}期已到期利息（到期日{inst.due_date}）",
            ))

    # 3) 已到期本金（按期号）
    for inst in account.installments:
        if remaining <= 0:
            break
        if inst.due_date <= as_of and inst.principal_balance > 0:
            _lock_before_principal(inst, account, as_of)
            paid = min(remaining, inst.principal_balance)
            inst.principal_paid += paid
            remaining -= paid
            allocations.append(LedgerEntry(
                period_no=inst.period_no, kind="PRINCIPAL", amount=paid,
                reason=f"第{inst.period_no}期已到期本金（到期日{inst.due_date}）",
            ))

    # 4) 未到期本金提前偿还（按期号）
    for inst in account.installments:
        if remaining <= 0:
            break
        if inst.due_date > as_of and inst.principal_balance > 0:
            _lock_before_principal(inst, account, as_of)
            paid = min(remaining, inst.principal_balance)
            inst.principal_paid += paid
            remaining -= paid
            allocations.append(LedgerEntry(
                period_no=inst.period_no, kind="PRINCIPAL", amount=paid,
                reason=f"第{inst.period_no}期未到期提前还本（到期日{inst.due_date}）",
            ))

    assert remaining == 0, "资金瀑布必须全额分配，否则应已被 OverpaymentError 拦截"
    return allocations


def allocate_early_settlement(
    account: Account, amount: int, as_of: date
) -> tuple[list[LedgerEntry], int]:
    """提前结清。返回 (分配明细, 减免利息)；金额不符抛区分化的错误。"""
    summary = payable_summary(account, as_of)
    required = summary["payoff_amount"]
    waived = summary["waivable_interest"]

    if amount < required:
        raise InsufficientPayoffError(
            "提前结清金额不足",
            amount=amount,
            required=required,
            shortfall=required - amount,
        )
    if amount > required:
        raise OverpaymentError(
            "提前结清金额超过结清应付总额",
            amount=amount,
            required=required,
            excess=amount - required,
        )

    allocations = allocate_repayment(account, amount, as_of)
    # 未到期账单利息减免
    for inst in account.installments:
        if inst.due_date > as_of and inst.interest_balance > 0:
            waived_here = inst.interest_balance
            inst.interest_paid += waived_here
            allocations.append(LedgerEntry(
                period_no=inst.period_no, kind="INTEREST_WAIVED", amount=waived_here,
                reason=f"第{inst.period_no}期未到期利息随提前结清减免",
            ))
    return allocations, waived
