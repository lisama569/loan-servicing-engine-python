"""领域错误定义。

所有业务错误都带稳定的 ``code``，便于调用方区分失败原因；
``details`` 承载可解释的上下文（账户号、流水号、金额依据等）。
"""

from __future__ import annotations


class LoanEngineError(Exception):
    """贷款引擎错误基类。"""

    code: str = "ENGINE_ERROR"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        if self.details:
            return f"{self.code}: {self.message} | details={self.details}"
        return f"{self.code}: {self.message}"


class AccountNotFoundError(LoanEngineError):
    code = "ACCOUNT_NOT_FOUND"


class AccountAlreadyExistsError(LoanEngineError):
    code = "ACCOUNT_ALREADY_EXISTS"


class InvalidAmountError(LoanEngineError):
    code = "INVALID_AMOUNT"


class DuplicatePaymentError(LoanEngineError):
    """同一还款流水号重复提交。

    幂等语义：重复请求返回首次结果而不是报错，因此该异常只在
    流水号被不同还款内容复用时抛出。
    """

    code = "DUPLICATE_PAYMENT_CONFLICT"


class IllegalStateError(LoanEngineError):
    """账户状态不允许该操作（如对未放款账户还款、结清后再还）。"""

    code = "ILLEGAL_STATE"


class OverpaymentError(LoanEngineError):
    """普通还款金额超过当前应还总额（罚息+已到期利息+剩余本金）。"""

    code = "OVERPAYMENT"


class InsufficientPayoffError(LoanEngineError):
    """提前结清金额不足。"""

    code = "INSUFFICIENT_PAYOFF"


class StaleEventError(LoanEngineError):
    """迟到事件：业务日期早于账户已处理到的日期。"""

    code = "STALE_EVENT"
