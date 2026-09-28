"""罚息：按逾期本金确定性日累加（纯派生计算，无增量状态）。

规则（跨宽限期 / 跨月边界结果稳定可复现的关键）：

1. 到期日 D、宽限期 g 天，则罚息区间起点 = ``D + g + 1``（含）。
   宽限期内零罚息。起点是**派生值**，不落库、不依赖事件推进，
   任何一天查询都得到同一结果，补缴/迟到/重复事件不可能重复计息；
2. 日罚息 = 当日逾期本金 × ``penalty_daily_rate``，每日四舍五入到分后累加；
3. 本金偿还时把截至还款日的应计罚息“锁定”为历史基线（``penalty_base``），
   之后只对剩余本金继续累计；
4. 剩余本金为 0 时不再产生新罚息，历史欠息仍可查可收。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.money import quantize_cents
from app.models import Account, Installment


def penalty_start_of(inst: Installment, grace_days: int) -> date:
    """罚息起算日（含）= 到期日 + 宽限期 + 1 天。纯派生。"""
    return inst.due_date + timedelta(days=grace_days + 1)


def penalty_days(inst: Installment, as_of: date, grace_days: int) -> int:
    """截至 as_of（含）该期处于罚息区间的自然日天数（宽限期内为 0）。"""
    start = penalty_start_of(inst, grace_days)
    if as_of < start:
        return 0
    return (as_of - start).days + 1


def _accrue(principal: int, start: date, end_inclusive: date, daily_rate: Decimal) -> int:
    """[start, end_inclusive] 闭区间按日罚息之和（每日 ROUND_HALF_UP 到分）。"""
    if principal <= 0 or end_inclusive < start:
        return 0
    days = (end_inclusive - start).days + 1
    per_day = quantize_cents(Decimal(principal) * Decimal(daily_rate) / Decimal(100))
    return per_day * days


def accrued_ever(inst: Installment, as_of: date, account: Account) -> int:
    """截至 as_of 该期“曾经产生过的全部罚息”总额（不扣 penalty_paid）。

    - 未锁定：对当前本金从起算日累计到 as_of
      （本金只可能在锁定日被减少，未锁定说明未发生本金偿还）；
    - 已锁定：基线（截至还款日，对旧本金累计）+ 基线日之后对剩余本金累计。
    """
    start = penalty_start_of(inst, account.grace_days)
    if as_of < start:
        return 0
    daily = Decimal(account.penalty_daily_rate)
    if inst.penalty_base_date is not None:
        later = _accrue(
            inst.principal_balance,
            inst.penalty_base_date + timedelta(days=1),
            as_of,
            daily,
        )
        return inst.penalty_base + later
    return _accrue(inst.principal_balance, start, as_of, daily)


def installment_penalty(inst: Installment, as_of: date, account: Account) -> int:
    """该期截至 as_of 的应计未付罚息（分，已扣除 penalty_paid）。"""
    return max(0, accrued_ever(inst, as_of, account) - inst.penalty_paid)


def total_penalty(account: Account, as_of: date) -> int:
    """账户全部逾期期数的应计未付罚息合计。"""
    return sum(installment_penalty(i, as_of, account) for i in account.installments)
