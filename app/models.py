"""领域模型与枚举定义。

金额一律以 ``int`` 类型的**分**存储与计算，杜绝浮点误差；
对外接口用十进制字符串（元）表达金额。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date


class AccountStatus(str, enum.Enum):
    """贷款账户状态机。

    状态流转::

        OPEN -> ACTIVE -> SETTLED
                  |
                  v
              (无独立 OVERDUE 态：逾期是 ACTIVE 账户的派生标记，
               每期账单有自身的 overdue 标记，罚息按日确定性重算)
    """

    OPEN = "OPEN"          # 已开户未放款
    ACTIVE = "ACTIVE"      # 已放款，还款中（可能同时处于逾期）
    SETTLED = "SETTLED"    # 本息罚息全部结清，终态


class PaymentType(str, enum.Enum):
    REPAY = "REPAY"                    # 普通还款（按期/部分/提前还本）
    EARLY_SETTLEMENT = "EARLY_SETTLEMENT"  # 提前结清一次性付款


@dataclass
class Installment:
    """一期账单（等额本金）。"""

    period_no: int
    due_date: date
    principal_due: int          # 分，本期应还本金
    interest_due: int           # 分，本期应还利息（提前还本时按规则减免）
    principal_paid: int = 0
    interest_paid: int = 0
    penalty_paid: int = 0       # 该期已付罚息
    # 逾期期数在发生本金偿还时，把截至该日的应计罚息“锁定”为历史基线，
    # 之后只对剩余本金继续累计；保证部分还本不会抹掉历史罚息。
    # 罚息起算日本身是派生值（due_date + grace_days + 1），不持久化。
    penalty_base: int = 0
    penalty_base_date: date | None = None

    @property
    def principal_balance(self) -> int:
        return self.principal_due - self.principal_paid

    @property
    def interest_balance(self) -> int:
        return self.interest_due - self.interest_paid

    @property
    def is_paid(self) -> bool:
        return self.principal_balance == 0 and self.interest_balance == 0


@dataclass
class LedgerEntry:
    """单笔付款的一条分配明细，构成可追踪的金额分配轨迹。"""

    period_no: int
    kind: str          # PENALTY / INTEREST / PRINCIPAL
    amount: int        # 分
    reason: str        # 分配依据的文字说明


@dataclass
class PaymentRecord:
    """一次成功还款（幂等记录）。"""

    reference_no: str
    payment_type: PaymentType
    amount: int                    # 分
    event_date: date
    allocations: list[LedgerEntry] = field(default_factory=list)
    waived_interest: int = 0       # 提前结清减免的未来利息
    resulting_status: AccountStatus = AccountStatus.ACTIVE


@dataclass
class Account:
    """贷款账户聚合根（内存表示）。"""

    account_no: str
    status: AccountStatus = AccountStatus.OPEN

    # 放款参数
    principal_amount: int = 0          # 分，合同本金
    annual_rate: str = "0.0"           # 年化利率，十进制字符串
    term_periods: int = 0              # 总期数
    disbursement_date: date | None = None
    first_due_date: date | None = None
    grace_days: int = 0
    penalty_daily_rate: str = "0.0"    # 逾期本金日罚息率

    # 账务
    installments: list[Installment] = field(default_factory=list)
    payments: list[PaymentRecord] = field(default_factory=list)
    last_event_date: date | None = None   # 已处理到的最晚业务日期
