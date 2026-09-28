"""按期还款、部分还款、提前还本、提前结清、超额与异常状态。"""

from __future__ import annotations

from datetime import date

import pytest

from app.errors import (
    IllegalStateError,
    InsufficientPayoffError,
    InvalidAmountError,
    OverpaymentError,
    StaleEventError,
)
from app.models import AccountStatus, PaymentType
from app.penalty import total_penalty


def test_normal_on_time_installment(service, standard_loan):
    # 到期日当天按期足额：333.33 本金 + 10.00 利息 = 343.33
    record = service.repay("L1", "T1", "343.33", date(2026, 11, 1))
    kinds = [(a.period_no, a.kind, a.amount) for a in record.allocations]
    assert (1, "INTEREST", 1000) in kinds
    assert (1, "PRINCIPAL", 33333) in kinds
    acc = service.get_account("L1")
    assert acc.installments[0].is_paid
    assert acc.status is AccountStatus.ACTIVE
    # 无罚息
    assert total_penalty(acc, date(2026, 11, 1)) == 0


def test_partial_repayment_waterfall_order(service, standard_loan, caplog):
    # 逾期到 11-10：罚息 102；还 200 元
    record = service.repay("L1", "T2", "200.00", date(2026, 11, 10))
    seq = [(a.kind, a.amount) for a in record.allocations]
    # 顺序必须是 罚息 -> 利息 -> 本金，金额守恒
    assert seq == [("PENALTY", 102), ("INTEREST", 1000), ("PRINCIPAL", 18898)]
    assert sum(a.amount for a in record.allocations) == 20000
    acc = service.get_account("L1")
    assert acc.installments[0].penalty_paid == 102
    assert acc.installments[0].interest_paid == 1000
    assert acc.installments[0].principal_paid == 18898
    # 次日罚息按剩余本金 14435 计算 -> round(14435*0.0005)=7 分
    assert total_penalty(acc, date(2026, 11, 11)) == 7
    # 日志包含输入与金额依据
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "应计罚息=102分" in text
    assert "资金" in text or "分配" in text


def test_partial_then_penalty_history_not_lost(service, standard_loan):
    service.repay("L1", "T3", "200.00", date(2026, 11, 10))
    # 到 11-20：锁基线 102(截至11-10)，之后 14435 本金从 11-11 起 10 天 * 7 = 70
    acc = service.get_account("L1")
    assert total_penalty(acc, date(2026, 11, 20)) == 70
    # 再还一笔只够罚息+部分 -> 70 罚息被付掉
    r = service.repay("L1", "T4", "0.70", date(2026, 11, 20))
    assert [(a.kind, a.amount) for a in r.allocations] == [("PENALTY", 70)]


def test_prepay_future_principal_keeps_interest(service, standard_loan):
    # 未到期时先把未来本金还掉一部分（走到瀑布第 4 档）
    r = service.repay("L1", "T5", "500.00", date(2026, 10, 15))
    kinds = [(a.period_no, a.kind) for a in r.allocations]
    assert all(k == "PRINCIPAL" for _, k in kinds)
    acc = service.get_account("L1")
    # 50000 分全部冲抵本金：1 期 33333 + 2 期 16667
    assert acc.installments[0].principal_paid == 33333
    assert acc.installments[1].principal_paid == 16667
    # 未到期利息不减免
    assert acc.installments[0].interest_balance == 1000


def test_early_settlement_waives_future_interest(service, standard_loan):
    r = service.repay("L1", "T6", "1000.00", date(2026, 10, 15),
                      PaymentType.EARLY_SETTLEMENT)
    assert r.waived_interest == 2000  # 667 + 333
    assert r.resulting_status is AccountStatus.SETTLED
    acc = service.get_account("L1")
    assert acc.status is AccountStatus.SETTLED
    assert all(i.principal_balance == 0 and i.interest_balance == 0
               for i in acc.installments)
    # 总账守恒：实付总额 = 100000
    assert sum(p.amount for p in acc.payments) == 100000


def test_natural_settlement_through_three_installments(service, standard_loan):
    amounts = ["343.33", "340.00", "336.67"]
    dates = [date(2026, 11, 1), date(2026, 12, 1), date(2026, 12, 31)]
    for ref, amt, d in zip(["N1", "N2", "N3"], amounts, dates):
        service.repay("L1", ref, amt, d)
    acc = service.get_account("L1")
    assert acc.status is AccountStatus.SETTLED
    # 本金守恒、利息守恒
    assert sum(i.principal_paid for i in acc.installments) == 100000
    assert sum(i.interest_paid for i in acc.installments) == 2000


def test_overpayment_rejected_with_distinguishable_reason(service, standard_loan):
    with pytest.raises(OverpaymentError) as ei:
        service.repay("L1", "O1", "9999.00", date(2026, 11, 1))
    assert ei.value.code == "OVERPAYMENT"
    assert ei.value.details["excess"] > 0
    # 拒绝后账户余额不变（无部分更新）
    acc = service.get_account("L1")
    assert sum(i.principal_paid for i in acc.installments) == 0
    assert len(acc.payments) == 0


def test_repay_settled_account_rejected(service, standard_loan):
    service.repay("L1", "S1", "1000.00", date(2026, 10, 15),
                  PaymentType.EARLY_SETTLEMENT)
    with pytest.raises(IllegalStateError) as ei:
        service.repay("L1", "S2", "10.00", date(2026, 10, 16))
    assert ei.value.code == "ILLEGAL_STATE"


def test_stale_event_rejected(service, standard_loan):
    service.repay("L1", "D1", "100.00", date(2026, 11, 5))
    with pytest.raises(StaleEventError) as ei:
        service.repay("L1", "D2", "10.00", date(2026, 11, 4))
    assert ei.value.code == "STALE_EVENT"
    # 迟到事件不得入账
    assert len(service.get_account("L1").payments) == 1


def test_invalid_amount_rejected(service, standard_loan):
    with pytest.raises(InvalidAmountError):
        service.repay("L1", "I1", "0", date(2026, 11, 1))


def test_early_settlement_shortfall_rejected(service, standard_loan):
    with pytest.raises(InsufficientPayoffError) as ei:
        service.repay("L1", "I2", "999.00", date(2026, 10, 15),
                      PaymentType.EARLY_SETTLEMENT)
    assert ei.value.code == "INSUFFICIENT_PAYOFF"
    assert ei.value.details["shortfall"] == 100
