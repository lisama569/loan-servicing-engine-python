"""Shared fixtures and structured test logging.

Every test logs: input parameters, state transitions and the numeric
basis of money calculations (before/after component balances).
"""

from __future__ import annotations

import logging
import sys

import pytest

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


@pytest.fixture
def log():
    return logging.getLogger("loan_test")
