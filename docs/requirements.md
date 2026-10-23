# AgentOps — 需求文档（v2.2，主文档）

> v1 见 [requirements-v1.md](requirements-v1.md)（仅作历史参考）。
> 范围决策（已确认）：**作品集项目、单用户、单进程**；范围停在"能演示的对照实验"。
> 相对 v2.1，已合入 [requirements-review.md](requirements-review.md) 的三处评测口径：`cause` 改为封闭 id、`NOT_A_FAULT` / `UNKNOWN` 只出现在 `verdict`、幻觉率只统计模型第一次输出。

---

## 0. 相对 v1 的关键改动

| # | v1 问题 | v2 对策 |
|---|---------|---------|
| 1 | 工具全模拟，评测自证 | Scenario Simulator（带噪声/红鲱鱼/稳定证据键）；基线与 Agent **共用同一套工具** |
| 2 | 评测、CI 太晚 | Eval-first：M1 即有基准与基线；CI 从 M0 开始 |
| 3 | 路线图顺序矛盾 | 单次 LLM（程序喂证据）→ 手写 Agent → 记忆/RAG → 框架 |
| 4 | 无时间维度 | 工具带 `time_range`；Incident 带 `occurred_at/detected_at` |
| 5 | 同步诊断 | 异步 Run + SSE |
| 6 | LLM 自报置信度 | 置信度仅作展示，**不参与门禁**；以证据键命中与账本校验评分 |
| 7 | 根因只是字符串 | Evidence / Hypothesis 一等公民，结论必须引用账本 |
| 8 | 无安全模型 | §4.5 |
| 9 | 多 Agent 过早 | 单 Agent + 只读 Verifier；多 Agent 仅当消融证明有收益 |
| 10 | 数据模型冗余 | §4.6，并规定**单一事实来源** |

---

## 1. 产品定位

**一句话**：面向 SRE 的**只读、证据驱动、可评测**的故障初诊 Agent。

- 输入：故障报告（文字 + `service` + `occurred_at/detected_at`）。
- 输出：带**证据引用**的根因排序、建议动作、可回放 Trace。
- 非目标：自动修复、替代值班人员、多租户、实时告警接入、程序性记忆、Web UI（M4 事件流稳定后再议）。

### 1.1 成功标准（评测口径）

**Top-1 命中的定义**（仅当期望 `verdict` 为 `FAULT`）：`category` 必须命中，**且** `cause` 精确命中 `ground_truth.cause` 或 `acceptable_alternatives`。`cause` 与 `acceptable_alternatives` 都是下文 FR-5 的封闭枚举 id，不是自然语言。只命中类别**不算命中**；类别命中率单独报告，仅作诊断用。`cause_summary` 是给人读的句子，**不参与任何分数**。

`verdict` 为 `NOT_A_FAULT` 或 `UNKNOWN` 时，不计算 Top-1 和 Top-3，也不评 `severity`。此时 `category`、`cause`、`severity` 必须为空，只评 `verdict` 是否正确。

| 指标 | 规则基线（M1） | Agent 目标 |
|------|----------------|-----------|
| Top-1（类别 + 具体原因） | 记录 | **基线 + 20pp**（前提见下） |
| Top-3 | 记录 | ≥ 85% |
| 证据键召回（Evidence Key Recall） | — | ≥ 80% |
| 引用不在账本的比例 | 统计模型**第一次**结构化输出 | **0%**（账本校验，代码检查。重试后的报告不改写这个比例） |
| `UNKNOWN` / `NOT_A_FAULT` 判定正确率 | 记录 | 单独统计，见 §3 FR-8 |
| 工具调用次数 | 记录 | **≤ 8 次工具调用**（一步 = 一次工具调用；模型轮次另计，不受 8 限制） |
| 成本 | — | 按模型分别设预算；**$0.05 仅约束便宜档模型**，大模型单独设上限并如实报告 |
| P95 时延 | — | ≤ 60s |

**基线公平性前提**：规则基线只能通过 `ToolPort` 取数，**不得读取 `ground_truth` 或场景定义**。规则由**不看答案**的方式编写（先写规则、冻结，再写/生成场景；或规则只依据通用阈值如 CPU>90%）。基线如果贴着天花板，+20pp 无意义；若基线 ≥ 90%，需先加难场景（红鲱鱼、级联、证据不足）再比较。

---

## 2. 用户与核心用例

- **值班工程师**：提交事件 → 看调查过程 → 获得初诊 → 反馈对错。
- **Agent 开发者（你）**：改 Prompt/模型/工具后跑基准，看对照报告。

UC-1 创建事件；UC-2 触发异步诊断；UC-3 实时/回放 Trace；UC-4 查看结论与证据；UC-5 人工反馈；UC-6 检索相似历史；UC-7 运行评测并比较两次版本；UC-8（M7）审批建议动作。

---

## 3. 功能需求

### FR-1 事件管理
- 字段：`id, title, description, service, occurred_at, detected_at, status, severity(用户标注，可空), metadata, archived_at`。
- 状态：`OPEN → INVESTIGATING → DIAGNOSED → RESOLVED`。
  - Run 进入终态后，**由人通过 `PATCH` 把事件置为 `RESOLVED`；Agent 不修改该字段**（Agent 只能把 `OPEN → INVESTIGATING → DIAGNOSED`）。
  - 归档用 `archived_at`（软删除），**不设 `ARCHIVED` 状态**；`DELETE` = 设置 `archived_at`。
- 校验失败 → `422`，统一错误体（含 `error_code`）。

### FR-2 诊断运行（异步，单进程）
- `POST /v1/incidents/{id}/runs` → `202` + `run_id`。
- **幂等**：同一 `incident_id` + 同一 `Idempotency-Key` → 返回**已存在的 Run**（不新建）；无 Key 则每次新建。
- Run 状态：`PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, TIMED_OUT, BUDGET_EXCEEDED`。
- Run 记录 `agent_version, model, prompt_version, config`（含温度）以便复现。
- 执行：进程内 `asyncio` 任务。**单进程；服务重启后，将遗留的 `RUNNING` Run 标为 `FAILED(reason=restart)`**。不引入 Redis/队列。
- `GET /v1/runs/{id}/events`（SSE）、`POST /v1/runs/{id}/cancel`。

### FR-3 诊断工具（统一契约）
1. Pydantic 输入/输出 Schema，自动生成给 LLM 的 JSON Schema。
2. **只读**；带 `time_range`、`service`；结果截断+摘要。
3. 返回 `ToolResult{status, data, summary, evidence_keys, truncated}`；失败是**观察结果而非异常**。
4. 超时、重试；输出经**脱敏 + 注入过滤**。
5. **参数收口**：能枚举的一律枚举，如 `query_metrics.metric ∈ {cpu, memory, disk, latency_p99, error_rate, db_connections, …}`，模型不得自由填写字符串；`service` 必须取自场景拓扑中的已知服务。
6. **稳定证据键（evidence key）**：场景中的每条"相关事实"带稳定键（如 `log:pool_timeout`、`metric:cpu_high`）；工具结果携带命中的键。评分只认这个键，不认运行时生成的编号。

最小工具集：`query_metrics`、`search_logs`、`get_service_status`、`get_recent_changes`、`get_dependency_health`、`query_db_health`、`search_memory`（M6）。

### FR-4 Agent 调查
```
Triage(代码) → Hypothesize(LLM) → Investigate(LLM 选工具，循环) → Verify(只读 Verifier，一轮) → Report
```
- **Triage 由代码完成**：`service / occurred_at / detected_at` 直接读取 Incident 字段；**仅当字段为空时**才让模型从正文补全，并在 Trace 中标注"由模型推断"。
- **Verifier 约束**：只能读取**本次账本**，核对（a）引用的证据键是否存在，（b）账本中是否有与 Top 假设矛盾的证据；**只跑一轮，不得发起任何工具调用**，不是第二个 Agent。
- 硬约束：最大工具调用次数、最大 Token、最大成本、墙上时钟超时、同工具同参数去重、连续 N 次无新证据则强制收敛。
- **两个时钟分开**：`WallClock`（预算/超时，真实时间）与 `ScenarioClock`（场景内"当前时间"，决定时间窗）。二者通过 `ClockPort` 分别注入，测试时可独立伪造。

### FR-5 结构化诊断输出
```json
{
  "hypotheses": [
    {"cause": "db_pool_exhausted", "cause_summary": "连接池已满，新请求拿不到连接",
     "category": "DATABASE", "confidence": 0.82,
     "supporting_evidence": ["log:pool_timeout","metric:db_conn_max"],
     "contradicting_evidence": []}
  ],
  "verdict": "FAULT",
  "severity": "high",
  "recommendations": [{"action": "...", "risk": "low|med|high", "requires_approval": true}],
  "unresolved_questions": ["..."]
}
```
- **`category`（机制，封闭枚举）**：`CPU, MEMORY, DISK, DATABASE, NETWORK, DEPENDENCY, DEPLOY, CONFIG, TRAFFIC`。这里没有 `NOT_A_FAULT` 和 `UNKNOWN`。
- **`cause`（封闭枚举 id）**：`cpu_saturation`（CPU）、`memory_leak`（MEMORY）、`disk_full`（DISK）、`db_pool_exhausted`（DATABASE）、`dns_failure`（NETWORK）、`bad_deploy`（DEPLOY）、`dependency_timeout`（DEPENDENCY）、`config_error`（CONFIG）、`traffic_spike`（TRAFFIC）。模型不得自造字符串。新增 id 必须先改本表。`cause_summary` 不计分。
- **`verdict`**：`FAULT | NOT_A_FAULT | UNKNOWN`。
  - `NOT_A_FAULT`：证据表明系统健康或属正常波动（如流量突增、告警误报）。
  - `UNKNOWN`：证据**不足以**判断。
  - 二者只出现在 `verdict`。评测分开统计。这两个结论下 `category`、`cause`、`severity` 为空。
- **幻觉率看第一次输出。** 每个引用必须在本次 Run 的账本中。第一次结构化输出里若有账本中不存在的证据键，记入「引用不在账本的比例」，然后允许重试一次。重试成功**不把该比例改写成 0**。重试后仍不合法：Run = `FAILED`，`final_report` 为空，该题评测记错。
- 预算耗尽/超时的 Run 也输出**半成品报告**（当前最佳假设 + 未解决问题）。`confidence` 由模型填写，只展示，不参与门禁。

### FR-6 Trace
- 事件类型：`RUN_STARTED, LLM_CALL, TOOL_CALL, TOOL_RESULT, EVIDENCE_ADDED, HYPOTHESIS_UPDATED, GUARDRAIL_TRIGGERED, FINAL_REPORT, ERROR`。
- 不存模型私有 CoT；存"决策摘要"字段。
- **写入顺序/事务**：先追加 `RunEvent`，`ToolCall` 与 `Evidence` **与对应事件在同一数据库事务内写入**。

### FR-7 记忆（M6）：情景记忆 + 服务知识库

> 本版**不做程序性记忆**（把成功案例提炼成调查策略写回提示词，会与"污染对抗"相冲突）。`HistoricalIncident` 并入情景记忆，**只保留 `MemoryItem`**。

| 层 | 内容 | 写入来源 |
|----|------|----------|
| Working | 单次 Run 的账本、假设板、预算（已有，不入 `MemoryItem`） | 运行时 |
| Episodic | 历史事件 + 诊断摘要 + 反馈结果 | Run 结束 + 人工反馈 |
| Semantic（服务知识库） | 拓扑、指标基线、已知故障模式、runbook | 人工导入 |

**信任与晋升**
- `trust_level`：`CANDIDATE → CONFIRMED`（人工判"正确"）/ `REJECTED`（判"错误"，仅留作"曾被证伪的假设"提示）。
- **晋升规则**：反馈为"正确"时，**只晋升该 Run 最终报告里实际引用过的记忆**（`MemoryRecall.cited = true`）。检索到但未被引用的记忆，**不得因用户点"正确"而变 `CONFIRMED`**。
- 写入前去重合并（同服务 + 同类别 + 相似度超阈值 → 合并并累加计数）。
- 摘要与工具输出同样脱敏。

**检索与注入**
- Triage 后一次性召回；调查中可通过 `search_memory` 工具追加。
- 注入时标注来源/时间/`trust_level`；提示词声明"记忆仅作线索，必须以当前证据验证"。
- 排序 = 相似度 × 时间衰减 × 信任权重 × 服务版本匹配；向量 + BM25 + 元数据过滤。
- `GET /v1/memory/search`（原 `incidents/search`，**避免与 `GET /incidents/{id}` 抢路径**）：**默认只返回 `CONFIRMED`**，可显式传参包含 `CANDIDATE`。

**跨 Run**：同一 `incident_id` 的新 Run 默认继承前序 Run 的已排除假设与证据摘要（可关）。

**评测（验收）**：无记忆 / 情景 / 情景+知识库 消融；**记忆污染场景**（历史含错误根因的相似事件，Agent 须用当前证据反驳）；**陈旧场景**（服务已升级）；无收益的层默认关闭并记录结论。

### FR-8 评测框架
- 基准用例 = `incident + scenario_id + expected{category, cause, acceptable_alternatives, key_evidence_keys, verdict, severity}`。`cause` 与 `acceptable_alternatives` 都是 FR-5 的枚举 id。期望 `verdict` 不是 `FAULT` 时，`category`、`cause`、`severity` 为空。
- **合并门禁只用确定性分数**：
  1. 期望 `verdict` 为 `FAULT` 时：`category` 命中且 `cause` id 命中（含 alternatives）；
  2. 证据键召回/精确率；
  3. **第一次输出**的引用是否全部在账本中（重试不改变这项分数）；
  4. `verdict` 是否正确（`UNKNOWN` / `NOT_A_FAULT` / `FAULT` 分开计）；
  5. 半成品报告同样评分，**空报告记为错误**（防止模型把预算耗尽当退路）。
- **稳定性指标**：同题重复 k 次，报告**答案一致的比例**（`agreement@k`，而非 `pass@k`）。温度设为 0 后**仍需报告该比例**。
- **LLM-as-judge 仅进夜间报告，不阻断合并**。
- **回放（cassette）的保护范围**：只保护解析器、账本、编排逻辑，**不保护 Prompt 质量**。Prompt/模型变更须以**夜间真实模型评测**为准。
- 轨迹指标：工具调用次数、冗余调用率、无效调用率。
- 对比报告：`eval compare A B`。基准 ≥ 20 题（≥ 4 `UNKNOWN`、≥ 3 `NOT_A_FAULT`、≥ 4 误导性证据、≥ 2 含提示词注入日志）。

### FR-9 反馈
`POST /v1/runs/{id}/feedback`：`verdict ∈ {correct, incorrect, partial}` + `true_cause`（FR-5 的 cause id，可空）。晋升规则见 FR-7。

### FR-10 API（单用户）
```
GET  /health
POST/GET/PATCH /v1/incidents[/{id}]        DELETE = 设置 archived_at
POST /v1/incidents/{id}/runs               202, Idempotency-Key
GET  /v1/runs/{id}, /v1/runs/{id}/events (SSE)
POST /v1/runs/{id}/cancel, /v1/runs/{id}/feedback
GET  /v1/memory/search
POST /v1/evaluations, GET /v1/evaluations/{id}
GET  /v1/scenarios
```
鉴权：请求头携带共享密钥，值来自环境变量 `API_KEY`；`/health` 免鉴权。分页、统一错误体。

---

## 4. 架构

### 4.1 总览
```
 Client / CLI
     │ REST + SSE（共享密钥）
┌────▼───────────────────────────────┐
│ API (FastAPI) 薄层                  │
├────────────────────────────────────┤
│ Services: Incident / Run / Eval /   │
│ Feedback / Memory                   │
├────────────────────────────────────┤
│ Run Executor（进程内 asyncio）       │
├────────────────────────────────────┤
│ Agent Runtime (domain)              │
│ Orchestrator · Budget · Guardrails  │
│ Evidence Ledger · Hypothesis Board  │
│ Verifier(只读账本)                   │
├──────────┬─────────────┬───────────┤
│ LLMPort  │ ToolPort    │ MemoryPort│
│ 先接1家   │ Simulator   │ (M6)      │
│ +Fake    │ (+Prom 可选) │           │
└──────────┴─────────────┴───────────┘
  ClockPort: WallClock / ScenarioClock
  PostgreSQL(+pgvector，M2/M6 起)
```

### 4.2 设计原则
1. 六边形：Runtime 仅依赖 Port，可全部 Fake。
2. 工具契约统一，Simulator 与真实适配器可互换。
3. **确定性优先**：时间窗、服务、去重、预算、校验由代码完成。
4. 证据账本是结论的唯一来源。
5. 失败即观察。

### 4.3 Scenario Simulator
- YAML：拓扑、指标时间序列、日志模板、变更记录、红鲱鱼、**证据键**、`ground_truth`。
- 参数化生成扩充场景；初始场景：高 CPU（`cpu_saturation`）、内存泄漏、连接池耗尽、磁盘满、DNS、错误部署、级联超时、配置错误、流量突增（`verdict=NOT_A_FAULT`）、证据不足（`verdict=UNKNOWN`）、误导性告警、日志含注入文本。
- 规则基线**不得**访问 `ground_truth`（由 `ToolPort` 隔离，`ground_truth` 只被评分器读取）。

### 4.4 框架引入理由
| 技术 | 阶段 | 理由 / 验收 |
|------|------|-------------|
| 手写循环 | M4 | 理解协议与预算；作为框架对照 |
| LangGraph | M7 | checkpoint 恢复、审批中断；迁移后基准**不退化** |
| MCP | M8 | 工具标准化暴露；对比直连延迟 |
| 多 Agent | M9 可选 | 仅当单 Agent 在跨域题低于阈值且消融有收益 |
| pgvector | M6 | 与业务数据同库 |

### 4.5 安全与护栏
- 只读；Agent 无 shell；建议动作带风险等级、默认需审批。
- **提示词注入**：工具输出以"不可信数据"分隔标记包裹；含注入文本的日志作为评测题。
- **脱敏**：密钥/token/邮箱 → **占位符**（如 `<REDACTED:email>`）；**服务名、主机名保留**；**IP 不删除**，替换为稳定占位符（同一 IP 同一占位，如 `<ip-1>`），以保留日志里的主机关联。
- **预算**：多维上限；触发后输出半成品报告并评分。
- 密钥仅环境变量；`.env` 不入库。

### 4.6 数据模型与单一事实来源

```
Incident(id, title, description, service, occurred_at, detected_at,
         status, user_severity, metadata, archived_at, created_at, updated_at)
Run(id, incident_id→, idempotency_key, status, agent_version, model, prompt_version,
    config, started_at, ended_at, tokens_in, tokens_out, cost_usd,
    final_report JSONB, failure_reason)
    UNIQUE(incident_id, idempotency_key) WHERE idempotency_key IS NOT NULL
RunEvent(id, run_id→, seq, type, payload, created_at)   -- append-only
ToolCall(id, run_id→, event_id→, tool, input, output, status, latency_ms)
Evidence(id, run_id→, tool_call_id→, evidence_key, summary, excerpt, time_range)
Hypothesis(id, run_id→, cause, cause_summary, category, confidence, status, version)
HypothesisEvidence(hypothesis_id→, evidence_id→, relation[supports|contradicts])
Feedback(id, run_id→, verdict, true_cause, notes, created_at)  -- true_cause 使用 FR-5 的 cause id
MemoryItem(id, layer[episodic|semantic], content, summary, service, category,
           trust_level[CANDIDATE|CONFIRMED|REJECTED], source_run_id→, source_feedback_id→,
           embedding vector, valid_from, valid_until, service_version,
           usage_count, helpful_count, last_used_at, superseded_by→, created_at)
MemoryRecall(id, run_id→, memory_id→, score, injected, cited)
EvalRun(id, name, git_sha, agent_version, model, config, started_at, summary)
EvalCaseResult(id, eval_run_id→, scenario_id, run_id→, scores)
```
场景放文件（`scenarios/*.yaml`），**不入库**。

**单一事实来源**
- 过程事实：`RunEvent` 为时间线；`ToolCall` / `Evidence` 与事件**同事务**写入。
- 假设：调查过程中**以 `Hypothesis` 表为准**；`Run.final_report` 只是 Run 结束时的**快照**，不得反向更新表。
- 记忆：只有 `MemoryItem`，无 `HistoricalIncident`。

### 4.7 目录结构
```
app/
  api/  domain/  agent/  tools/(adapters/simulator)  llm/(adapters/fake,<provider>)
  memory/(M6)  persistence/(M2)  eval/  core/
docs/requirements.md  docs/requirements-v1.md  docs/requirements-review.md
scenarios/  tests/{unit,integration,eval}
```
`domain/` 出现后再启用 `mypy --strict`。

---

## 5. 非功能需求

| 项 | 要求 |
|----|------|
| 性能 | 非 LLM API P95 < 300ms；诊断 P95 < 60s |
| 可靠性 | 单工具失败不终止 Run；LLM 重试退避；重启后 `RUNNING` → `FAILED(restart)` |
| 可复现 | Run 保存模型/Prompt 版本/配置；评测支持回放 |
| 可观测 | 结构化 JSON 日志（含 `run_id`）；OTel **到 M5 再引入** |
| 安全 | §4.5；`pip-audit` 进 CI |
| 可测试 | 单元测试不触网；**覆盖率 80% 只约束已存在的 domain/agent 代码** |

---

## 6. 路线图（范围停在可演示的对照实验）

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| **M0 地基（最小）** | `pyproject.toml`、ruff、pytest、GitHub Actions、统一错误体、**请求模型改为 FR-1 字段**、README 补全 | PR 触发 CI 通过；`/health` 与 `/docs` 可用。**不含** Postgres / Compose / OTel / mypy strict |
| **M1 场景与基线** | Scenario Simulator、工具契约、证据键、20 题基准、规则基线（不读 `ground_truth`）、评测 Runner/报告 | 基线分数 JSON 入库；基线公平性自检通过 |
| **M2 持久化与异步 Run** | Postgres + Compose（随**第一张表**出现）、SQLAlchemy async、Alembic、Incident/Run/RunEvent、asyncio 执行、SSE、幂等、重启恢复策略 | 重启后历史可查；`RUNNING` 被标失败；同 Key 返回同 Run |
| **M3 单次 LLM 诊断（对照组）** | LLMPort + 一家适配器 + Fake；**程序把全部工具结果一次性喂给模型，模型不选择工具**；结构化输出 | 与基线对比成报告。**若模型能选工具，则不属于 M3** |
| **M4 手写 Agent** | 模型自主选工具、账本、预算、护栏、只读 Verifier、账本校验、cassette 回放、`NOT_A_FAULT/UNKNOWN` | 相对 M3 的增益 = "会调查"的收益；离线 CI 回归 |
| **M5 评测门禁与可观测** | 门禁（确定性分数）、`agreement@k`、夜间真实模型 + judge、OTel | 确定性指标退化阻断合并 |
| **M6 记忆（情景 + 知识库）** | `MemoryItem`、信任/晋升（仅被引用者）、混合检索、跨 Run 继承、`/memory/search` | 消融；污染/陈旧场景不盲信；无收益层关闭并记录 |
| **M7 LangGraph + HITL** | 迁移 Orchestrator；checkpoint；审批 | 基准不退化 |
| **M8 MCP**（+ 可选 Prometheus） | 自建 MCP Server；Prometheus **一个场景能跑即可，不作主线验收** | MCP 与直连对比 |
| **M9 可选** | 多 Agent / 反馈转基准 | 消融报告 |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| 基线贴天花板 | 规则先冻结；基线 ≥ 90% 则加难场景 |
| 评测抖动 | 温度 0；报告 `agreement@k`；门禁只用确定性分数 |
| 成本 | 分模型预算；便宜模型做 Triage 补全 |
| 上下文爆炸 | 工具侧截断；账本只回传摘要 |
| 范围蔓延 | 严格里程碑；新技术须附引入理由与对照实验 |

---

## 8. 已确认的决策

1. 模型端口先接**已有密钥的一家**，另一家只留适配器空位。
2. 目标为作品集；Prometheus 非主线验收。
3. 先 API + CLI，Trace 页面待 M4 事件流稳定后再议。
4. 异步为进程内 `asyncio`，单进程，不引入 Redis。
5. v2 为主文档；v1 移至 `docs/requirements-v1.md`。
6. 记忆只做情景层 + 服务知识库，无程序性记忆。
7. 单用户，无 `tenant_id`；API 用环境变量共享密钥。

## 9. 仍待确认
- **首个 LLM 提供商是哪家？**（决定 M3 的适配器；文档暂以 `<provider>` 占位）
