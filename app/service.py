"""贷款领域服务：开户/放款、还款（普通/提前结清）、查询。

每个写请求的处理顺序（全部在同一 IMMEDIATE 事务内）：

1. 查幂等键：流水号已存在 -> 同参数返回首次结果（幂等），参数不一致则拒绝；
2. 读账户并做状态校验（未放款/已结清拒绝，原因可区分）；
3. 校验业务日期不早于已处理日期（拒绝迟到事件，防止状态倒退/重复罚息）；
4. 按 event_date 确定性推进逾期标记（重算，不增量累加）；
5. 资金瀑布分配（罚息 -> 已到期利息 -> 已到期本金 -> 未到期本金）；
6. 判定结清、登记幂等键、整体快照落库；任何一步失败整体回滚。
"""

from __future__ import annotations

import logging
from datetime import date

from app.allocation import allocate_early_settlement, allocate_repayment, payable_summary
from app.errors import (
    AccountAlreadyExistsError,
    IllegalStateError,
    StaleEventError,
)
from app.models import Account, AccountStatus, PaymentRecord, PaymentType
from app.money import cents_to_yuan, yuan_to_cents
from app.overdue import apply_overdue_marks, is_overdue
from app.penalty import total_penalty
from app.schedule import generate_installments

logger = logging.getLogger("loan_engine")


class LoanService:
    def __init__(self, repository) -> None:
        self.repo = repository

    # ------------------------------------------------------------------
    def disburse(
        self,
        account_no: str,
        principal_yuan: str,
        annual_rate: str,
        term_periods: int,
        disbursement_date: date,
        first_due_date: date,
        grace_days: int = 0,
        penalty_daily_rate: str = "0",
        period_days: int = 30,
    ) -> Account:
        """开户并放款：生成等额本金计划，账户 OPEN -> ACTIVE。"""
        principal = yuan_to_cents(principal_yuan)

        def _do(conn):
            if self.repo.find(account_no, conn) is not None:
                raise AccountAlreadyExistsError("账户已存在，禁止重复放款", account_no=account_no)
            installments = generate_installments(
                principal, annual_rate, term_periods, first_due_date, period_days
            )
            account = Account(
                account_no=account_no,
                status=AccountStatus.ACTIVE,
                principal_amount=principal,
                annual_rate=annual_rate,
                term_periods=term_periods,
                disbursement_date=disbursement_date,
                first_due_date=first_due_date,
                grace_days=grace_days,
                penalty_daily_rate=penalty_daily_rate,
                installments=installments,
                last_event_date=disbursement_date,
            )
            self.repo.save(account, conn)
            logger.info(
                "放款 account=%s principal=%s分(%s元) rate=%s periods=%d first_due=%s grace=%d",
                account_no, principal, cents_to_yuan(principal), annual_rate,
                term_periods, first_due_date, grace_days,
            )
            return self.repo.get(account_no, conn)

        return self.repo.run_in_transaction(_do)

    # ------------------------------------------------------------------
    def repay(
        self,
        account_no: str,
        reference_no: str,
        amount_yuan: str,
        event_date: date,
        payment_type: PaymentType = PaymentType.REPAY,
    ) -> PaymentRecord:
        """提交一笔还款。重复流水号幂等返回首次结果。"""
        amount = yuan_to_cents(amount_yuan)
        logger.info(
            "收到还款请求 account=%s ref=%s type=%s amount=%s分(%s元) event_date=%s",
            account_no, reference_no, payment_type.value, amount,
            cents_to_yuan(amount), event_date,
        )

        def _do(conn) -> PaymentRecord:
            # 1) 幂等
            existing = self.repo.find_payment(reference_no, conn)
            if existing is not None:
                record, ref_account = existing
                if ref_account != account_no or record.amount != amount \
                        or record.payment_type != payment_type \
                        or record.event_date != event_date:
                    from app.errors import DuplicatePaymentError
                    raise DuplicatePaymentError(
                        "流水号已被另一条还款占用",
                        reference_no=reference_no,
                        existing_account=ref_account,
                    )
                logger.info("幂等命中 ref=%s -> 直接返回首次结果，不重复入账", reference_no)
                return record

            # 2) 读账户 + 状态校验
            account = self.repo.get(account_no, conn)
            if account.status is AccountStatus.OPEN:
                raise IllegalStateError("账户尚未放款，不能还款", account_no=account_no)
            if account.status is AccountStatus.SETTLED:
                raise IllegalStateError("账户已结清，拒绝再次还款", account_no=account_no)

            # 3) 迟到事件
            if account.last_event_date is not None and event_date < account.last_event_date:
                raise StaleEventError(
                    "还款业务日期早于账户已处理日期（迟到事件）",
                    event_date=event_date,
                    last_event_date=account.last_event_date,
                )

            before = self._snapshot_balances(account, event_date)
            # 4) 逾期推进
            marks = apply_overdue_marks(account, event_date)
            for note in marks:
                logger.info("逾期推进 account=%s %s", account_no, note)
            penalty_before = total_penalty(account, event_date)
            summary = payable_summary(account, event_date)
            logger.info(
                "入账前依据 account=%s date=%s 应计罚息=%s分 已到期利息=%s分 "
                "已到期本金=%s分 未来本金=%s分 逾期中=%s",
                account_no, event_date, penalty_before,
                summary["matured_interest"], summary["matured_principal"],
                summary["future_principal"], is_overdue(account, event_date),
            )

            # 5) 分配
            if payment_type is PaymentType.EARLY_SETTLEMENT:
                allocations, waived = allocate_early_settlement(account, amount, event_date)
            else:
                allocations = allocate_repayment(account, amount, event_date)
                waived = 0
            for al in allocations:
                logger.info(
                    "分配 account=%s ref=%s 第%s期 %s %s分 | %s",
                    account_no, reference_no, al.period_no, al.kind, al.amount, al.reason,
                )
            if waived:
                logger.info("提前结清减免未来利息 account=%s 共%d分", account_no, waived)

            # 6) 结清判定与落库
            settled = self._is_settled(account)
            account.last_event_date = event_date
            if settled:
                account.status = AccountStatus.SETTLED
            record = PaymentRecord(
                reference_no=reference_no,
                payment_type=payment_type,
                amount=amount,
                event_date=event_date,
                allocations=allocations,
                waived_interest=waived,
                resulting_status=account.status,
            )
            account.payments.append(record)
            self.repo.insert_payment(record, account_no, conn)
            self.repo.save(account, conn)

            after = self._snapshot_balances(account, event_date)
            logger.info(
                "入账完成 account=%s ref=%s 状态=%s 本金%s->%s分 利息%s->%s分 罚息%s->%s分",
                account_no, reference_no, account.status.value,
                before["principal"], after["principal"],
                before["interest"], after["interest"],
                before["penalty"], after["penalty"],
            )
            return record

        return self.repo.run_in_transaction(_do)

    # ------------------------------------------------------------------
    def get_account(self, account_no: str) -> Account:
        return self.repo.get(account_no)

    def account_view(self, account_no: str, as_of: date) -> dict:
        """可解释的账户视图：状态、逾期标记、各项余额、应还汇总。"""
        account = self.repo.get(account_no)
        summary = payable_summary(account, as_of)
        return {
            "account_no": account_no,
            "status": account.status.value,
            "as_of": as_of.isoformat(),
            "overdue": is_overdue(account, as_of),
            "penalty_cents": total_penalty(account, as_of),
            "balances_cents": self._snapshot_balances(account, as_of),
            "payable_cents": summary,
        }

    @staticmethod
    def _snapshot_balances(account: Account, as_of: date | None = None) -> dict[str, int]:
        return {
            "principal": sum(i.principal_balance for i in account.installments),
            "interest": sum(i.interest_balance for i in account.installments),
            "penalty": (
                total_penalty(account, as_of)
                if as_of is not None
                else sum(i.penalty_paid for i in account.installments)
            ),
        }

    @staticmethod
    def _is_settled(account: Account) -> bool:
        """结清 = 所有期数本金与利息余额为 0（罚息在瀑布中先行清偿）。"""
        return all(
            i.principal_balance == 0 and i.interest_balance == 0
            for i in account.installments
        )
