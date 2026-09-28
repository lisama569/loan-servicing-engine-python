"""HTTP 接口冒烟：放款、还款、幂等、错误码区分。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app


@pytest.fixture
def client():
    return TestClient(create_app(":memory:"))


def _disburse(client, account_no="H1"):
    return client.post("/accounts/disburse", json={
        "account_no": account_no,
        "principal_yuan": "1000.00",
        "annual_rate": "0.12",
        "term_periods": 3,
        "disbursement_date": "2026-10-01",
        "first_due_date": "2026-11-01",
        "grace_days": 3,
        "penalty_daily_rate": "0.0005",
    })


def test_disburse_and_get(client):
    res = _disburse(client)
    assert res.status_code == 200
    assert res.json()["account"]["status"] == "ACTIVE"
    got = client.get("/accounts/H1")
    assert got.status_code == 200
    assert len(got.json()["account"]["installments"]) == 3


def test_repay_and_idempotent_replay(client):
    _disburse(client)
    payload = {
        "account_no": "H1", "reference_no": "W1",
        "amount_yuan": "343.33", "event_date": "2026-11-01",
    }
    r1 = client.post("/payments", json=payload)
    r2 = client.post("/payments", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    account = client.get("/accounts/H1").json()["account"]
    assert len([i for i in account["installments"] if i["principal_paid"]]) == 1


def test_error_codes_distinguishable(client):
    _disburse(client)
    # 超额
    r = client.post("/payments", json={
        "account_no": "H1", "reference_no": "W2",
        "amount_yuan": "9999.00", "event_date": "2026-11-01",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERPAYMENT"

    # 账户不存在
    r = client.post("/payments", json={
        "account_no": "MISSING", "reference_no": "W3",
        "amount_yuan": "10.00", "event_date": "2026-11-01",
    })
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

    # 非法金额
    r = client.post("/payments", json={
        "account_no": "H1", "reference_no": "W4",
        "amount_yuan": "0", "event_date": "2026-11-01",
    })
    assert r.json()["error"]["code"] == "INVALID_AMOUNT"


def test_view_reports_overdue_and_penalty(client):
    _disburse(client)
    client.post("/payments", json={
        "account_no": "H1", "reference_no": "W5",
        "amount_yuan": "100.00", "event_date": "2026-11-10",
    })
    res = client.get("/accounts/H1/view", params={"as_of": "2026-11-11"})
    view = res.json()["view"]
    assert view["overdue"] is True
    # 100 元 = 102 罚息 + 1000 利息 + 8898 本金；剩余本金 24435，
    # 次日罚息 round(24435*0.0005)=12
    assert view["penalty_cents"] == 12
