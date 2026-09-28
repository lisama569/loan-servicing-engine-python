# 本地贷款还款与逾期处理引擎

一个纯本地运行的贷款账务内核（FastAPI + 线程安全内存仓储），让**同一笔贷款**的
放款、按期/部分/提前/超额还款、逾期罚息与结清，在**重复请求、并发请求、日期
边界**下都给出一致、可复现、可解释的结果。

- 全部金额使用 `Decimal`，按分 `ROUND_HALF_UP` 量化，避免浮点漂移。
- 每笔成功操作都产生不可变账务事件（含操作前后三要素余额快照与分配明细）。
- 失败操作在校验阶段即被拒绝，已产生的临时计息通过快照回滚，不留中间状态。

## 目录结构

```
loan_engine/
  money.py         金额量化、月份推算
  models.py        Loan / Installment / LedgerEvent / PaymentResult
  statemachine.py  合法状态迁移表
  schedule.py      等额本金 + 按月计息的还款计划
  overdue.py       宽限期判定、罚息游标累计、状态评估与恢复
  allocation.py    还款校验与瀑布分配（罚息→利息→本金，按期序）
  repository.py    每贷款一把锁 + 幂等结果表（内存）
  service.py       编排：放款、幂等提交、失败回滚、只读快照
  api.py           FastAPI 路由与可区分错误码
tests/             33 个单测（含并发/幂等/跨月/异常状态）
```

## 一、状态机

```
                 到期/未逾期/有资金流        超过 宽限期+到期日
   OPEN  ───────────────────────▶  NORMAL  ───────────────▶  OVERDUE
    │                                 ▲                        │
    │                                 └───── 逾期期次全部结清 ──┘
    │                                                          │
    └────────────────────── 余额为零且各期结清 ───────────────▶ PAID_OFF（终态）
```

- `OPEN`：已放款，首期尚未到期且无资金流。
- `NORMAL`：有期次到期或已发生还款，当前没有超过宽限期的欠款。
- `OVERDUE`：存在到期日 + 宽限期已过但未结清的期次。
- `PAID_OFF`：三要素（本金/利息/罚息）余额全为零且各期结清，**终态不可逆**，
  之后的任何还款以 `LOAN_ALREADY_SETTLED` 拒绝。
- 非法迁移（如 `PAID_OFF → NORMAL`）抛 `ILLEGAL_TRANSITION`；状态只由
  `evaluate_status()` 依据余额与到期日推导，避免手工状态倒退。

## 二、还款计划

- 等额本金：每期本金 = 本金 / 期数，尾期吸收舍入余数，保证计划本金之和
  严格等于放款本金。
- 每期利息 = 当期期初剩余本金 × 年利率 / 12（按分量化），因此越后期利息越少。
- 第 k 期到期日 = 放款日 + k 个自然月（月末自动钳制，如 1/31 → 2/28）。

## 三、金额分配规则（可追踪）

期次按到期日**从前到后**消费；同一期内部瀑布顺序固定：

```
应计罚息（penalty） → 利息（interest） → 本金（principal）
```

- **按期还款 NORMAL**：只能覆盖截至 `value_date` 已出账的金额
  （已计罚息 + 已到期期次的本息）。
- **部分还款**：按上述瀑布逐期填充，未填满的期次仍为未结清，可继续补缴。
- **提前结清 EARLY_SETTLE**：金额必须恰好等于 `payoff-quote`
  （全部剩余本金 + 已出账利息 + 已计罚息）；未到期期次的利息作为
  `interest_waived` 核销，明细中可见。
- **超额还款**：普通还款金额大于当日可收金额时，以
  `PAYMENT_EXCEEDS_BALANCE` 拒绝（系统不持有无人认领的溢缴款）；提前结清
  金额不符以 `EARLY_SETTLE_AMOUNT_MISMATCH` 拒绝。
- 每次入账返回/记录：请求金额、期次级 `(罚息, 利息, 本金, 减免)` 分配明细、
  操作前后三要素余额、状态迁移与是否结清，事件可通过
  `GET /loans/{id}/events` 审计。

## 四、逾期判定、罚息累计与恢复

设到期日 `D`、宽限期 `G` 天：

```
D … D+G        宽限期：不算逾期，不计罚息
D+G+1 起        逾期；罚息天数 = 起息日 − (D+G)
日罚息 = 该期剩余本金 × 0.0005/天（按分量化）
```

- **幂等游标**：每期维护 `accrued_until`。重复计息同一天增量为 0；
  “逐日计息”和“跨多日一次计息”结果完全相同（跨月、跨宽限期可复现）。
- 还款入账时**先把罚息累计到该笔的 `value_date` 再分配**，因此迟到事件
  （late posting）与当日按日计提的金额一致，不会少计；罚息永远在瀑布第一位
  受偿，补缴不会造成重复罚息。
- 部分还款后剩余本金成为新的计息基准；本金结清的期次不再产生罚息，但已计提
  部分仍需缴纳。
- **恢复**：逾期期次全部结清且其余期次未逾期时，`OVERDUE → NORMAL`；
  全部期次结清则直接 `PAID_OFF`。不存在“结清后又逾期/状态倒退”。
- 事件按 `value_date` 非递减提交；更早日期的事件以
  `EVENT_DATE_OUT_OF_ORDER` 拒绝，防止历史被重写。
- 查询接口（`snapshot`/`payoff-quote`）在**副本**上计息，只读不移动游标。

## 五、并发与幂等约束

- **幂等键**：`(loan_id, payment_id)`。同一流水重复提交且参数（金额/日期/类型）
  一致 → 返回首次结果并标记 `duplicate=true`，账务只生效一次；参数不一致 →
  `IDEMPOTENCY_CONFLICT`。键按贷款隔离。
- **并发**：每笔贷款一把可重入锁，同一贷款的还款完全串行化；不同贷款互不阻塞。
  并发下要么整笔成功、要么整笔拒绝（提交前深拷贝，异常时整体回滚，含计息游标），
  失败操作不会产生事件、分配或不可恢复状态。
- 并发后守恒恒等式成立：
  `剩余本金 + 已还本金 = 放款本金`，
  `已还罚息 + 罚息余额 = 累计计提罚息`。

### 错误码一览（可区分失败原因）

| code | 含义 |
|---|---|
| `LOAN_NOT_FOUND` | 贷款不存在（HTTP 404，其余 409） |
| `LOAN_ALREADY_EXISTS` | 放款 ID 重复 |
| `PAYMENT_AMOUNT_INVALID` | 金额非正/参数非法 |
| `PAYMENT_EXCEEDS_BALANCE` | 普通还款超过当日可收（拒绝溢缴） |
| `EARLY_SETTLE_AMOUNT_MISMATCH` | 提前结清金额不等于报价 |
| `NOTHING_DUE` | 当日尚无已出账金额 |
| `LOAN_ALREADY_SETTLED` | 已终态结清，禁止再操作 |
| `IDEMPOTENCY_CONFLICT` | 复用流水号但参数不同 |
| `EVENT_DATE_OUT_OF_ORDER` | 事件日期早于已应用事件 |
| `ILLEGAL_TRANSITION` | 非法状态迁移 |

## 六、HTTP 接口

```
POST /loans                          放款
POST /loans/{id}/payments            还款（幂等：payment_id）
GET  /loans/{id}                     当前快照
GET  /loans/{id}/snapshot?as_of=     指定日期快照（副本计息）
GET  /loans/{id}/payoff-quote?as_of= 提前结清报价
GET  /loans/{id}/events              不可变账务事件 / 审计轨迹
```

示例：

```bash
uv run uvicorn main:app --reload

curl -s -X POST localhost:8000/loans -H 'content-type: application/json' -d '{
  "loan_id":"L1","principal":"1200.00","annual_rate":"0.12",
  "term_months":3,"disbursement_date":"2026-01-15","grace_days":3}'

curl -s -X POST localhost:8000/loans/L1/payments -H 'content-type: application/json' -d '{
  "payment_id":"P1","amount":"412.00","value_date":"2026-02-15"}'
```

## 七、本地验证

```bash
# 依赖（Python 3.14，uv）
uv sync

# 全部单测（日志打印输入、状态迁移、金额计算依据到 stdout）
uv run pytest -q -s

# 按场景查看
uv run pytest -q tests/test_idempotency.py   # 重复提交 / 流水号冲突
uv run pytest -q tests/test_concurrency.py   # 多线程还款与总账守恒
uv run pytest -q tests/test_overdue.py       # 跨宽限期 / 跨月 / 迟到入账
uv run pytest -q tests/test_lifecycle.py     # 部分/提前/正常结清
uv run pytest -q tests/test_errors.py        # 异常状态与失败原因
uv run pytest -q tests/test_api.py           # HTTP 层
```

测试日志示例（每次入账打印迁移与每一期的分配依据）：

```
payment applied loan=L1 id=P1 kind=NORMAL date=2026-02-25 amount=413.40 \
  status OPEN->NORMAL principal 1200.00->800.00 interest 24.00->12.00 penalty 1.40->0.00
  allocate period=1 penalty=1.40 interest=12.00 principal=400.00 waived=0.00
```

## 八、范围说明

当前仓储为进程内内存实现（锁 + 幂等表），适用于本地验证与单进程部署；
跨进程/持久化场景需将仓储替换为带唯一约束（`(loan_id,payment_id)`）与
行级锁/乐观版本号的数据库实现，领域层（状态机/计息/分配）无需改动。
