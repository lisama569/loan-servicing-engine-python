from loan_engine.errors import LoanError


def test_smoke():
    assert LoanError().code == "DOMAIN_ERROR"
