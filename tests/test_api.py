"""HTTP layer: origination, idempotent payment API, error codes, audit events."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from loan_engine.api import create_app
from loan_engine.service import LoanService


@pytest.fixture
def client():
    return TestClient(create_app(LoanService()))


def _originate(client, loan_id="L1"):
    resp = client.post("/loans", json={
        "loan_id": loan_id, "principal": "1200.00", "annual_rate": "0.12",
        "term_months": 3, "disbursement_date": "2026-01-15", "grace_days": 3})
    assert resp.status_code == 201, resp.text


def test_originate_pay_duplicate_and_audit_trail(client):
    _originate(client)
    payload = {"payment_id": "P1", "amount": "412.00",
               "value_date": "2026-02-15", "kind": "NORMAL"}
    r1 = client.post("/loans/L1/payments", json=payload)
    r2 = client.post("/loans/L1/payments", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["duplicate"] is False and b2["duplicate"] is True
    assert b2["status_transition"] == "OPEN -> NORMAL"
    assert b2["balances"]["principal"] == ["1200.00", "800.00"]
    assert b2["allocations"][0]["to_interest"] == "12.00"
    assert b2["allocations"][0]["to_principal"] == "400.00"

    events = client.get("/loans/L1/events").json()["events"]
    types = [(e["type"], e["event_id"]) for e in events]
    assert types[0] == ("DISBURSEMENT", "L1:DISBURSEMENT")
    assert sum(1 for t, _ in types if t == "PAYMENT") == 1


def test_overpayment_returns_distinguishable_code(client):
    _originate(client)
    resp = client.post("/loans/L1/payments", json={
        "payment_id": "BIG", "amount": "9999.00",
        "value_date": "2026-02-15"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "PAYMENT_EXCEEDS_BALANCE"


def test_loan_not_found_is_404(client):
    resp = client.get("/loans/NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"] == "LOAN_NOT_FOUND"


def test_snapshot_and_payoff_quote(client):
    _originate(client)
    snap = client.get("/loans/L1/snapshot", params={"as_of": "2026-02-20"}).json()
    # grace end 02-18, 2 penalty days on 400 = 0.40
    assert snap["penalty_balance"] == "0.40"
    assert snap["status"] == "OVERDUE"

    quote = client.get("/loans/L1/payoff-quote",
                       params={"as_of": "2026-02-01"}).json()
    assert quote["payoff_amount"] == "1200.00"
