"""账户仓储：SQLite 持久化。

并发与一致性策略：
- 每个写操作在独立事务中执行 ``BEGIN IMMEDIATE``，先拿到写锁再读账户，
  同一账户的并发还款在 SQLite 层串行化，杜绝读后写丢失更新；
- 还款流水号 ``reference_no`` 建有 UNIQUE 约束；重复提交由
  :meth:`find_payment` 直接返回首次结果（幂等），流水号被不同内容复用则拒绝；
- 账户聚合整体快照与幂等键在同一事务内落库，失败即回滚，不留部分更新。

注：默认内存库为进程内演示/测试使用，文件库（``app.db``）可持久化。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date

from app.errors import AccountNotFoundError, DuplicatePaymentError
from app.models import (
    Account,
    AccountStatus,
    Installment,
    LedgerEntry,
    PaymentRecord,
    PaymentType,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_no TEXT PRIMARY KEY,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    reference_no TEXT PRIMARY KEY,
    account_no   TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    event_date   TEXT NOT NULL,
    payment_type TEXT NOT NULL,
    payload      TEXT NOT NULL
);
"""


def _default(o: object):
    if isinstance(o, date):
        return {"__date__": o.isoformat()}
    raise TypeError(f"不可序列化: {type(o)!r}")


def _hook(d: dict):
    if "__date__" in d:
        return date.fromisoformat(d["__date__"])
    return d


def _account_to_dict(a: Account) -> dict:
    return {
        "account_no": a.account_no,
        "status": a.status.value,
        "principal_amount": a.principal_amount,
        "annual_rate": a.annual_rate,
        "term_periods": a.term_periods,
        "disbursement_date": a.disbursement_date,
        "first_due_date": a.first_due_date,
        "grace_days": a.grace_days,
        "penalty_daily_rate": a.penalty_daily_rate,
        "installments": [vars(i) for i in a.installments],
        "payments": [
            {
                "reference_no": p.reference_no,
                "payment_type": p.payment_type.value,
                "amount": p.amount,
                "event_date": p.event_date,
                "allocations": [vars(x) for x in p.allocations],
                "waived_interest": p.waived_interest,
                "resulting_status": p.resulting_status.value,
            }
            for p in a.payments
        ],
        "last_event_date": a.last_event_date,
    }


def _account_from_dict(d: dict) -> Account:
    installments = [Installment(**i) for i in d["installments"]]
    payments = [
        PaymentRecord(
            reference_no=p["reference_no"],
            payment_type=PaymentType(p["payment_type"]),
            amount=p["amount"],
            event_date=p["event_date"],
            allocations=[LedgerEntry(**x) for x in p["allocations"]],
            waived_interest=p["waived_interest"],
            resulting_status=AccountStatus(p["resulting_status"]),
        )
        for p in d["payments"]
    ]
    return Account(
        account_no=d["account_no"],
        status=AccountStatus(d["status"]),
        principal_amount=d["principal_amount"],
        annual_rate=d["annual_rate"],
        term_periods=d["term_periods"],
        disbursement_date=d["disbursement_date"],
        first_due_date=d["first_due_date"],
        grace_days=d["grace_days"],
        penalty_daily_rate=d["penalty_daily_rate"],
        installments=installments,
        payments=payments,
        last_event_date=d["last_event_date"],
    )


class AccountRepository:
    """SQLite 账户仓储。单连接 + BEGIN IMMEDIATE 串行化写事务。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def run_in_transaction(self, fn):
        """在 IMMEDIATE 事务中执行 fn(conn)，异常回滚并原样抛出。"""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

    def save(self, account: Account, conn: sqlite3.Connection | None = None) -> None:
        payload = json.dumps(_account_to_dict(account), default=_default)
        target = conn or self._conn
        target.execute(
            "INSERT INTO accounts(account_no, payload) VALUES(?, ?) "
            "ON CONFLICT(account_no) DO UPDATE SET payload=excluded.payload",
            (account.account_no, payload),
        )

    def get(self, account_no: str, conn: sqlite3.Connection | None = None) -> Account:
        account = self.find(account_no, conn)
        if account is None:
            raise AccountNotFoundError("账户不存在", account_no=account_no)
        return account

    def find(self, account_no: str, conn: sqlite3.Connection | None = None) -> Account | None:
        target = conn or self._conn
        row = target.execute(
            "SELECT payload FROM accounts WHERE account_no=?", (account_no,)
        ).fetchone()
        return _account_from_dict(json.loads(row[0], object_hook=_hook)) if row else None

    def insert_payment(
        self, record: PaymentRecord, account_no: str, conn: sqlite3.Connection
    ) -> None:
        """事务内登记幂等键。流水号冲突抛 DuplicatePaymentError。"""
        try:
            conn.execute(
                "INSERT INTO payments(reference_no, account_no, amount, event_date, "
                "payment_type, payload) VALUES(?,?,?,?,?,?)",
                (
                    record.reference_no,
                    account_no,
                    record.amount,
                    record.event_date.isoformat(),
                    record.payment_type.value,
                    json.dumps(
                        {
                            "allocations": [vars(x) for x in record.allocations],
                            "waived_interest": record.waived_interest,
                            "resulting_status": record.resulting_status.value,
                        },
                        default=_default,
                    ),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicatePaymentError(
                "还款流水号已存在但请求内容不一致",
                reference_no=record.reference_no,
            ) from exc

    def find_payment(
        self, reference_no: str, conn: sqlite3.Connection
    ) -> tuple[PaymentRecord, str] | None:
        """按流水号查找首次成功还款，返回 (记录, 账户号)；不存在返回 None。"""
        row = conn.execute(
            "SELECT account_no, amount, event_date, payment_type, payload "
            "FROM payments WHERE reference_no=?",
            (reference_no,),
        ).fetchone()
        if row is None:
            return None
        account_no, amount, event_date, ptype, payload = row
        d = json.loads(payload, object_hook=_hook)
        record = PaymentRecord(
            reference_no=reference_no,
            payment_type=PaymentType(ptype),
            amount=amount,
            event_date=date.fromisoformat(event_date),
            allocations=[LedgerEntry(**x) for x in d["allocations"]],
            waived_interest=d["waived_interest"],
            resulting_status=AccountStatus(d["resulting_status"]),
        )
        return record, account_no
