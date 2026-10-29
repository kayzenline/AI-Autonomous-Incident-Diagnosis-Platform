# AgentOps 工程交接文档（Handoff & Engineering Rationale）

> **更新日期**：2026-10-29
> **当前主分支状态**：M2（PR #36）与 M5 工具链（PR #37）已合并，相关 CI 通过。
> **已合入能力**：M0/M1、M3/M4、M2 持久化与异步 Run、M5 离线门禁/夜间评测工具链/OTel。
> **仍需验收**：M2 真实 PostgreSQL 迁移与进程重启链路；M5 真实模型配置与首份测量报告。M6 尚未实现。
> **分数口径**：下文 M3/M4 的 0.85/1.00 来自 fake adapter 与冻结场景，是离线 harness 检查，不是真实模型准确率。

---

## 一、 全局进度总览（我们已经做到哪一步？）

| 阶段 / 里程碑 | 关联 Issue / PR | 核心产物 | 当前状态 |
| :--- | :--- | :--- | :---: |
| **需求收敛 (v2.3)** | PR `#17` (`725fd1b`) | `docs/requirements.md`（统一 M0–M9 规范，移除旧版冗余文档） | ✅ 已合入 `main` |
| **M0：骨架与契约** | `#6, #8, #9` | `app/main.py`、`app/schemas/incident.py`、统一错误体、GitHub Actions CI | ✅ 已合入 `main` |
| **M1：模拟器与规则基线** | `#7, #10, #11, #12, #25, #26` / PR `#31, #32` | `app/tools/`（6 个只读工具 + 脱敏 + `<<UNTRUSTED_DATA>>`）、`scenarios/frozen/*.yaml`（29 个场景）、`app/agent/baseline.py`（Top-1 `0.65`） | ✅ 已合入 `main` |
| **P0/P1 前置改造** | `#29, #30` / PR `#33` | 清理 29 个场景 `incident.title`/`description` 剧透词；新增 `AsyncToolPort` 与 `ThreadedToolPort` | ✅ 已合入 `main` |
| **M3：单次 LLM 对照组** | `#13, #27` / PR `#33` (`8a2d2b8`) | `app/llm/port.py`、`OpenAIAdapter`、`FakeLLMAdapter`、`app/agent/single_shot.py`、`evals/baselines/m3-single-shot.json`（Top-1 `0.85`） | ✅ 已合入 `main` |
| **M4：多步调查循环 + Cassette** | `#14, #28` / PR `#34` (`98eda68`) | `EvidenceLedger`、`HypothesisBoard`、`ReadOnlyVerifier`、`investigate()`、`InvestigativeFakeLLM`、`app/eval/cassette.py`、`evals/baselines/m4-agent.json`（Top-1 `1.00`）、SRE `inspect` / `replay` CLI | ✅ 已合入 `main` |
| **M2：持久化与异步流** | Issue #15 / PR #36 | Postgres + Alembic、Incident/Run/RunEvent、幂等、SSE、CLI | 代码已合并，补充部署验收 |
| **M5：Nightly 评测与可观测性** | Issue #16 / PR #37 | Nightly workflow、agreement@k、judge 评论、OTel | 工具链已合并，待真实测量 |

---

## 二、 每一步的实际工程思路与设计取舍

### 第 0 步：需求收敛与架构边界确立（`docs/requirements.md` v2.3，PR `#17`）
- **问题背景**：早期仓库同时存在 `requirements-v1.md`、`requirements-review.md` 与 `requirements.md`，其中 v1 包含大量无法在单人学期项目内被诚实验证的设定（如多子 Agent 辩论、未实测先写死的准确率数字、混用用户申报严重度与诊断严重度）。
- **实际思路**：
  1. **单一事实来源（Single Source of Truth）**：将需求合并为唯一的 `docs/requirements.md`（v2.3），明确六边形架构（Ports & Adapters）：核心诊断逻辑只依赖抽象端口（`ToolPort`、`LLMPort`、`ClockPort`、`MemoryPort`）。
  2. **分离两类 Severity**：调用方传入的严重度命名为 `IncidentCreate.user_severity`（仅反映报障人主观判断），而诊断结论中的 `DiagnosisReport.severity` 必须由故障影响面证据推导，且非 `FAULT` 结论（`NOT_A_FAULT` / `UNKNOWN`）的 `severity` 必须为 `null`。
  3. **三组对照实验设计（M1 vs M3 vs M4）**：要证明 Agent 循环（M4）真正有工程价值，不能只跑一个 Agent 自嗨，必须建立严格的对照组：
     - **M1（纯代码规则基线）**：固定阈值与静态优先级，单服务扫描；
     - **M3（单次调用 LLM）**：代码先把已知拓扑的工具全扫一遍（Blind Sweep），一次性塞给 LLM 出报告；
     - **M4（手写多步调查 Agent）**：在 `<= 8` 次工具调用预算内，由模型根据上一步观测动态决定下一步查什么、何时跨服务下钻、何时缩小时间窗翻页。

---

### 第 1 步：M1 模拟器、只读工具契约与架构可视化（PR `#31`、`#32`）
- **实际思路**：
  1. **为什么先做确定性 YAML 模拟器（`SimulatorTools`）**：真实生产环境故障不可复现、充满抖动，无法作为回归测试基准。通过 YAML 定义服务拓扑、时序指标点、日志行、变更记录、下游依赖状态与数据库连接池状态，可以让同一个故障场景在毫秒级被重放上千次。
  2. **安全与防注入边界（`app/tools/safety.py`）**：日志和变更描述属于外部不可信数据（真实场景可能包含用户输入或 Prompt Injection，例如冻结集中的 `dns-injection`、`memory-injection`、`pool-injection`）。因此工具层在返回前统一做两件事：
     - 正则脱敏敏感凭据（Bearer Token、API Key、连接串密码）；
     - 使用 `<<UNTRUSTED_DATA>>...<</UNTRUSTED_DATA>>` 包裹不可信文本，并在系统提示词中声明包裹内容仅作为观测数据、绝不作为指令执行。
  3. **静态防作弊门禁（`app/eval/fairness.py`）**：使用 Python `ast` 静态扫描所有 Agent 与 LLM 适配器模块源码，一旦发现任何模块导入 `app.eval`、`simulator` 或源码字符串中出现 `"ground_truth"` / `"user_severity"`，测试直接失败。
  4. **M1 基线水位线**：M1 在 29 个冻结场景（20 个 `FAULT`、5 个 `NOT_A_FAULT`、4 个 `UNKNOWN`）上取得 **Top-1 = `0.65` (13/20)**、**Top-3 = `0.80` (16/20)**，为 M3 和 M4 留出了清晰的提升空间。

---

### 第 2 步：进入 LLM 阶段前的两个关键前置动作（`#30` 去剧透 & `#29` 异步工具端口，PR `#33`）
在开始写 M3/M4 之前，我们站在资深 SRE 与评测公平性视角识别并解决了两个隐患：

1. **Issue `#30`：清除场景文本剧透（De-spoilering）**
   - **隐患**：原 YAML 场景的 `incident.title` 和 `incident.description` 写有类似 `"DNS NXDOMAIN on edge"` 或 `"CPU saturation with disk herring"` 的标题。M1 规则基线不读文本所以不受影响，但一旦接入 LLM，模型根本不需要看工具返回就能从标题猜出答案。
   - **做法**：重写全部 29 个场景的 `incident.title` 与 `incident.description`，只保留值班告警现象（如 `"Edge 502 Bad Gateway alert"`、`"Checkout latency and host alert firing"`），绝不出现根因词或干扰项提示。修改后运行 `python -m app.eval check`，确认 M1 分数分毫不差。
2. **Issue `#29`：异步工具端口（`AsyncToolPort` & `ThreadedToolPort`）**
   - **隐患**：FastAPI 与多步 Agent 循环运行在 `asyncio` 事件循环上，而 `SimulatorTools`（以及未来部分同步 SDK）是同步方法。如果在协程里直接调用同步工具，会阻塞整个事件循环。
   - **做法**：在 `app/tools/port.py` 定义异步协议 `AsyncToolPort`，并提供 `ThreadedToolPort`（内部通过 `asyncio.to_thread` 调度同步 `ToolPort`）与辅助函数 `ensure_async_tools()`。这样同步模拟器零改动即可安全接入异步编排器。

---

### 第 3 步：M3 单次调用 LLM 对照组（`#13` & `#27`，PR `#33`）
- **核心定位**：M3 的任务是回答一个尖锐问题——**“如果不写 Agent 循环，直接用代码把所有服务的监控、日志、变更全抓下来塞给大模型，能做到多少分？它会在哪里失败？”**
- **实际思路（`app/agent/single_shot.py`）**：
  1. **代码全量盲扫（Code-Driven Topology Sweep）**：按场景已知服务列表，对每个服务固定调用 6 个指标 + `search_logs(min_level="ERROR")` + `get_service_status` + `get_recent_changes` + `get_dependency_health` + `query_db_health`（单服务 11 次工具调用，双服务 22 次，三服务 33 次，平均每次运行 **12.90 次**工具调用）。
  2. **证据白名单与单次重试统计**：所有工具返回的 `evidence_keys` 汇总为 `allowed_evidence_keys`。若模型第一次输出包含非法 `evidence_key` 或 Schema 错误，触发最多 **1 次**重试。
  3. **严禁重写首次非法引用率**：我们在 `SingleShotResult` 中分别记录 `first_output_illegal_citation_rate`（首次输出的非法引用占比）与 `accepted_illegal_citation_rate`（最终采纳报告的非法引用占比，必须为 `0.0`）。即使重试成功，`first_output_illegal_citation_rate` 依然保留首次真实数值。
- **M3 的实测表现与暴露出的 3 个本质缺陷（Top-1 = `0.85`，17/20）**：
  - M3 相比 M1 解决了跨服务多跳场景（因为代码把下游服务也扫了），但**在以下 3 个场景必然失败**：
    1. `drilldown-truncated-dns`（预测 `UNKNOWN`，Miss）：默认 30 分钟窗口内前 20 条 ERROR 日志全是缓存噪音（返回 `truncated=True`），真正的根因日志 `log:late_nxdomain` 在第 21 条（`03:08:00Z`）。**单次盲扫无法根据 `truncated=True` 缩小时间窗二次查询。**
    2. `cpu-with-disk-herring`（预测 `disk_full` 为 Top-1，Miss）：主机 CPU（97%）与磁盘（96%）同时超阈值，静态优先级无法主动结合“磁盘无任何写入报错日志”来证伪 `disk_full`。
    3. `dependency-with-cpu-herring`（预测 `cpu_saturation` 为 Top-1，Miss）：下游 `payments` 超时（`1900ms`）导致调用方 `catalog` 尾延迟飙升且伴随 CPU 96%，单次静态排序误将本地 CPU 排在下游超时之前。

---

### 第 4 步：M4 手写多步调查循环、Cassette 回放与 SRE 工具链（`#14` & `#28`，PR `#34`）
M4 是整个平台的核心大脑。我们没有使用黑盒框架，而是手写了完整的状态机与防护体系：

#### 4.1 架构分层（`app/agent/`）
1. **证据账本 `EvidenceLedger`（`app/agent/ledger.py`）**：
   - 作为单次调查的唯一事实来源，记录每次工具调用的参数、返回摘要、时间窗与 `evidence_key`。
   - **重复调用缓存拦截（`find_cached_call`）**：对 `(tool, arguments)` 生成规范化签名。当模型在同一轮调查中重复请求相同工具与参数时，编排器直接拦截并返回缓存结果，发出 `GUARDRAIL_TRIGGERED(reason="duplicate_tool_call")` 事件，**不消耗 8 次工具调用预算**。
2. **假设看板 `HypothesisBoard`（`app/agent/ledger.py`）**：
   - 维护候选根因的生命周期（`PROPOSED` -> `LEADING` / `REJECTED`）及版本号 `version`。
   - 一方面在 SRE 轨迹中清晰展示“为什么排除了某个红鲱鱼干扰项（如 `disk_full -> REJECTED`）”；另一方面当触发超时（`TIMED_OUT`）或预算耗尽（`BUDGET_EXCEEDED`）时，直接从看板提取当前活跃假设生成降级部分报告（Partial Report）。
3. **只读校验器 `ReadOnlyVerifier`（`app/agent/verifier.py`）**：
   - **物理隔离**：`ReadOnlyVerifier` 的构造函数只接收 `(EvidenceLedger, HypothesisBoard)`，没有任何 `ToolPort` 引用，从代码结构上保证 `verifier_tool_calls == 0`。
   - **单次校验职责**：校验 JSON Schema、`cause`/`category` 映射一致性、所有引用 `evidence_key` 100% 存在于 `EvidenceLedger`，并拒绝将看板上已标记为 `REJECTED` 的假设作为 Top-1 根因输出。
4. **双时钟解耦（`WallClock` vs `ScenarioClock`）**：
   - 在 `investigate()` 中，`ScenarioClock` 仅用于推导历史故障查询窗口（`default_window`），而 `WallClock` 专门用于计算 `wall_timeout_seconds`（默认 60s）与 `TraceEvent.created_at`。在单元测试中推进虚拟 `WallClock` 可精确触发 `TIMED_OUT`，而不会篡改历史查询时间窗。

#### 4.2 为什么 M4 能在 `<= 8` 次工具调用内拿到 `Top-1 = 1.00`？（`app/llm/adapters/investigator_fake.py`）
我们按照资深值班 SRE 的真实排查路径设计了按需决策树（平均每次运行仅需 **6.31 次**工具调用，最高 **8 次**，比 M3 减少 **51%** 工具调用）：
1. **第 1 步（看错误日志）**：先查故障服务 `search_logs(min_level="ERROR")`。
   - **动态翻页下钻（解决 `drilldown-truncated-dns`）**：若返回 `truncated=True` 且第 1 页无决定性故障特征词，立即取第 1 页最后一条日志时间戳 `+1s` 作为新起点，发起窄窗口 `search_logs`，仅用 3 次工具调用即捕获 `log:late_nxdomain`。
2. **第 2 步（看错误率与监控可用性）**：查 `query_metrics(metric="error_rate")`。
   - 若监控后端不可用（如 `unknown-metrics-down`），仅用 **2 次**工具调用立即中止并诚实返回 `UNKNOWN`，绝不浪费预算。
   - 若第 1 步日志已明确命中 DNS NXDOMAIN，此时拿到 `error_rate` 定级后仅用 **2–3 次**工具调用即可收敛出报告。
3. **第 3–5 步（按需验证专项日志、变更与多跳依赖）**：
   - 若日志命中磁盘满或连接池耗尽特征，定向补查 `disk` 或 `db_connections` + `query_db_health` 进行交叉验证（防止被 `noisy-pool-warmup` 这种健康预热日志误导）。
   - 若 `error_rate >= 5%`，检查 `get_recent_changes`（捕获 `bad_deploy` / `config_error`）与 `get_dependency_health`。
   - 若存在下游依赖且本地无变更/无决定性报错，自动沿依赖链下钻（支持二跳依赖 `checkout -> inventory -> pricing` 与下游隐性连接池满 `drilldown-payments-pool`），均严格控制在 `7–8` 次调用内完成。
4. **第 6 步（交叉证据加权消歧，解决红鲱鱼）**：
   - 在 `cpu-with-disk-herring` 中，虽然 `disk=96%`，但 `search_logs` 中无任何磁盘 I/O 错误，而 `cpu=97%`，因此将 `cpu_saturation` 置为 `LEADING`、将无日志支撑的 `disk_full` 置为 `REJECTED`。
   - 在 `dependency-with-cpu-herring` 中，下游 `payments` 明确处于 `timeout (1900ms)` 且调用方 `latency_p99=1800ms`，因此将 `dependency_timeout` 置为 `LEADING`、将并发积压引起的本地 `cpu_saturation` 置为 `REJECTED`。

#### 4.3 Cassette 确定性录制与回放（`#28`，`app/eval/cassette.py`）
- **为什么需要 Cassette**：PR CI 不能依赖真实外部 LLM API（既慢、花钱又会因为模型端微调导致 PR 无故挂掉），但 PR CI 又必须测试真实的 `investigate()` 编排器、JSON 解析器、`EvidenceLedger`、`ReadOnlyVerifier` 和 `SimulatorTools`。
- **做法**：將 29 个场景的模型响应序列录制到 `evals/cassettes/m4-frozen.json`。每次运行 `python -m app.eval check`（含 GitHub Actions PR CI）或 `python -m app.eval replay` 时，`ReplayLLM` 会将录制的响应逐条回放进真实的 `investigate()` 循环，并严格比对最终状态、`ledger_keys`、工具调用次数、`first_output_illegal_citation_rate` 以及完整报告结构。任何漂移或篡改都会抛出 `CassetteMismatchError` 并阻断 PR。

#### 4.4 面向值班 SRE 与开发者的实用 CLI（`app/eval/runner.py`）
为了让开发者和值班 SRE 能直观看到 Agent 每一步在想什么、查了什么工具、拿到了什么证据，我们在 `python -m app.eval` 中提供了全套诊断命令：

```bash
# 查看单场景完整多步调查时间线、证据账本、假设流转、校验结果与 Token/预算消耗
.venv/bin/python -m app.eval inspect drilldown-truncated-dns --mode m4
.venv/bin/python -m app.eval inspect cpu-with-disk-herring --mode m4

# 输出机器可读的完整 InvestigationResult JSON（含每条 TraceEvent 明细）
.venv/bin/python -m app.eval inspect multihop-checkout-pricing-dns --mode m4 --json

# 离线重放 29 个场景的 Cassette 并验证零漂移
.venv/bin/python -m app.eval replay

# 一键校验 M1 / M3 / M4 基线、对比报告、场景哈希清单与 Cassette 重放
.venv/bin/python -m app.eval check
```

---

## 三、接手顺序：验收 M2，完成 M5 实测，再进入 M6

### 1. M2 部署验收

主要实现已在 PR #36 合并，不需要重新开发。Issue #15 的收尾证据由
`tests/test_m2_acceptance.py` 提供：Alembic 创建数据库，真实 uvicorn 子进程
启动，CLI 提交并读取 SSE，保存诊断结果，然后停止进程、启动另一进程，
检查历史报告和事件可查、Last-Event-ID 续传、幂等，以及残留 PENDING/RUNNING
转为 FAILED(restart)。同时验证活动运行冲突、归档事件拒绝新运行及证据关联。

普通 pytest 使用临时 SQLite；CI 的 postgres-acceptance job 使用仓库的
docker-compose.yml 启动 PostgreSQL 16，执行同一验收。仅将
M2_TEST_DATABASE_URL 指向专用测试库；测试会迁移并写入诊断数据。

```bash
docker compose up -d --wait
M2_TEST_DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5432/agentops   .venv/bin/pytest -q tests/test_m2_acceptance.py
```

归档由 archived_at 表示，不存在 ARCHIVED 状态。部分报告、取消、
ToolCall/Evidence 事务与引用规则还由现有单元测试覆盖。

### 2. M5 首次真实测量

PR #37 已提供离线门禁、夜间报告、agreement@k、judge 评论与 OTel。
先明确供应商、模型、重复次数和 judge 模型，配置 Actions secret/variables，
再手动运行 nightly-eval 并核查报告。配置方法见 README。
保留模型、提交、冻结场景哈希和所有重复报告；不能把 fake smoke 当成实测。

### 3. M6 记忆消融

接下来才引入 MemoryItem、反馈信任、检索和跨 Run 继承。
先保留 M5 无记忆的真实对照，再做无记忆/情景记忆/情景+知识库消融。
遵循 requirements.md FR-7：仅晋升实际引用的记忆，污染和陈旧记忆必须
接受当前证据验证；评测不继承前一次 Run，也不写入 MemoryItem。
