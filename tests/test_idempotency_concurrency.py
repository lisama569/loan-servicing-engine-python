"""幂等保护、重复提交、并发还款与总账守恒。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from app.errors import DuplicatePaymentError
from app.models import PaymentRecord


def test_duplicate_submission_applies_once(service, standard_loan):
    kwargs = dict(
        account_no="L1", reference_no="DUP1", amount_yuan="343.33",
        event_date=date(2026, 11, 1),
    )
    r1 = service.repay(**kwargs)
    r2 = service.repay(**kwargs)
    r3 = service.repay(**kwargs)
    assert r1.reference_no == r2.reference_no == r3.reference_no
    acc = service.get_account("L1")
    # 只入账一次
    assert len(acc.payments) == 1
    assert sum(i.principal_paid for i in acc.installments) == 33333


def test_same_reference_different_content_rejected(service, standard_loan):
    service.repay("L1", "X1", "100.00", date(2026, 11, 1))
    try:
        service.repay("L1", "X1", "200.00", date(2026, 11, 1))
    except DuplicatePaymentError as e:
        assert e.code == "DUPLICATE_PAYMENT_CONFLICT"
    else:  # pragma: no cover
        raise AssertionError("应当拒绝流水号复用")
    # 首次入账不受影响
    acc = service.get_account("L1")
    assert len(acc.payments) == 1
    assert acc.payments[0].amount == 10000


def test_concurrent_repayments_are_serialized_and_conserved(service, standard_loan):
    # 10 笔各 100 元，对同一账户并发提交，各带独立流水号
    refs = [f"C{i}" for i in range(10)]

    def _submit(ref: str):
        return service.repay("L1", ref, "100.00", date(2026, 11, 5))

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_submit, refs))

    assert all(isinstance(r, PaymentRecord) for r in results)
    acc = service.get_account("L1")
    # 十笔全部成功且只入账一次
    assert len(acc.payments) == 10
    total_paid = sum(p.amount for p in acc.payments)
    assert total_paid == 100000
    # 账户侧本金+利息（无罚息：11-05 在 1 期宽限期最后一天前？起算日 11-05）
    # 11-01 到期 + 3 天宽限 => 11-05 为罚息起算日，存在 1 天罚息
    principal_paid = sum(i.principal_paid for i in acc.installments)
    interest_paid = sum(i.interest_paid for i in acc.installments)
    penalty_paid = sum(i.penalty_paid for i in acc.installments)
    assert principal_paid + interest_paid + penalty_paid == total_paid
    # 至少覆盖完到期利息 1000 与大部分本金
    assert interest_paid == 1000
    assert principal_paid == 100000 - 1000 - penalty_paid


def test_concurrent_same_reference_only_one_wins(service, standard_loan):
    def _submit(_):
        return service.repay("L1", "SAME", "100.00", date(2026, 11, 1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_submit, range(8)))

    # 所有线程得到同一条幂等结果，只入账一次
    assert len({r.reference_no for r in results}) == 1
    acc = service.get_account("L1")
    assert len(acc.payments) == 1
    assert acc.payments[0].amount == 10000


def test_failed_operation_leaves_no_partial_update(service, standard_loan):
    # 超额还款必然失败
    try:
        service.repay("L1", "F1", "9999.00", date(2026, 11, 5))
    except Exception:
        pass
    acc = service.get_account("L1")
    assert len(acc.payments) == 0
    assert sum(i.principal_paid for i in acc.installments) == 0
    # 逾期标记推进发生在失败前，但它是确定性派生操作：
    # 下次合法请求重放得到同样结果，不影响金额
    service.repay("L1", "F2", "50.00", date(2026, 11, 5))
    acc2 = service.get_account("L1")
    # 50 元瀑布：17 罚息 + 1000 利息 + 3983 本金 = 5000
    paid_penalty = sum(i.penalty_paid for i in acc2.installments)
    paid_interest = sum(i.interest_paid for i in acc2.installments)
    paid_principal = sum(i.principal_paid for i in acc2.installments)
    assert (paid_penalty, paid_interest, paid_principal) == (17, 1000, 3983)
    assert paid_penalty + paid_interest + paid_principal == 5000
    assert len(acc2.payments) == 1


def test_concurrent_mixed_success_and_overpayment_conserved(service, standard_loan):
    """并发提交：9 笔 100 元正常 + 1 笔超额 9999 元；成功者守恒，失败者无副作用。"""
    from app.errors import OverpaymentError

    calls = [(f"M{i}", "100.00") for i in range(9)] + [("MBAD", "9999.00")]

    def _submit(item):
        ref, amount = item
        try:
            service.repay("L1", ref, amount, date(2026, 11, 5))
            return True
        except OverpaymentError:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(_submit, calls))

    assert outcomes.count(True) == 9
    assert outcomes.count(False) == 1
    acc = service.get_account("L1")
    assert len(acc.payments) == 9
    total_paid = sum(p.amount for p in acc.payments)
    assert total_paid == 90000
    paid = (
        sum(i.principal_paid for i in acc.installments)
        + sum(i.interest_paid for i in acc.installments)
        + sum(i.penalty_paid for i in acc.installments)
    )
    assert paid == total_paid
