# Patent-Agent v6 技术规格（字段级实现蓝图）

> 本文档是 v6 的**实现规格**而非架构介绍。每个机制按统一六要素描述：
> **① 机制定义（含"不是什么"边界）→ ② 核心数据结构（字段级）→ ③ 执行流程（伪代码）
> → ④ Failure cases（枚举+处置）→ ⑤ Eval 指标 → ⑥ Baseline/对照实验。**
> 评审通过后，本文档既是编码蓝图，也是设计自检材料（§11 为机制追问清单）。
>
> 设计立场：上层 7 个 Agent Intelligence 机制做深、
> 下层 Runtime 基座（EventStore/Resume/Replay）+ 专利垂直纵深（CNF ACI/法律护栏/HITL）守住。
> 四个自觉约束：Plan DAG 不做通用调度引擎；Router 不做伪学习；上下文选择用确定性规则不额外调 LLM；
> worker 只在 handoff 契约内自治，越界动作必须上报。

---

## 0. 全局约定

- 语言 Python 3.14，stdlib + 现有 fastapi；LLM 仅 OpenAI 兼容 Chat Completions（flash 编排 / pro 子agent）。
- 所有 LLM 输出走 `LLMClient.chat_json`（json_object + 围栏剥离 + 缺键修复，已实现）。
- 所有时间戳 `HH:MM:SS`（UI）/ ISO8601（持久化）；金额/计数用 int；枚举值用小写下划线英文。
- 每个机制产出的事件都必须写入 EventStore（§R0），事件是唯一状态来源。

---

# 第一部分 · Runtime 基座（R0-R1）

## R0 · EventStore（append-only 事件日志）

### ① 机制定义
所有 Action/Observation/Plan 修订/Handoff/裁决/检查点都是**只追加、有序、不可变**的事件；
运行态 State 是事件流的折叠结果，不存在独立可变状态表。
**不是什么**：不是普通操作日志（日志可丢、无 schema）；事件有强类型、有序号、可回放、是恢复依据。

### ② 数据结构
```sql
-- 扩展现有 case_events 表
case_events(
  case_id TEXT, seq INTEGER, run_id TEXT, ts TEXT,
  actor TEXT,            -- supervisor | search_team | evidence_team | critic | human | system
  etype TEXT,            -- 见事件枚举
  tag TEXT, type TEXT,   -- UI 展示用（沿用现有 tag/type）
  payload TEXT,          -- JSON，各 etype 的 schema 见 §9
  causation_id TEXT,     -- 触发本事件的上一事件 seq（因果链，trajectory 分析用）
  PRIMARY KEY(case_id, seq)
)
```
事件枚举：`plan.created / plan.revised / step.started / step.done /
action.emitted / observation.recorded / handoff.opened / handoff.returned /
failure.classified / recovery.selected / guard.verdict / critic.issued /
checkpoint.written / human.approved / run.finished`。

### ③ 执行流程
- 每个组件产出事件时调 `events.append(etype, actor, payload, causation_id=当前seq)`，单连接写锁；
- `fold(case_id, until_seq)`：按 reducer 表逐事件重建 State（§M1）；
- SSE 推送与写库用同一份事件对象（现有 worker 队列模式）。

### ④ Failure cases
| 情况 | 处置 |
|---|---|
| 写事件失败（DB 锁/磁盘） | 该 turn 失败，错误 withholding，重试 3 次；事件不完整不允许写 checkpoint |
| 事件序号冲突 | 单写锁保证不发生；发生即 bug，fail-fast |
| payload 缺字段 | append 层做 schema 校验，拒绝写入并告警（不许脏事件进流） |

### ⑤ Eval 指标
事件完整率（实际写入/预期）=100%（硬门禁）；replay 折叠出的 State 与实时 State 一致率。

### ⑥ Baseline
v2 已有 35 条事件的扁平日志；v6 对比：事件类型从 6 扩到 16、带 causation 链、可 fold 出 Plan。

---

## R1 · Checkpoint / Resume / Fixture-Replay

### ① 机制定义
- **Checkpoint**：每个 Plan step 完成时写 `{run_id, seq_cursor, plan_snapshot_id, counters}`；
- **Resume**：HITL 审批后/断连后从游标 fold 重建，已完成的付费动作有幂等键不重复扣费；
- **Replay**：给定历史事件 + 录制的外部响应 fixtures，**离线重放**整条轨迹，零外部调用、零付费。
- **术语纪律（采纳建议 §14）**：对外只称 **"fixture-based deterministic replay"**——
  确定性来自 fixtures 对 LLM/API 响应的录制，不暗示 agent 天然确定。

### ② 数据结构
```pythonc
Checkpoint = {"run_id": str, "seq": int, "plan_snapshot": PlanDAG,   # M1
              "counters": dict, "written_at": ISO8601}
Fixture    = {"key": hash(请求方法+URL+归一化body), "response": 任意, "recorded_at": str}
# read 幂等键
READ_IDEM_KEY = sha1(case_id + "|" + sorted(family_keys))
```

### ③ 执行流程
1. step.done 后写 checkpoint；
2. resume：加载最新 checkpoint → fold 到 seq → 从下一个 ready node 继续；
   detail 调用前查 `read_idem` 表（族集合→已拉全文），命中直接复用；
3. replay：ReplayClient/ReplayAminer 拦截所有出站请求，按 key 查 fixture，命中返回，未命中报缺录；
   shadow replay：新 runtime 跑旧 fixtures，产出新轨迹与旧轨迹做 action-diff（M7）。

### ④ Failure cases
| 情况 | 处置 |
|---|---|
| resume 时发现游标后有孤儿事件 | 以 checkpoint 为准截断重放（孤儿事件标记 superseded） |
| fixture 缺失 | replay 立即失败并列出缺失 key，不许静默打外网 |
| 幂等键碰撞（不同族集合同 hash） | key 含族 key 全集，碰撞概率可忽略；发生则 fail-fast |
| 长时间运行后 token 过期 | resume 时重新建 LLMClient（凭证不持久化进 checkpoint） |

### ⑤ Eval
resume 成功率（断连模拟后状态零丢失比例）；resume 重复付费次数 = 0；replay 轨迹一致率（相同 fixtures 下 action 序列一致，温度固定 0）。

### ⑥ Baseline
v2 是"审批后整轮重跑"（付费会重复）；v6 对照：resume 重复付费 2 次 → 0 次。

---

# 第二部分 · 七个 Agent Intelligence 机制

## M1 · Dynamic Plan DAG（动态规划，S 级之首）

### ① 机制定义
Supervisor 持有一个**有界 DAG 形式的计划**：节点是带意图的待执行 step，边是依赖；
每轮根据观察**局部修改 DAG**（插入/重试/跳过节点），而不是重新输出一段计划文本。
**不是什么**：不是通用 DAG 调度引擎（无条件边谓词引擎、无子图嵌套）；查新拓扑有界：
parse → N×search（并行）→ N×read（依赖 search）→ critique → submit，外加失败时动态插枝。

### ② 核心数据结构
```pythonc
PlanDAG = {
  "plan_id": "plan_0001", "revision": 1,
  "goal": "判定权利要求要素相对 2018-2026 公开专利的新颖性/创造性",
  "nodes": [PlanNode],
  "assumptions": [str], "open_gaps": [str],
}
PlanNode = {
  "node_id": "s2", "intent": ENUM,        # anchor_search|gap_search|deep_read|targeted_read|critique|submit
  "status": ENUM,                         # pending|ready|running|done|skipped|failed
  "depends_on": [node_id],                # DAG 边（提交时做环检测）
  "owner_team": ENUM,                     # search|evidence|review
  "args": dict,                           # 动作参数（searches/patent_ids/feature_idx…）
  "result_ref": str|None,                 # 产出物事件引用（观察/chart id）
  "origin": ENUM,                         # initial | inserted_by_critic | inserted_by_recovery | retry
  "origin_ref": str|None,                 # 触发插入的 critique/failure 事件 seq
  "attempts": int,
}
# 折叠 State
FoldedState = {"pool": [...], "charts": {...}, "counters": {...}, "graph": EvidenceGraph}
```
LLM 修订输出（强约束 JSON）：
`{"decision":"advance"|"revise", "revisions":[{"op":"insert|retry|skip|update_args",
"node":…, "depends_on":…, "reason":…}], "open_gaps":[…], "assumptions":[…]}`

### ③ 执行流程（伪代码）
```
plan = initial_dag(case)                      # 静态模板生成，不花 LLM
while plan 无终止节点完成:
    ready = [n for n in plan.nodes if n.status=="ready" and deps_done(n)]
    if ready:                                 # 有可执行节点：直接执行（不调规划 LLM）
        for n in parallel_if_safe(ready): execute_via_team(n)
    else:                                     # 全 blocked/failed：规划 LLM 决策
        obs = compress_for_planner(plan, state)
        out = llm.plan_revise(system_plan, obs)   # flash, 结构化 revisions
        apply_revisions(plan, out)           # 环检测/同词重复拦截/次数闸门
        emit(plan.revised, causation=上一seq)
    for n in finished_now: checkpoint.write(n)
```
**关键：LLM 只在"无路可走或需要判断"时被调用**，常规推进零 LLM 成本——这是与 ReAct 每轮调模型的本质差异之一。

### ④ Failure cases
| 情况 | 处置 |
|---|---|
| revisions 造成环/引用不存在节点 | apply 层拒绝，三要素错误回喂，attempt+1 |
| 同角度空结果后提交相同 args | 动作校验拦截（v2 实测坑），建议换词 |
| revise 无 reason | schema 拒绝（修订可追溯是硬要求） |
| revision 次数到顶（≤3） | 停止插入，以现有结果进入 critique/submit，stop_reason=max_search_rounds |
| 规划 LLM 不可用 | fallback：模板化推进器（v2 固定流水线），事件标注 degraded |

### ⑤ Eval 指标
- replan_quality：每次修订后**新增有效候选数/补全证据特征数**（修订必须带来增量，否则记无效修订）；
- 无意义动作率：未带来状态增量的 action 占比；
- 规划 LLM 调用次数/案（成本视角，常规推进应接近 0-2 次）；
- DAG 合法性 100%（无环/无悬挂依赖）。

### ⑥ Baseline/对照
B0 v2 固定流水线（无 plan）；B1 纯 ReAct（每轮 LLM 选 action，无 plan 实体）；
v6 PlanDAG。对照指标：无意义动作率、replan 后召回增量、规划 token 成本。
**预期可讲结论**：DAG 常规推进免 LLM 调用，规划 token 显著低于 ReAct，且修订有因果可追踪。

---

## M2 · Supervisor 两层层级 + Typed Handoff

### ① 机制定义
两层：**Supervisor（Planner）→ 3 个 Team Lead（search/evidence/review）→ Lead 下 worker**。
Team 间通过强类型 Handoff 契约交接，契约即事件。
**自治边界（自觉约束）**：worker 仅能在契约内自救（reader 9k→4.5k 窗口）；
任何跨策略动作（换检索角度、追加预算、改读其他专利）必须以 request 回 Supervisor 改 DAG——
层级不被旁路。

### ② 核心数据结构
```pythonc
Handoff = {
  "handoff_id": "ho_0007", "from": "supervisor", "to": "evidence_team",
  "task": {"goal": str, "constraints": [str], "success_criteria": [str]},
  "context_refs": [str],          # 事件游标/plan 快照引用，不复制全文
  "evidence_refs": [str],         # patent refs / chart refs
  "allowed_tools": [str],         # 白名单，越界工具调用在执行层拒绝
  "budget": {"paid_reads": int, "tokens": int},
  "deadline_ms": int,
  "return_schema": "ChartsV1",    # 强类型返回契约名
}
HandoffResult = {"ho_ref": str, "status": "ok|partial|failed",
                 "outputs_ref": str, "tokens": dict, "paid_used": int,
                 "requests": [Request]}     # worker 越界请求（反向 handoff）
CritiqueHandoff = {"from":"review_team","to":"supervisor",
                   "gap_nodes":[EvidenceNodeRef], "conflicts":[...],
                   "suggested_actions":[修订建议]}   # M4
```

### ③ 执行流程
1. Supervisor 对 ready 节点按 owner_team 开 handoff（带预算/工具白名单/成功标准）；
2. Team Lead 拆分子任务派 worker（search worker 按角度、reader 按专利），
   worker 独立上下文窗口，结果只回 Lead；
3. Lead 聚合 → HandoffResult；越界请求不自行执行，随结果回 Supervisor；
4. Supervisor 把 requests 喂给下一次 plan 修订（成为 inserted node 的 origin_ref）。

### ④ Failure cases
| 情况 | 处置 |
|---|---|
| worker 调白名单外工具 | 执行层拒绝，记越界事件；两次越界该 worker 失败 |
| handoff 超时 | deadline 后 Lead 回 partial + 已完成部分；Supervisor 决定 retry/skip |
| 预算超支 | 付费动作在执行层拦截，转 ask_human（G6/Policy） |
| 返回不符 return_schema | schema 校验失败，Lead 层重试一次，再失败 handoff failed |

### ⑤ Eval 指标
- handoff_efficiency：回传 outputs 被下游实际引用的比例（无引用=无效交接）；
- 越界拦截率与越界后合理升级率；
- 隔离收益：子 agent 上下文平均 token vs 全量塞主上下文的 token（对照实验量化）。

### ⑥ Baseline/对照（核心实验，见 §8）
Single（无团队，主 agent 全干）/ Supervisor+Workers（无 critic）/ Full（含 review team）
三组同 fixtures 跑，比较 recall、grade_accuracy、token、成本、时延——**如实报告 trade-off**。

---

## M3 · Capability-aware Tool Router

### ① 机制定义
Agent 只声明 `intent`，Router 从注册执行器中按 **capability / cost / availability**
规则打分选一个执行，并在失败时按序 fallback。
**不是什么（自觉约束）**：不做"学习历史成功率的智能 router"——样本量不足时统计无意义；
路由决策是确定性规则 + 熔断状态，trajectory-based 选择仅留接口。

### ② 核心数据结构
```pythonc
ToolSpec = {"name": str, "capabilities": [ENUM],   # parse|coarse_search|bibliographic|fulltext
            "cost_kind": "free|paid|tokens", "cost": float,
            "read_only": bool, "concurrency_safe": bool,
            "timeout_ms": int}
RouteScore = {"tool": str, "capability": float, "cost_score": float,
              "available": bool, "total": float}
RouteDecision = {"selected": str, "candidates": [RouteScore], "reason": str}
CircuitState = {"tool": str, "fail_window": [ts], "open": bool}   # 近窗失败率熔断
```
现有执行器：`local.parse(tokens)`、`aminer.search(free)`、`aminer.info(free)`、
`aminer.detail(paid,budget_gated)`；预留 `remote.*`（MCP adapter，无真实源不接）。

### ③ 执行流程
```
def route(intent, ctx):
    cands = [t for t in registry if intent in t.capabilities]
    for t in cands: score(t)   # capability 规则表(0/0.5/1)；cost: free0 < tokens < paid
    if circuit_open(t): t.available=False
    pick = highest(cands)
    try: return exec(pick)
    except classified e: recovery.route_fallback(pick, e) → next candidate → 重试
```
打分规则表写在 `router.py` 常量区（可解释，不搞黑盒权重）；每次决策进 trace（M7 数据源）。

### ④ Failure cases
| 情况 | 处置 |
|---|---|
| 无可用执行器 | PLAN_INVALID：三要素错误回 Supervisor（如只有 paid 能满足且无预算→ask_human） |
| 选中执行器熔断 | 自动次选；全部熔断→TOOL_TRANSIENT 交 Recovery Engine |
| paid 执行器余额不足 | 5002→免费路径降级（摘要档/info 号单），事件明示覆盖度下降 |

### ⑤ Eval 指标
tool_efficiency：路由选择与"事后最优执行器"（fixtures 下已知哪个会成功）一致率；
fallback 必要性（fallback 后成功率）；平均执行成本。

### ⑥ Baseline
B0 硬编码直调（v2）；v6 Router。预期：正常路径零额外成本（规则表查表），
失败路径选择正确率可在 fixtures 上算出。

---

## M4 · Critique → Repair 反思闭环（Evidence Graph 驱动）

### ① 机制定义
不是"reviewer 打分"。Critic 遍历 **Evidence Graph** 找三类结构性问题
（证据缺口 / 分级冲突 / 证据薄弱），产出结构化 Critique；
Supervisor 据此在 DAG 插入 repair 节点（定向检索/定向 read）；修复后 Critic **复验同一节点**。
闭环：**Generate → Critique → Repair → Verify**。法律形式问题（X 自洽/日期/同族）走确定性护栏，
Critic 只管内容充分性——双轨，不把法律判断交给 LLM。

### ② 核心数据结构（内存图，dict/JSON 落盘，无图数据库）
```pythonc
Node: ClaimElement{cid,text}  Patent{pid,pubno,pub_date,kind}
      Evidence{eid,pid,section,paragraph_index,quote,quote_match:float}
Edge: (ClaimElement)--covers?-->ClaimElement?  # 交底特征即要素清单
      (ClaimElement)-[disclosed:{level:是|部分|否}]->(Evidence)-[from]->(Patent)
      (Patent)-[grade:{reader_suggest,critic_verdict}]->Grade
Critique = {"round": int, "gaps": [{"cid": str, "problem": ENUM,   # missing_evidence|weak_quote|uncovered
                                    "suggested_action": {"intent":str,"args":dict}}],
            "conflicts": [{"pid":str,"reader_grade":str,"issue":str,"verdict_grade":str}],
            "passed": bool}
```

### ③ 执行流程
1. Readers 回 charts → 构图（确定性：节点/边由 chart JSON 投影，LLM 不自由发挥图结构）；
2. Critic（pro，上下文=图摘要 + 仅相关段落 ≤3k + 分级 rubric）输出 Critique；
3. passed=true → M-submit；否则 CritiqueHandoff 回 Supervisor；
4. Supervisor 把每个 gap 转成 `targeted_read/targeted_search` 节点插入 DAG（origin=critic）；
5. repair 完成后图更新，Critic 仅复验原 gap 节点；≤2 轮，仍不过→结论强制保守措辞（G8）+标注人工复核。

### ④ Failure cases
| 情况 | 处置 |
|---|---|
| Critic 输出的 patent/paragraph 不存在 | 三要素错误，重试一次；再失败跳过该条 critique（fail-open，不阻塞） |
| quote 与段落不匹配 | 确定性 G1 先拦（不进 LLM），用原文兜底 |
| repair 后证据仍缺 | 保守降级 + 报告显式标注"该特征未检索到直接对比文件" |
| Critic 与 Reader 对 X/Y 分歧 | GRADE_CONFLICT：证据规则仲裁（X 须全要素覆盖），保守降级优先 |

### ⑤ Eval 指标
gap 修复率（一轮 repair 后 gap 闭环比例）；critique 精确率（指出的 gap 人工判定为真问题的比例，防乱挑刺）；
evidence_precision（引用真实支持，G1 兜底目标 1.0）；grade_accuracy（vs golden 标签）。

### ⑥ Baseline/对照
B0 v2 reader 无 critic；B1 critic 但无 repair（只出意见）；v6 完整闭环。
**核心结论候选**：repair 闭环使证据完备率（应挂证特征挂证比例）从 x→y。

---

## M5 · Failure-aware Recovery Engine

### ① 机制定义
统一错误分类器 + 策略矩阵：任何组件抛错先分类，再匹配恢复策略，
恢复动作作为信号回 Planner。错误**withholding**（可恢复错误不提前终止 SSE），每个策略带熔断。

### ② 核心数据结构
```pythonc
Failure = {"fid": str, "class": ENUM, "source": str, "detail": str, "retriable": bool}
# ENUM: TOOL_TRANSIENT|TOOL_AUTH|TOOL_BUDGET|ZERO_RESULT|EVIDENCE_GAP|
#       GRADE_CONFLICT|PLAN_INVALID|LLM_DEGRADED|TIMEOUT
RecoveryPolicy = {"strategy": ENUM, "max_attempts": int}
# strategy: retry_backoff|fallback_executor|rewrite_query|targeted_action|
#           arbitrate_conservative|ask_human|degraded_pipeline
RecoveryRecord = {"failure_ref": str, "strategy": str, "attempts": int,
                  "outcome": "recovered|escalated|degraded", "new_state_ref": str}
```

### ③ 策略矩阵（全部已在 v2 有零散实现，v6 系统化）
| class | 首选策略 | 升级 | 终态 |
|---|---|---|---|
| TOOL_TRANSIENT/TIMEOUT | 退避重试（0.55s 限速器）×3 | fallback 执行器 | LLM_DEGRADED |
| TOOL_AUTH | 不重试 | 告警终止 | run 失败，明确提示 |
| TOOL_BUDGET | 免费路径降级 | ask_human 加额 | force_submit_budget |
| ZERO_RESULT | 空结果引导+禁止同词，要求换角度（回 Planner） | 角度插枝≤2 | 判为区别特征停止纠缠 |
| EVIDENCE_GAP | Critic repair 节点 | ≤2 轮 | 保守标注 |
| GRADE_CONFLICT | 规则仲裁 X→Y | — | 记录分歧 |
| PLAN_INVALID | 三要素回喂修订 ×2 | — | fallback 管道 |
| LLM_DEGRADED | 各级降级（parser stub/reader 骨架/planner 模板） | — | degraded 标记 |

### ④ Failure cases（引擎自身）
- 恢复策略本身失败：按升级列上移；所有计数器在 State 中持久化，resume 后不重置不重复消耗预算；
- 恢复死循环：每个 class 独立熔断（CC 教训），超限即升级终态。

### ⑤ Eval 指标
recovery_success_rate（分类后最终恢复比例）；recovery_steps 中位数；
误分类率（fixtures 下人工标注错误类型对照）；恢复后产物达标率；
**失败注入测试**：主动注入 timeout/空结果/坏 JSON/余额不足，验证每条路径。

### ⑥ Baseline
B0 v2 散落 try/except（部分错误无恢复）；v6 矩阵覆盖率 = 枚举的 9 类全部有注入测试。

---

## M6 · Experience Memory（建议态，克制实现）

### ① 机制定义
从"任务→轨迹→结果→人工修正"提取经验条目；Planner 规划时检索相似情境，
以**带来源的建议**注入，不自动改行为；样本 <10 条不启用。
**不是什么**：不是知识库 markdown；也不是自动策略学习（不宣称 learned agent）。

### ② 数据结构
```pythonc
Experience = {"exp_id": str, "situation": str,              # 特征化情境（领域/IPC/现象）
              "attempted_strategy": str, "outcome": ENUM,   # good|bad|neutral
              "human_correction": str|None,
              "measured_gain": float|None,                  # 有 golden 对照才填
              "source_case": str, "confidence": float, "created_at": ISO8601}
```

### ③ 流程
案件结案/HITL 修正落库 → 离线（非 run 中）由 flash 抽取候选经验 → 入表（可人工审）；
Planner 初始 system 动态区注入 ≤3 条最相似经验（标题+来源案件+"可忽略"）；
被采纳且事后效果好 → confidence+，反之衰减。

### ④ Failure cases
小样本过拟合（门槛禁用）；经验互相矛盾（取高 confidence 且并列展示）；客户数据合规（经验条目不含交底书原文，只含抽象策略，可审计）。

### ⑤ Eval 指标
建议采纳率、采纳后策略收益（golden 集对照）、错误建议率（人工抽检）。
### ⑥ Baseline
无记忆 vs 建议态记忆；golden 集 ≥10 件后才报数，此前文档明确标注"样本不足，未启用统计结论"。

---

## M7 · Trajectory Evaluation（结果 + 过程双层评测）

### ① 定义
基于 EventStore 自动计算过程指标；配合 golden fixtures 做结果指标与消融对照。
评测本身零 LLM 成本优先（规则可算的不用 judge），仅"计划合理性/批判质量"用 LLM-judge 且固定温度 0。

### ② 指标定义（全部从事件流计算）
| 指标 | 公式/来源事件 |
|---|---|
| planning_efficiency | 带来状态增量的 action / 总 action |
| replan_quality | Σ(修订后新增有效候选+补全特征) / 修订次数 |
| tool_efficiency | Router 选择与事后最优一致次数 / 路由次数（fixtures 已知成败） |
| recovery_quality | recovered / classified；恢复步数中位数 |
| handoff_efficiency | 下游引用的 outputs / 总 outputs |
| context_efficiency | 有效 evidence token / 主上下文峰值 token；缓存命中率 |
| 结果层 | recall@20 / fine_precision / grade_accuracy / evidence_precision(=1.0) / 成本/时延 |

### ③ 输出
每案一份 JSON scorecard + 汇总表；消融跑批量出柱状图。
### ④ Failure
fixture 不全→禁止出对比结论（缺录即报错，沿用 R1）；LLM-judge 不一致→双人设复读取交集。
### ⑤ Baseline
见 §8 消融矩阵。
### ⑥ 对照
同 fixtures、温度 0、仅 harness 版本不同，保证差异来自机制而非模型波动。

---

# 第三部分 · 垂直纵深（守住，不被"通用化"稀释）

## V1 · 专利 ACI（已有，作为工具层一部分）
CNF 编译器（fanout/截断/锚点+单角度补检）、族感知预算闸门、IPC 双格式清洗、
段落切分（§章节 ¶序号）、限速退避——这些是 Tool Router 下的 native 工具，
也是 M3 capability 打分的真实候选；在叙事中是"垂直领域 ACI"，不与通用机制混淆。

## V2 · 法律护栏（确定性，提交前强制）
G1 证据有效（quote 相似度）/ G2 X 须单篇全要素覆盖 / G3 Y 组合路径 / G4 公开日 /
G5 同族不重复 / G6 付费预算 / G7 意见与证据一致 / G8 无证据肯定结论强制降级措辞。
G2/G3 在 Evidence Graph 上做图查询实现；全部裁决事件化；submit 修订 ≤2 次。

## V3 · HITL Policy Gate
审批检索式（含 budget）是唯一人工强制闸；加额度为可选闸；所有批准可带"本会话同类型不再问"。

---

# 第四部分 · 上下文工程与模型路由（务实版）

## C1 · 三层压缩（不硬凑五层；SWE 证据：简单规则 > 复杂选择）
- L0 观察帽：标题 Top15 平铺禁翻页 / hits 帽 50 / 单观察 ≤1500 token / 空结果固定话术；
- L1 引用回收：未被任何 chart/决策引用的旧观察丢弃；旧 search 标题表压成统计行；
- L2 compact：超窗口阈值时 flash 一次性生成结构化进展摘要（已确认事实/预算/当前图状态），写 checkpoint。
Reader 全文只存在于隔离子窗口（天然隔离层）。**不做**每观察 LLM relevance 打分。

## C2 · 缓存边界
system = 静态角色+action schema+规则（前缀，字节稳定）‖ 动态 state 观察；
会话内锁模型；要 pro 一律开子 agent（M2）；工具表固定在静态区，新增工具走预留槽不改前缀。

## C3 · 模型配置
| 角色 | 模型 | 温度 | 说明 |
|---|---|---|---|
| Supervisor 规划/修订 | flash | 0.1 | 常规推进不调它 |
| Search/Evidence Lead、Workers | flash | 0.1 | 便宜量大 |
| Parser | flash | 0.1 | 一次性 |
| Reader / Critic | pro | 0.05/0.05 | 法律相关深推理 |
| LLM-judge(eval) | pro | 0 | 离线 |

---

# 第五部分 · 消融实验矩阵（§8，本项目的"数据话语权"）

fixtures 录制（去密钥）：演示案件先录全量 AMiner + LLM 响应；golden 冷启动 3 件。

| 实验 | 配置 | 假设 |
|---|---|---|
| E0 | v2 基线（无 plan/handoff/critic） | 对照原点 |
| E1 | Single agent（主 agent 全干，无团队） | 量化层级成本/收益 |
| E2 | Supervisor+Workers（无 critic） | 隔离上下文收益 |
| E3 | Full（+critic repair 闭环） | M4 证据完备率收益 |
| E4 | Full − replanning | M1 修订收益（召回/无效动作率） |
| E5 | Full − router（硬编码） | M3 失败路径收益 |
| E6 | Full − recovery 矩阵 | M5 注入测试对照 |
| E7 | Full + consensus-vote | 证明投票只在高风险仲裁有用（预期收益小/成本高，如实写） |
| E8 | reader=flash 全闪 | 模型路由成本/质量 trade-off |

每组报：recall@20 / grade_acc / evidence_precision / token(flash,pro分列) /
缓存命中 / paid / 时延 / planning_efficiency / recovery_rate。
**E1-E3 是主表（回答"多 agent 到底有没有用、贵多少"），E7 预期是"看起来聪明但不值"的诚实结论。**

---

# 第六部分 · 分期（每期都有可演示闭环，不允许空壳机制）

| 里程碑 | 范围（机制） | 演示验收（能现场跑） |
|---|---|---|
| **v6.0** | R0/R1 + M1 PlanDAG + M2 Handoff（search/evidence 两 team）+ M3 Router + C1 基础 | 现场看到 plan v1→v2 节点插入、handoff 事件、零 LLM 常规推进；断连 resume 不重复扣费 |
| **v6.1** | M4 EvidenceGraph+Critic Repair + V2 护栏图查询 + review team | 现场演示 critic 抓缺口→插入 targeted_read→复验通过；X 被规则改 Y |
| **v6.2** | M5 Recovery 矩阵 + 失败注入测试 + C2 缓存实测 + trace | 注入 5 种故障全部正确恢复；出 token/缓存对比 |
| **v6.3** | M7 Eval 管线 + 3 件 golden fixtures + E0-E3/E7/E8 消融 | 出对照柱状图；replay 零成本回归 |
| **v6.4** | M6 经验建议态（≥10 样本前仅骨架）+ skill 注册表 + Tool Adapter 预留 | UI 看到带来源建议 |

代码目录：`harness/{planner,supervisor,teams,handoff,router,recovery,context,events,checkpoint,replay,graph,guardrails,policy,skills,memory,trace}.py`；
`eval/{golden,fixtures,metrics,run_eval}.py`；v2 retrieval/providers/services 全部复用，orchestrator 留作 LLM_DEGRADED fallback。

---

# 第七部分 · 接口/事件对前端的影响（最小改动原则）

- SSE 帧不变（progress/result/error/done），progress.payload 仍是 {ts,tag,type,msg}，
  新增事件类型仅丰富文案（plan.revised → tag `agent.plan`；handoff.opened → `agent.handoff`；
  critic.issued → `subagent.critic`；recovery.selected → `guard.recovery`）；
- summary 增补：`{plan_revisions, replan_gain, recovery:{classified,recovered},
  handoff_count, tokens:{flash,pro,cached}, stop_reason}`；
- 案件详情页事件流天然展示，无需改交互；报告附录增加 critic 意见与经验建议来源。

---

# 第八部分 · 设计评审追问自检清单（每个机制都要能答）

1. **Plan DAG 和 LangGraph 的图有什么区别？** —— 我们的图是 Planner 自己在运行时修改的有界计划，
   节点带 origin/causation 可追溯；不是开发者预编译的静态流程图；且常规推进免 LLM 调用。
2. **为什么 worker 不能自己改查询？** —— 契约内可自救，越界上报；否则 DAG 和预算被旁路，层级失效。
3. **Replay 的确定性从哪来？** —— fixtures 录制外部响应，温度 0；是 fixture-based deterministic replay，
   不是说 LLM 天然确定。
4. **多 agent 比单 agent 好吗？** —— 看 E1-E3 数据：质量增益 vs token/成本/时延，trade-off 如实报；
   隔离上下文在长文专利场景的主要收益是防污染和上下文预算，不是"人多力量大"。
5. **投票为什么不是核心？** —— 同 prompt 同证据错误高度相关，3 倍成本；只在高风险分级分歧时仲裁（E7 数据支持）。
6. **Tool Router 是不是在做 learned routing？** —— 不是，规则打分+熔断；样本量不支持统计学习，接口预留。
7. **Critic 和法律护栏为什么分开？** —— 内容充分性靠 LLM critic，法律形式正确性靠确定性规则（X 自洽/日期/同族），
   两者不一致时规则胜诉、保守降级。
8. **上下文压缩为什么不用 LLM 给每条观察打分？** —— SWE-agent 消融：简单规则优于复杂选择且省一次调用；
   LLM 只在 compact 摘要时用一次。
9. **resume 怎么保证不重复付费？** —— read 幂等键=案件+族集合，已拉全文持久化复用。
10. **一次 run 里 LLM 到底做了哪些决策？** —— 交底书解析、plan 修订（仅 blocked 时）、reader 精读、
    critic 批判；其余（fanout/族合并/预算/护栏/路由/折叠）全是确定性的——这正是 harness 的价值主张。

---

## 评审确认项
1. 六要素规格的字段定义是否可直接开工（有无需要再细化的 schema）？
2. 消融实验 E0-E8 的设计是否认可，尤其 E7（预期投票收益小）如实报告的策略？
3. v6.0 先做"Plan DAG + Handoff + Router + 真 resume"，以演示案件跑通为验收，确认开工顺序？
