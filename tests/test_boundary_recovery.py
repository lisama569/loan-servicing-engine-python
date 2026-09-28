"""跨宽限期边界、逾期恢复、多期跨月、迟到入账、持久化恢复。"""

from __future__ import annotations

from datetime import date

import pytest

from app.errors import StaleEventError
from app.models import AccountStatus
from app.penalty import total_penalty
from app.repository import AccountRepository
from app.service import LoanService


def test_repay_on_last_grace_day_no_penalty(service, standard_loan):
    # 到期 11-01，宽限到 11-04：11-04 当天还款仍无罚息
    r = service.repay("L1", "B1", "343.33", date(2026, 11, 4))
    assert all(a.kind != "PENALTY" for a in r.allocations)
    from app.penalty import total_penalty as _tp
    assert _tp(service.get_account("L1"), date(2026, 11, 4)) == 0


def test_repay_at_penalty_start_day_accrues_one_day(service, standard_loan):
    r = service.repay("L1", "B2", "343.50", date(2026, 11, 5))
    assert any(a.kind == "PENALTY" and a.amount == 17 for a in r.allocations)


def test_overdue_recovery_then_normal_state(service, standard_loan):
    # 逾期 -> 还完第 1 期 -> 账户脱离逾期；后续正常还款不受历史罚息污染
    service.repay("L1", "B3", "344.35", date(2026, 11, 10))
    acc = service.get_account("L1")
    from app.overdue import is_overdue
    assert is_overdue(acc, date(2026, 11, 10)) is False
    # 第 2 期正常还
    service.repay("L1", "B4", "340.00", date(2026, 12, 1))
    acc = service.get_account("L1")
    assert acc.installments[1].is_paid
    assert total_penalty(acc, date(2026, 12, 1)) == 0


def test_large_partial_after_long_overdue_conservation(service, standard_loan):
    # 拖到 2027-01-10：三期全部逾期，大额部分还款，校验资金守恒
    r = service.repay("L1", "B5", "600.00", date(2027, 1, 10))
    total = sum(a.amount for a in r.allocations)
    assert total == 60000
    # 分配顺序：先三期罚息，再利息，再本金
    kinds = [a.kind for a in r.allocations]
    non_penalty = [k for k in kinds if k != "PENALTY"]
    # 所有非罚息项中，利息全部排在本金之前
    first_principal = next((i for i, k in enumerate(non_penalty) if k == "PRINCIPAL"), len(non_penalty))
    assert all(k == "INTEREST" for k in non_penalty[:first_principal])
    acc = service.get_account("L1")
    paid_penalty = sum(i.penalty_paid for i in acc.installments)
    paid_interest = sum(i.interest_paid for i in acc.installments)
    paid_principal = sum(i.principal_paid for i in acc.installments)
    assert paid_penalty + paid_interest + paid_principal == 60000
    # 还款后重算罚息为 0（已全部付清到 1-10），且再往后只对剩余本金累计
    assert total_penalty(acc, date(2027, 1, 10)) == 0
    assert total_penalty(acc, date(2027, 1, 11)) > 0


def test_late_posted_event_rejected_and_no_state_regression(service, standard_loan):
    service.repay("L1", "B6", "100.00", date(2026, 11, 20))
    snapshot_principal = sum(i.principal_paid for i in service.get_account("L1").installments)
    with pytest.raises(StaleEventError) as ei:
        # 模拟迟到入账：业务日期 11-10 的事件 11-21 才到达
        service.repay("L1", "B7", "50.00", date(2026, 11, 10))
    assert ei.value.code == "STALE_EVENT"
    acc = service.get_account("L1")
    assert len(acc.payments) == 1
    assert sum(i.principal_paid for i in acc.installments) == snapshot_principal
    assert acc.last_event_date == date(2026, 11, 20)


def test_repeated_overdue_observation_does_not_double_penalty(service, standard_loan):
    # 同一天重复“查询/推进”多次，罚息必须完全一致
    from app.overdue import apply_overdue_marks
    acc = service.get_account("L1")
    values = []
    for _ in range(5):
        apply_overdue_marks(acc, date(2026, 11, 30))
        values.append(total_penalty(acc, date(2026, 11, 30)))
    assert len(set(values)) == 1
    # 30-11 距起算日 11-05 共 26 天 * 17
    assert values[0] == 17 * 26


def test_file_backed_repository_persists_and_reloads(tmp_path):
    db = str(tmp_path / "loan.db")
    repo1 = AccountRepository(db)
    svc1 = LoanService(repo1)
    svc1.disburse(
        account_no="P1", principal_yuan="500.00", annual_rate="0.12",
        term_periods=2, disbursement_date=date(2026, 10, 1),
        first_due_date=date(2026, 11, 1), grace_days=0,
        penalty_daily_rate="0.0005",
    )
    svc1.repay("P1", "K1", "255.00", date(2026, 11, 1))
    repo1.conn().close()

    repo2 = AccountRepository(db)
    svc2 = LoanService(repo2)
    acc = svc2.get_account("P1")
    assert acc.status is AccountStatus.ACTIVE
    # 幂等键也持久化：重复流水号返回首次结果
    r = svc2.repay("P1", "K1", "255.00", date(2026, 11, 1))
    assert r.reference_no == "K1"
    assert len(acc.payments) == 1


def test_missing_account_distinguishable(service):
    with pytest.raises(Exception) as ei:
        service.get_account("NOPE")
    assert ei.value.code == "ACCOUNT_NOT_FOUND"


def test_recover_then_overdue_again_no_state_regression(service, standard_loan):
    """逾期 -> 结清当期恢复 -> 下一期再次逾期：不得有状态倒退或罚息污染。"""
    from app.overdue import is_overdue

    # 第 1 期逾期到 11-10，付清（罚息 102 + 利息 1000 + 本金 33333 = 344.35）
    r1 = service.repay("L1", "R10", "344.35", date(2026, 11, 10))
    assert sum(a.amount for a in r1.allocations) == 34435
    acc = service.get_account("L1")
    assert is_overdue(acc, date(2026, 11, 10)) is False

    # 第 2 期 12-01 到期后再次逾期（跨月）
    acc = service.get_account("L1")
    assert is_overdue(acc, date(2026, 12, 1)) is False
    assert is_overdue(acc, date(2026, 12, 5)) is True  # 起算日 12-05
    # 第 1 期历史不复活
    assert total_penalty(acc, date(2026, 12, 10)) == round(33333 * 0.0005) * 6

    # 付清第 2 期（罚息 102 + 利息 667 + 本金 33333 = 341.02）
    service.repay("L1", "R11", "341.02", date(2026, 12, 10))
    acc = service.get_account("L1")
    assert is_overdue(acc, date(2026, 12, 10)) is False
    # 第 3 期 12-31 到期，12-10 尚未到期
    assert not acc.installments[2].is_paid
