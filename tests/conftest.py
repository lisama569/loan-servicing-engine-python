"""测试公共夹具。

- 每个用例使用独立内存仓储，互不干扰；
- 打开 loan_engine 日志，打印输入、状态迁移与金额计算依据（caplog 会捕获并显示）。
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.repository import AccountRepository
from app.service import LoanService


@pytest.fixture
def service():
    return LoanService(AccountRepository())


@pytest.fixture(autouse=True)
def _engine_logs(caplog):
    caplog.set_level(logging.INFO, logger="loan_engine")
    yield


def make_standard_loan(service, account_no="L1", *, disburse=date(2026, 10, 1)):
    """1000 元、12% 年化、3 期等额本金、每月 1 日到期、3 天宽限、日罚息 0.05%。"""
    return service.disburse(
        account_no=account_no,
        principal_yuan="1000.00",
        annual_rate="0.12",
        term_periods=3,
        disbursement_date=disburse,
        first_due_date=date(2026, 11, 1),
        grace_days=3,
        penalty_daily_rate="0.0005",
    )


@pytest.fixture
def standard_loan(service):
    return make_standard_loan(service)
