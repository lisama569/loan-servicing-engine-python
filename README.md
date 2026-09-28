# 本地贷款还款与逾期处理引擎

一套本地运行的贷款账户服务，覆盖**放款 → 按期/部分/提前还款 → 逾期与罚息 → 结清**
的完整生命周期，并在重复请求、并发请求与日期边界下给出一致、可解释、可复现的结果。

- 金额：内部一律以 **整数分** 计算，接口用十进制字符串（元），四舍五入 `ROUND_HALF_UP`
- 计息：等额本金；罚息按逾期本金日累加，**确定性重算、无增量状态**
- 并发：SQLite `BEGIN IMMEDIATE` 事务串行化同账户写操作
- 幂等：还款流水号唯一约束，重复提交只生效一次

## 目录结构

```
app/
  errors.py       # 错误码体系（失败原因可区分）
  models.py       # Account / Installment / PaymentRecord / LedgerEntry
  money.py        # 元<->分、ROUND_HALF_UP
  schedule.py     # 等额本金计划生成、提前结清利息减免
  penalty.py      # 罚息派生计算与历史基线锁定
  overdue.py      # 逾期判定（纯派生）
  allocation.py   # 还款资金瀑布
  repository.py   # SQLite 仓储 + 事务 + 幂等键
  service.py      # 领域服务（编排，结构化日志）
  schemas.py      # HTTP 请求模型
  api.py          # FastAPI 路由
tests/            # pytest 场景测试
main.py           # uvicorn 入口
```

## 1. 账户状态机

```
            放款                 全部本息（含罚息）清偿
  (无账户) ──────► ACTIVE ───────────────────────────► SETTLED（终态）
                    │  ▲                               
                    │  └──────── 逾期是 ACTIVE 的派生标记，无独立状态
                    ▼
              （逾期中 = 存在跨宽限期未清偿的账单）
```

- `ACTIVE` 是唯一的还款中状态；**逾期不是独立状态**，而是任意日期都能由
  “到期日 + 宽限期 + 未偿余额”重新算出的派生标记，因此不存在状态倒退问题。
- 结清判定：所有期数 `principal_balance == 0 且 interest_balance == 0`
  （资金瀑布保证走到结清时罚息已先行付清）。
- 非法操作拒绝（错误码可区分）：
  - `ACCOUNT_NOT_FOUND` / `ACCOUNT_ALREADY_EXISTS`
  - `ILLEGAL_STATE`：账户不存在/未放款不能还；已结清再还
  - `OVERPAYMENT`：还款额超过应还总额；`INSUFFICIENT_PAYOFF`：提前结清金额不足
  - `STALE_EVENT`：还款业务日期早于账户已处理日期（迟到事件）
  - `INVALID_AMOUNT`：非正数、超过两位小数、非法数字
  - `DUPLICATE_PAYMENT_CONFLICT`：流水号被不同还款内容复用

## 2. 还款计划与金额分配

### 等额本金计划
- 每期本金 = 本金 / 期数，余数并入末期（本金严格守恒）
- 每期利息 = 期初剩余本金 × 年利率 × 周期天数 / 360（默认 30 天周期，按分四舍五入）

### 普通还款（REPAY）资金瀑布
严格按以下顺序分配，每一步都生成带期号、科目、金额与依据的 `LedgerEntry`：

1. 截至业务日全部应计未付**罚息**（按期号升序）
2. 最早已到期账单的**利息**
3. 最早已到期账单的**本金**
4. 剩余金额提前偿还最早未到期账单的**本金**
   （未到期利息不减免；需免未来利息请用提前结清）

分配后入账金额 = 各科目分配之和，逐笔可追踪。部分还款允许；超过
“罚息 + 已到期利息 + 全部剩余本金”的部分按 `OVERPAYMENT` 拒绝。

### 提前结清（EARLY_SETTLEMENT）
- 应付金额必须**恰好** = 罚息 + 已到期利息 + 全部剩余本金；
  不足 → `INSUFFICIENT_PAYOFF`，超出 → `OVERPAYMENT`
- 所有 `due_date > 业务日` 的未到期利息全额**减免**（`INTEREST_WAIVED` 明细可查）

## 3. 逾期判定、罚息与恢复规则

- 到期日 `D`、宽限期 `g`：
  - `D ~ D+g`：宽限期，**无罚息、不算逾期**；
  - 罚息起算日 = `D + g + 1`（含），当日起算 1 天。
- 日罚息 = 当日逾期本金 × `penalty_daily_rate`，每日金额四舍五入到分后按自然日累加；
  跨月、跨年只按日期差计算，结果稳定可复现。
- **不保存任何增量罚息流水**：每次都从 `(到期日, 宽限期, 逾期本金, 锁定基线)`
  确定性重算。补缴、重复事件、迟到事件在模型层面就不可能造成重复罚息。
- **部分还本的历史锁定**：当某期罚息被全额付清且发生本金减少时，
  把截至还款日的应计罚息固化为 `penalty_base`，之后仅对剩余本金继续累计；
  本金清零后不再产生新罚息，历史欠息仍可查可收。
- 某期本金与利息清偿后，账户级逾期标记自动消失（派生）；
  下一期到期未还可再次进入逾期，历史期数的罚息不会“复活”。

## 4. 并发与幂等

- 每个写请求在单个 `BEGIN IMMEDIATE` 事务内完成：
  **先取写锁 → 幂等检查 → 读账户 → 校验 → 计算 → 落库**，杜绝读后写丢失更新；
  任何一步失败整体回滚，不留部分更新。
- `payments.reference_no` 为 UNIQUE：
  - 同流水号 + 同参数重复提交 → 直接返回**首次结果**，不重复入账；
  - 同流水号 + 不同金额/类型/日期/账户 → `DUPLICATE_PAYMENT_CONFLICT`。
- 还款业务日期不得早于 `last_event_date`（`STALE_EVENT`），
  防止迟到事件改写已确认的历史。

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts/disburse` | 开户并放款，生成还款计划 |
| POST | `/payments` | 提交还款（`REPAY` / `EARLY_SETTLEMENT`） |
| GET  | `/accounts/{no}` | 账户与各期账单明细 |
| GET  | `/accounts/{no}/view?as_of=YYYY-MM-DD` | 指定日期的状态/逾期/余额/应还视图 |

业务错误统一返回 HTTP 422：

```json
{"error": {"code": "OVERPAYMENT", "message": "...", "details": {"amount": 999900, "repay_max": 81102}}}
```

## 6. 本地验证

```bash
# 安装依赖（需要 Python 3.14+）
uv sync

# 运行全部测试（50 个用例，日志会打印输入/状态迁移/金额依据）
uv run pytest

# 只看某类场景
uv run pytest tests/test_idempotency_concurrency.py -v
uv run pytest tests/test_boundary_recovery.py -v

# 启动服务
uv run python main.py
# 或：uv run uvicorn app.api:app --reload
```

### 快速手测

```bash
curl -s -X POST localhost:8000/accounts/disburse -H 'Content-Type: application/json' -d '{
  "account_no":"L1","principal_yuan":"1000.00","annual_rate":"0.12",
  "term_periods":3,"disbursement_date":"2026-10-01","first_due_date":"2026-11-01",
  "grace_days":3,"penalty_daily_rate":"0.0005"}'

curl -s -X POST localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "account_no":"L1","reference_no":"P1","amount_yuan":"200.00","event_date":"2026-11-10"}'

# 重复提交同一报文：返回首次结果，不重复入账
curl -s -X POST localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "account_no":"L1","reference_no":"P1","amount_yuan":"200.00","event_date":"2026-11-10"}'

curl -s 'localhost:8000/accounts/L1/view?as_of=2026-11-11'
```

## 7. 测试场景覆盖

| 文件 | 覆盖点 |
| --- | --- |
| `test_money_schedule.py` | 金额精度/非法输入、等额本金守恒与利息 |
| `test_penalty_overdue.py` | 宽限期边界、起算日、跨月、确定性重算、本金清零后停计 |
| `test_repayment_flow.py` | 按期/部分/提前还本/提前结清/自然结清、超额、已结清拒绝、迟到事件 |
| `test_idempotency_concurrency.py` | 重复提交、流水号冲突、10 线程并发、总账守恒、失败无部分更新 |
| `test_boundary_recovery.py` | 宽限末日、起算日当天、逾期恢复、再逾期、跨月多期、文件库恢复 |
| `test_api.py` | HTTP 放款/还款/幂等/错误码/账户视图 |
