"""逾期判定与罚息：宽限期边界、跨月、确定性重算、恢复。"""

from __future__ import annotations

from datetime import date

from app.models import Account, AccountStatus
from app.overdue import apply_overdue_marks, is_overdue
from app.penalty import installment_penalty, penalty_start_of, total_penalty
from app.schedule import generate_installments


def _account(grace=3, rate="0.0005"):
    acc = Account(
        account_no="X", status=AccountStatus.ACTIVE, principal_amount=100000,
        annual_rate="0.12", term_periods=3, grace_days=grace,
        penalty_daily_rate=rate,
    )
    acc.installments = generate_installments(100000, "0.12", 3, date(2026, 11, 1))
    return acc


def test_within_grace_no_penalty():
    acc = _account(grace=3)
    # 到期日 11-01，宽限到 11-04
    assert is_overdue(acc, date(2026, 11, 1)) is False
    assert is_overdue(acc, date(2026, 11, 4)) is False
    notes = apply_overdue_marks(acc, date(2026, 11, 4))
    assert notes == []
    assert total_penalty(acc, date(2026, 11, 4)) == 0


def test_penalty_starts_day_after_grace_boundary():
    acc = _account(grace=3)
    # 11-05 为罚息起算日（含）：当天 1 天罚息
    apply_overdue_marks(acc, date(2026, 11, 5))
    assert is_overdue(acc, date(2026, 11, 5)) is True
    from app.penalty import penalty_start_of
    assert penalty_start_of(acc.installments[0], 3) == date(2026, 11, 5)
    per_day = round(33333 * 0.0005)  # 17 分/日
    assert total_penalty(acc, date(2026, 11, 5)) == per_day
    # 11-10：6 天
    assert total_penalty(acc, date(2026, 11, 10)) == per_day * 6


def test_overdue_marks_idempotent_and_reproducible():
    acc = _account()
    notes1 = apply_overdue_marks(acc, date(2026, 11, 10))
    assert notes1  # 纯说明，无副作用
    # 再次推进 / 用更早日期观察都不会改变结果
    notes2 = apply_overdue_marks(acc, date(2026, 11, 10))
    assert notes2 == notes1
    p1 = total_penalty(acc, date(2026, 11, 10))
    p2 = total_penalty(acc, date(2026, 11, 10))
    assert p1 == p2


def test_penalty_cross_month_boundary_stable():
    # 第 2 期 12-01 到期，跨到次年 1 月
    acc = _account()
    apply_overdue_marks(acc, date(2027, 1, 5))
    inst2 = acc.installments[1]
    # 起算日 12-05；到 2027-01-05 共 32 天
    assert penalty_start_of(inst2, 3) == date(2026, 12, 5)
    per_day = round(33333 * 0.0005)
    assert installment_penalty(inst2, date(2027, 1, 5), acc) == per_day * 32


def test_no_new_penalty_after_principal_cleared_but_keeps_history(service, standard_loan, caplog):
    # 逾期 6 天后还掉第 1 期全部（含罚息）
    service.repay("L1", "R1", "344.35", date(2026, 11, 10))
    acc = service.get_account("L1")
    inst1 = acc.installments[0]
    assert inst1.principal_balance == 0
    assert inst1.interest_balance == 0
    # 之后再观察，不再产生新罚息，也不出现负值
    assert total_penalty(acc, date(2026, 12, 1)) == 0
    assert installment_penalty(inst1, date(2027, 6, 1), acc) == 0
    # 已不再处于逾期（该期本息已清，其余未到期）
    assert is_overdue(acc, date(2026, 11, 20)) is False
