# AgentOps — 需求文档（v2.3）

> 范围决策（已确认）：**作品集项目、单用户、单进程**；范围停在"能演示的对照实验"。
> 版本演进（已合并旧版 v1 与历次评审文档）：
> - **v2.1**：确立只读诊断、Scenario Simulator + 稳定证据键、Eval-first（M1 基线）、单进程异步 Run + SSE、证据账本与只读 Verifier、情景记忆 + 服务知识库（无程序性记忆）。
> - **v2.2**：`cause` 改为封闭 id、`NOT_A_FAULT` / `UNKNOWN` 只出现在 `verdict`、幻觉率只统计模型第一次输出。
> - **v2.3（当前）**：主结论改为 M4 Top-1 减 M3 Top-1、幻觉率与校验通过条件拆开、补齐 Top-1/Top-3 与证据精确率、删掉 `traffic_spike`、备选 cause 必须属于期望 category、M3 参数由代码生成、PR 门禁与夜间评测拆开、M1 分数写入文件、冻结题集、请求字段统一为 `user_severity`、注入文本只包裹不删除。实现时的行为见各节，未填数字见 §9。
> - **精确率空集**：期望键和引用都为空时，证据精确率无定义，不进入平均。这修正了「没有引用时精确率为 0」对应当引用为空的 `NOT_A_FAULT` / `UNKNOWN` 的误罚。期望键非空但没有引用，仍为 0。

---

## 0. 相对 v1 的关键改动

| # | v1 问题 | v2 对策 |
|---|---------|---------|
| 1 | 工具全模拟，评测自证 | Scenario Simulator（带噪声/红鲱鱼/稳定证据键）；基线与 Agent **共用同一套工具** |
| 2 | 评测、CI 太晚 | Eval-first：M1 即有基准与基线；CI 从 M0 开始 |
| 3 | 路线图顺序矛盾 | 单次 LLM（程序喂证据）→ 手写 Agent → 记忆/RAG → 框架 |
| 4 | 无时间维度 | 工具带 `time_range`；Incident 带 `occurred_at/detected_at` |
| 5 | 同步诊断 | 异步 Run + SSE |
| 6 | LLM 自报置信度 | 置信度仅作展示，**不参与排序或门禁**；以证据键命中与账本校验评分 |
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

**排序。** 评分器按 `hypotheses` 的数组顺序计分，不得按 `confidence` 重排。`hypotheses[0]` 是 Top-1。正确 `cause` 出现在前三条里即 Top-3 命中；列表短于 3 时按实际长度。`confidence` 只展示。

**Top-1 命中的定义**（仅当期望 `verdict` 为 `FAULT`）：`category` 必须命中，**且** `hypotheses[0].cause` 精确命中 `ground_truth.cause` 或 `acceptable_alternatives`。`cause` 与 `acceptable_alternatives` 都是下文 FR-5 的封闭枚举 id，不是自然语言。备选 cause 必须属于期望 `category`。场景冻结后不得按模型输出再补同义词。只命中类别**不算命中**；类别命中率单独报告，仅作诊断用。`cause_summary` 是给人读的句子，**不参与任何分数**。

**Top-3 命中**：正确 `cause` 出现在前三条（不足三条则按实际长度），且该条的 `category` 命中。判定用的 cause 集合与 Top-1 相同。

`verdict` 为 `NOT_A_FAULT` 或 `UNKNOWN` 时，不计算 Top-1 和 Top-3，也不评 `severity`。此时 `category`、`cause`、`severity` 必须为空，只评 `verdict` 是否正确。评分比较报告里的 `severity` 与场景期望，不读事件上的 `user_severity`。FAULT 场景的严重度进入夜间报告，不进 Pull Request 门禁。

**证据键**
- 召回 = `|引用键 ∩ 期望键| / |期望键|`
- 精确率 = `|引用键 ∩ 期望键| / |引用键|`。期望键和引用都为空时精确率无定义，不进入平均（与召回在期望键为空时相同）。期望键非空但没有引用时精确率为 0。有引用而期望键为空时精确率为 0。红鲱鱼键在账本里是合法引用，会进入分母。
- 工具结果只带证据键和数据，不带 `relevant: true`。`ground_truth` 只有评分器能读。

**引用的两个数**
- 报告值：第一次结构化输出的非法引用比例。不设 0% 硬目标。重试成功不得把该比例改写成 0。
- 通过条件：校验器接受的那份报告里非法引用为 0。重试后仍非法则 Run = `FAILED`，`final_report` 为空，该题记错。

**主结论**是 **M4 Top-1 减 M3 Top-1**（同样的证据，差别只在谁选工具）。相对规则基线的差值照实记录。基线 Top-1 低于 30% 时，不把相对基线 +20pp 写成失败。基线 ≥ 90% 就加难。

对外报告的分数只来自事先冻结的题集（场景 id + 文件哈希），见 FR-8。

| 指标 | 规则基线（M1） | Agent 目标 |
|------|----------------|-----------|
| Top-1（类别 + 具体原因） | 记录 | **主结论 = M4 Top-1 − M3 Top-1**；相对基线的差值照实记录 |
| Top-3 | 记录 | ≥ 85%（定义见上） |
| 证据键召回 | — | ≥ 80%（公式见上） |
| 证据键精确率 | — | 记录（公式见上；与召回一起进入夜间报告） |
| 第一次输出的非法引用比例 | 统计模型**第一次**结构化输出 | 报告该比例，不设 0% 硬目标 |
| 校验通过 | — | 被接受的报告里非法引用为 0；否则该题记错 |
| `UNKNOWN` / `NOT_A_FAULT` 判定正确率 | 记录 | 单独统计，见 §3 FR-8；目标值见 §9 |
| 工具调用次数 | 记录 | **≤ 8 次工具调用**（一步 = 一次工具调用；模型轮次另计，不受 8 限制） |
| 成本 | — | 按模型分别设预算；**$0.05 仅约束便宜档模型**，大模型单独设上限并如实报告 |
| 单次 Run 墙上时钟 | — | 上限 60 秒；超时即 `TIMED_OUT` |

**基线公平性前提**：规则基线只能通过 `ToolPort` 取数，**不得读取 `ground_truth` 或场景定义**。规则由**不看答案**的方式编写（先写规则、冻结，再写/生成场景；或规则只依据通用阈值如 CPU>90%）。基线 Top-1 ≥ 90% 时，先加难场景（红鲱鱼、级联、证据不足）再比较。基线 Top-1 低于 30% 时，相对基线的涨幅只作记录。

最初 23 题和规则基线在同一提交（`f1eba32`）进入仓库，git 历史不能证明规则先于这些答案冻结。规则使用通用阈值。`RULE_ORDER` 没有为了红鲱鱼误判重排，后加的多跳、下钻、冲突和噪声题也没有改这份顺序；那些题是对着已经在树上的规则写的。只扫事件服务、并用默认时间窗，拿不到多跳和下钻题的决定性证据键。规则 Top-1 因此留在约 0.87 以下，给 M3 / M4 留出空间。具体分数以 `evals/baselines/m1-rule.json` 为准。

---

## 2. 用户与核心用例

- **值班工程师**：提交事件 → 看调查过程 → 获得初诊 → 反馈对错。
- **Agent 开发者（你）**：改 Prompt/模型/工具后跑基准，看对照报告。

UC-1 创建事件；UC-2 触发异步诊断；UC-3 实时/回放 Trace；UC-4 查看结论与证据；UC-5 人工反馈；UC-6 检索相似历史；UC-7 运行评测并比较两次版本；UC-8（M7）审批建议动作。

---

## 3. 功能需求

### FR-1 事件管理
- 字段：`id, title, description, service, occurred_at, detected_at, status, user_severity(用户标注，可空), metadata, archived_at`。诊断严重度只出现在报告的 `severity` 里，请求体不用这个名字。
- 状态：`OPEN → INVESTIGATING → DIAGNOSED → RESOLVED`。
  - Run 进入终态后，**由人通过 `PATCH` 把事件置为 `RESOLVED`；Agent 不修改该字段**（Agent 只能把 `OPEN → INVESTIGATING → DIAGNOSED`）。
  - 有报告（含半成品）→ `DIAGNOSED`。没有报告的 `FAILED` / `CANCELLED` 保持 `INVESTIGATING`。已经 `RESOLVED` 仍允许再跑，状态不自动退回。
  - 同一事件同时只允许一个 `PENDING` / `RUNNING` Run。已有未终态 Run 时再次创建（且未命中同一 `Idempotency-Key`）返回 `409`。
  - 归档用 `archived_at`（软删除），**不设 `ARCHIVED` 状态**；`DELETE` = 设置 `archived_at`。归档后的事件拒绝新建 Run（`409`）。列表默认隐藏 `archived_at` 非空的事件。
- 校验失败 → `422`，统一错误体（含 `error_code`）。

### FR-2 诊断运行（异步，单进程）
- `POST /v1/incidents/{id}/runs`：新建返回 `202` + `run_id`。
- **幂等**：同一 `incident_id` + 同一 `Idempotency-Key` → 返回**已存在的 Run**（不新建）。命中已有 Run 时返回该 Run 的当前状态：仍在跑（`PENDING` / `RUNNING`）返回 `202`，已终态返回 `200`。无 Key 则每次新建；该事件已有未终态 Run 时返回 `409`（见 FR-1）。评测请求不带 `Idempotency-Key`（见 FR-8）。
- Run 状态：`PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, TIMED_OUT, BUDGET_EXCEEDED`。
- `BUDGET_EXCEEDED` 和 `TIMED_OUT` 必须带半成品 `final_report`，并参与评分。`CANCELLED` 不补报告。空报告记错。
- Run 记录 `agent_version, model, prompt_version, config`（含温度）以便复现。
- 执行：进程内 `asyncio` 任务。**单进程；服务重启后，将遗留的 `RUNNING` Run 标为 `FAILED(reason=restart)`**。不引入 Redis/队列。M7 的 checkpoint 不取代这条策略，见 §6。
- `GET /v1/runs/{id}` 返回当前状态和 `final_report`。`GET /v1/runs/{id}/events`（SSE）从 `seq=0` 回放已有事件，并支持 `Last-Event-ID`。客户端断开不取消 Run。鉴权继续用请求头；浏览器 `EventSource` 放进以后的 Trace 页面，不在 M2 范围。`POST /v1/runs/{id}/cancel`。

### FR-3 诊断工具（统一契约）
1. Pydantic 输入/输出 Schema，自动生成给 LLM 的 JSON Schema。
2. **只读**；带 `time_range`、`service`；结果截断+摘要。
3. 返回 `ToolResult{status, data, summary, evidence_keys, truncated}`；失败是**观察结果而非异常**。
4. 超时、重试；输出经**脱敏**，再以不可信数据分隔标记包裹。注入文本保留，不删除（见 §4.5）。
5. **参数收口**：能枚举的一律枚举。`query_metrics.metric ∈ {cpu, memory, disk, latency_p99, error_rate, db_connections}`，封闭列表，模型不得自由填写字符串；新增值必须先改本表。`service` 必须取自场景拓扑中的已知服务。
6. **稳定证据键（evidence key）**：场景中的每条"相关事实"带稳定键（如 `log:pool_timeout`、`metric:cpu_high`）；工具结果携带命中的键和数据，不带 `relevant: true`。评分只认这个键，不认运行时生成的编号。`ground_truth` 只有评分器能读。
7. **M3 的参数只由代码生成**：拓扑中的每个服务、每个指标枚举值、一个由事件时间决定的窗口（见 FR-4）、日志用固定查询（`level>=ERROR`）。禁止按题写查询。

最小工具集：`query_metrics`、`search_logs`、`get_service_status`、`get_recent_changes`、`get_dependency_health`、`query_db_health`。`search_memory` 到 M6 才加入。M1–M5 的工具集不含 `search_memory`。M6 加入该工具后，新分数不得与已发布的 M3/M4 分数直接相减。

### FR-4 Agent 调查
```
Triage(代码) → Hypothesize(LLM) → Investigate(LLM 选工具，循环) → Verify(只读 Verifier，一轮) → Report
```
- **Triage 由代码完成**：`service / occurred_at / detected_at` 直接读取 Incident 字段；**仅当字段为空时**才让模型从正文补全，并在 Trace 中标注"由模型推断"。评测用例里 `service` 和时间必填。API 上 `service` 可空，空了才允许模型补全。
- **默认时间窗**：有 `occurred_at` 时，窗口为它的前 15 分钟到 `detected_at` 后 5 分钟；没有则用 `detected_at` 前 30 分钟。工具的 `time_range` 用 `ScenarioClock`；`RunEvent.created_at` 用 `WallClock`。
- **Verifier 约束**：只能读取**本次账本**，核对（a）引用的证据键是否存在，（b）账本中是否有与 Top 假设矛盾的证据；**只跑一轮，不得发起任何工具调用**，不是第二个 Agent。非法引用触发的那一次重试只重新生成报告，不新增工具调用，也不算第二轮调查。
- 硬约束：最大工具调用次数、最大 Token、最大成本、墙上时钟超时（单次 Run 上限 60 秒，超时即 `TIMED_OUT`）、同工具同参数去重、连续 N 次无新证据则强制收敛。
- **两个时钟分开**：`WallClock`（预算/超时，真实时间）与 `ScenarioClock`（场景内"当前时间"，决定时间窗）。二者通过 `ClockPort` 分别注入，测试时可独立伪造。
- **`LLMPort` 的调用是异步的。** 同步 SDK 必须放进线程，避免堵住单进程事件循环。

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
- **`category`（机制，封闭枚举）**：`CPU, MEMORY, DISK, DATABASE, NETWORK, DEPENDENCY, DEPLOY, CONFIG`。这里没有 `NOT_A_FAULT`、`UNKNOWN` 和 `TRAFFIC`。流量突增不占类别：它只作为 `verdict=NOT_A_FAULT`，此时 `category` 与 `cause` 都为空。
- **`cause`（封闭枚举 id，必须与 category 成对）**：`cpu_saturation`（CPU）、`memory_leak`（MEMORY）、`disk_full`（DISK）、`db_pool_exhausted`（DATABASE）、`dns_failure`（NETWORK）、`bad_deploy`（DEPLOY）、`dependency_timeout`（DEPENDENCY）、`config_error`（CONFIG）。模型不得自造字符串。新增 id 必须先改本表，并写明唯一配对的 category。`cause` 与 `category` 不匹配时，这次结构化输出视为非法，走下面同一套重试规则。`cause_summary` 不计分。流量突增不是 cause id，只作为 `verdict=NOT_A_FAULT` 的例子。
- **`verdict`**：`FAULT | NOT_A_FAULT | UNKNOWN`。
  - `NOT_A_FAULT`：证据表明系统健康或属正常波动（如流量突增、告警误报）。
  - `UNKNOWN`：证据**不足以**判断。
  - 二者只出现在 `verdict`。评测分开统计。这两个结论下 `category`、`cause`、`severity` 为空。
- **报告里的 `severity`** 是诊断严重度。评分比较它和场景期望，不读 `user_severity`。`NOT_A_FAULT` / `UNKNOWN` 不评严重度。FAULT 场景的严重度进入夜间报告，不进 Pull Request 门禁。
- **引用分两个数。** 每个引用必须在本次 Run 的账本中。第一次结构化输出里若有账本中不存在的证据键，记入「第一次输出的非法引用比例」（不设 0% 硬目标），然后允许重试一次。重试只重新生成报告，不新增工具调用。重试成功**不把该比例改写成 0**。通过条件是：校验器接受的那份报告里非法引用为 0。重试后仍不合法：Run = `FAILED`，`final_report` 为空，该题评测记错。
- `BUDGET_EXCEEDED` 与 `TIMED_OUT` 必须输出**半成品报告**（当前最佳假设 + 未解决问题）并参与评分。`CANCELLED` 不补报告。空报告记错。`confidence` 由模型填写，只展示，不参与门禁，也不用于重排假设。
- **`Hypothesis.status`**：`PROPOSED | LEADING | REJECTED`。跨 Run 继承的「已排除假设」就是 `REJECTED`。

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
- `trust_level`：`CANDIDATE → CONFIRMED`（人工判 `correct`）/ `REJECTED`（人工判 `incorrect`，仅留作"曾被证伪的假设"提示）。
- **晋升规则**：反馈为 `correct` 时，**只晋升该 Run 最终报告里实际引用过的记忆**（`MemoryRecall.cited = true`）。检索到但未被引用的记忆，**不得因用户点 `correct` 而变 `CONFIRMED`**。`incorrect` 把这些实际引用过的记忆标为 `REJECTED`。`partial` 不改变 `trust_level`。
- 写入前去重合并（同服务 + 同类别 + 相似度超阈值 → 合并并累加计数）。
- 摘要与工具输出同样脱敏。

**检索与注入**
- Triage 后一次性召回；调查中可通过 `search_memory` 工具追加。
- 注入时标注来源/时间/`trust_level`；提示词声明"记忆仅作线索，必须以当前证据验证"。
- 排序 = 相似度 × 时间衰减 × 信任权重 × 服务版本匹配；向量 + BM25 + 元数据过滤。
- `GET /v1/memory/search`（原 `incidents/search`，**避免与 `GET /incidents/{id}` 抢路径**）：**默认只返回 `CONFIRMED`**，可显式传参包含 `CANDIDATE`。

**跨 Run**：同一 `incident_id` 的新 Run 默认继承前序 Run 的已排除假设（`Hypothesis.status = REJECTED`）与证据摘要（可关）。评测不走这条继承，见 FR-8。

**评测（验收）**：无记忆 / 情景 / 情景+知识库 消融；**记忆污染场景**（历史含错误根因的相似事件，Agent 须用当前证据反驳）；**陈旧场景**（服务已升级）；无收益的层默认关闭并记录结论。

### FR-8 评测框架
- 基准用例 = `incident + scenario_id + expected{category, cause, acceptable_alternatives, key_evidence_keys, verdict, severity}`。`cause` 与 `acceptable_alternatives` 都是 FR-5 的枚举 id，备选 cause 必须属于期望 `category`。期望 `verdict` 不是 `FAULT` 时，`category`、`cause`、`severity` 为空。评分比较报告 `severity` 与场景期望，不读 `user_severity`。
- **证据键**：召回 = `|引用键 ∩ 期望键| / |期望键|`（期望键为空则无定义，不进入平均）；精确率 = `|引用键 ∩ 期望键| / |引用键|`（期望键与引用都为空则无定义，不进入平均；期望键非空但没有引用时为 0；有引用而期望键为空时为 0；红鲱鱼键是合法引用，进入分母）。工具结果只带证据键和数据。`ground_truth` 留在 `scenarios/*.yaml`，只有评分器读取。
- **每次 Pull Request 的门禁**（回放不保护 Prompt 质量，这些检查不跑真实模型的新诊断）：
  1. 单元测试；
  2. 规则基线；
  3. 回放结果与录音一致；
  4. 引用校验器本身通过。
- **夜间真实模型**（报告留档；作品集项目里失败不卡住合并）：
  1. 期望 `verdict` 为 `FAULT` 时的 Top-1（`hypotheses[0]` 的 category + cause，含 alternatives）与 Top-3；主结论是 M4 Top-1 减 M3 Top-1；
  2. 证据键召回与精确率；
  3. 第一次结构化输出的非法引用比例（报告值，不设 0% 硬目标）；校验器接受的报告里非法引用必须为 0，否则该题记错；
  4. `verdict` 是否正确（`UNKNOWN` / `NOT_A_FAULT` / `FAULT` 分开计）；
  5. FAULT 场景的报告 `severity`；
  6. `agreement@k`（k 见 §9）。
- **半成品与空报告**：`BUDGET_EXCEEDED` 和 `TIMED_OUT` 的半成品报告同样评分。`CANCELLED` 不补报告。空报告记为错误。
- **评测隔离**：每次评测新建事件，或强制关闭跨 Run 继承。请求不带 `Idempotency-Key`。评测 Run 不写 `MemoryItem`。
- **冻结题集**：参数化生成的题可以练习。对外报告的分数只来自事先冻结的题集（场景 id + 文件哈希），其中至少 6 题在调 Prompt 期间不打开。
- **稳定性指标**：同题重复 k 次，报告**答案一致的比例**（`agreement@k`，而非 `pass@k`）。温度设为 0 后**仍需报告该比例**。k 不在本文编造，见 §9。
- **LLM-as-judge 仅进夜间报告，不阻断合并**。
- **回放（cassette）的保护范围**：只保护解析器、账本、编排逻辑，**不保护 Prompt 质量**。Prompt/模型变更以夜间真实模型评测留档，不作为合并门禁。
- 轨迹指标：工具调用次数、冗余调用率、无效调用率。
- 对比报告：`eval compare A B`。基准 ≥ 20 题（≥ 4 `UNKNOWN`、≥ 3 `NOT_A_FAULT`、≥ 4 误导性证据、≥ 2 含提示词注入日志）。`POST /v1/evaluations` 与诊断一样返回 `202`，在进程内异步跑。
- 注入文本不改变该题的 `ground_truth`。误导性告警在场景里显式标成 `NOT_A_FAULT` 或 `UNKNOWN`，不在评分时临时决定。初始清单里的这一题标 `NOT_A_FAULT`。

### FR-9 反馈
`POST /v1/runs/{id}/feedback`：`verdict ∈ {correct, incorrect, partial}` + `true_cause`（FR-5 的 cause id，可空）。`correct` 只晋升该报告实际引用过的记忆。`incorrect` 把这些记忆标为 `REJECTED`。`partial` 不改变 `trust_level`。细则见 FR-7。

### FR-10 API（单用户）
```
GET  /health
POST/GET/PATCH /v1/incidents[/{id}]        DELETE = 设置 archived_at
POST /v1/incidents/{id}/runs               新建 202；命中未终态 Run 为 202，命中已终态 Run 为 200
GET  /v1/runs/{id}, /v1/runs/{id}/events (SSE，从 seq=0 回放，支持 Last-Event-ID)
POST /v1/runs/{id}/cancel, /v1/runs/{id}/feedback
GET  /v1/memory/search
POST /v1/evaluations                       202，进程内异步
GET  /v1/evaluations/{id}
GET  /v1/scenarios                         只返回 id、标题、服务、标签；不含 ground_truth
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
`LLMPort` 的调用是异步的。同步 SDK 放进线程，避免堵住单进程事件循环。

### 4.2 设计原则
1. 六边形：Runtime 仅依赖 Port，可全部 Fake。
2. 工具契约统一，Simulator 与真实适配器可互换。
3. **确定性优先**：时间窗、服务、去重、预算、校验由代码完成。
4. 证据账本是结论的唯一来源。
5. 失败即观察。

### 4.3 Scenario Simulator
- YAML：拓扑、指标时间序列、日志模板、变更记录、红鲱鱼、**证据键**、`ground_truth`。工具结果只带证据键和数据，不带 `relevant: true`。
- 参数化生成扩充场景，生成的题可以练习。对外报告只用事先冻结的题集（见 FR-8）。初始场景：高 CPU（`cpu_saturation`）、内存泄漏、连接池耗尽、磁盘满、DNS、错误部署、级联超时、配置错误、流量突增（`verdict=NOT_A_FAULT`，`cause` 为空，不是 cause id）、证据不足（`verdict=UNKNOWN`）、误导性告警（本清单写明 `verdict=NOT_A_FAULT`；其他此类题在场景里写明 `NOT_A_FAULT` 或 `UNKNOWN`）、日志含注入文本（注入不改变该题的 `ground_truth`）。后加的冻结题覆盖多跳依赖（决定性证据在下游服务）、下钻参数（另一个 `service`，或比默认 20 行日志页更窄的时间窗）、同一轮工具结果里互相冲突的证据，以及健康主机上的噪声日志。这些题不把 `ground_truth` 放进 `ToolPort`，工具结果也不带 `relevant: true`。
- 规则基线**不得**访问 `ground_truth`（由 `ToolPort` 隔离，`ground_truth` 只被评分器读取）。`GET /v1/scenarios` 不返回 `ground_truth`。

### 4.4 框架引入理由
| 技术 | 阶段 | 理由 / 验收 |
|------|------|-------------|
| 手写循环 | M4 | 理解协议与预算；作为框架对照 |
| LangGraph | M7 | checkpoint 恢复、审批中断；迁移后基准**不退化**。checkpoint 若要恢复中断的 Run，必须写明它取代 M2 的「重启则 `RUNNING` → `FAILED(restart)`」，并作为对照实验的一部分。在那之前 Run 不能从中断处续跑 |
| MCP | M8 | 工具标准化暴露；对比直连延迟 |
| 多 Agent | M9 可选 | 仅当单 Agent 在跨域题低于阈值且消融有收益 |
| pgvector | M6 | 与业务数据同库 |

### 4.5 安全与护栏
- 只读；Agent 无 shell；建议动作带风险等级、默认需审批。
- **提示词注入**：注入文本保留，只包上「不可信数据」分隔标记；不删除注入句。含注入文本的日志作为评测题，且不改变该题的 `ground_truth`。
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
Hypothesis(id, run_id→, cause, cause_summary, category, confidence,
           status[PROPOSED|LEADING|REJECTED], version)
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
- 假设：调查过程中**以 `Hypothesis` 表为准**；`status` 为 `PROPOSED | LEADING | REJECTED`。跨 Run 继承的已排除假设是 `REJECTED`。`Run.final_report` 只是 Run 结束时的**快照**，不得反向更新表。
- 记忆：只有 `MemoryItem`，无 `HistoricalIncident`。

### 4.7 目录结构
```
app/
  api/  domain/  agent/  tools/(adapters/simulator)  llm/(adapters/fake,openai)
  memory/(M6)  persistence/(M2)  eval/  core/
docs/requirements.md
scenarios/  evals/baselines/  tests/{unit,integration,eval}
```
`domain/` 出现后再启用 `mypy --strict`。

---

## 5. 非功能需求

| 项 | 要求 |
|----|------|
| 性能 | 非 LLM API P95 < 300ms；单次 Run 墙上时钟上限 60 秒，超时即 `TIMED_OUT` |
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
| **M1 场景与基线** | Scenario Simulator、工具契约、证据键、20 题基准、规则基线（不读 `ground_truth`）、评测 Runner/报告 | 基线分数 JSON 写入 `evals/baselines/`；基线公平性自检通过。Postgres 仍从 M2、随第一张表出现 |
| **M2 持久化与异步 Run** | Postgres + Compose（随**第一张表**出现）、SQLAlchemy async、Alembic、Incident/Run/RunEvent、asyncio 执行、SSE、幂等、重启恢复策略、**CLI**（一个命令提交事件、接上事件流、打印报告） | 重启后历史可查；`RUNNING` 被标失败；同 Key 仍在跑返回 `202`，已终态返回 `200`；CLI 提交事件、接上事件流并打印报告 |
| **M3 单次 LLM 诊断（对照组）** | LLMPort + 一家适配器 + Fake；**程序把全部工具结果一次性喂给模型，模型不选择工具**；取数参数只由代码生成（见 FR-3），禁止按题写查询；结构化输出 | 与基线对比成报告。**若模型能选工具，则不属于 M3** |
| **M4 手写 Agent** | 模型自主选工具、账本、预算、护栏、只读 Verifier、账本校验、cassette 回放、`NOT_A_FAULT/UNKNOWN` | 对外主结论是 M4 Top-1 − M3 Top-1；PR 上的回归是回放与录音一致 |
| **M5 评测门禁与可观测** | PR 门禁（单元测试、规则基线、回放与录音一致、引用校验器）、夜间真实模型（Top-1、证据召回与精确率、`agreement@k`）+ judge、OTel | 夜间报告留档。作品集项目里夜间失败不阻断合并 |
| **M6 记忆（情景 + 知识库）** | `MemoryItem`、信任/晋升（仅被引用者）、混合检索、跨 Run 继承、`/memory/search`、此时才加入 `search_memory` | 消融；污染/陈旧场景不盲信；无收益层关闭并记录。新分数不得与已发布的 M3/M4 分数直接相减 |
| **M7 LangGraph + HITL** | 迁移 Orchestrator；checkpoint；审批。M2 的「重启则 `RUNNING` → `FAILED(restart)`」继续有效 | 基准不退化。checkpoint 若要恢复中断的 Run，必须写明它取代这条重启策略，并作为对照实验的一部分。在那之前 Run 不能从中断处续跑 |
| **M8 MCP**（+ 可选 Prometheus） | 自建 MCP Server；Prometheus **一个场景能跑即可，不作主线验收** | MCP 与直连对比 |
| **M9 可选** | 多 Agent / 反馈转基准 | 消融报告 |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| 基线贴天花板 | 规则先冻结；基线 ≥ 90% 则加难场景 |
| 评测抖动 | 温度 0；夜间报告 `agreement@k`；Pull Request 门禁见 FR-8，不含真实模型的 Top-1 |
| 成本 | 分模型预算；便宜模型做 Triage 补全 |
| 上下文爆炸 | 工具侧截断；账本只回传摘要 |
| 范围蔓延 | 严格里程碑；新技术须附引入理由与对照实验 |

---

## 8. 已确认的决策

1. 模型端口先接 **OpenAI**（`OPENAI_API_KEY`，支持官方及兼容接口）与 `FakeLLMAdapter`，另一家只留适配器空位。
2. 目标为作品集；Prometheus 非主线验收。
3. 先 API + CLI。CLI 归 M2：一个命令提交事件、接上事件流、打印报告。Trace 页面待 M4 事件流稳定后再议；浏览器 `EventSource` 不在 M2。
4. 异步为进程内 `asyncio`，单进程，不引入 Redis。
5. 本文档（`docs/requirements.md`）为唯一需求文档，旧版 v1 与评审意见均已合入。
6. 记忆只做情景层 + 服务知识库，无程序性记忆。
7. 单用户，无 `tenant_id`；API 用环境变量共享密钥。

## 9. 仍待确认

这些数不在本文编造。写成配置，或写成当时的实验记录。

- 相似度去重阈值。
- 分页方式。
- `agreement@k` 的 k。
- `UNKNOWN` 正确率的目标值。
- M8 与直连对比时的延迟差。
- M9「单 Agent 在跨域题低于阈值」里的阈值。
