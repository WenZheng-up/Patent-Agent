# Patent-Agent v7 架构图册（实现锚定版）

> 本文是 v6 技术规格（AGENT_SPEC_v6.md，字段级细节在那里）的**图式总览**，
> 编码时先看本文定位数据流，再查 v6 规格的字段定义。
> **模型约束：全部 LLM 角色统一 deepseek-flash（用户仅提供 flash）。**
> 无 pro 路由：Reader/Critic 用 flash 但靠"独立上下文窗口 + rubric 强约束 prompt + 确定性护栏"
> 保证质量；模型路由表因此简化，缓存前缀更稳定（会话内零切模型，缓存收益更大）。

---

## 图 0 · 一张总图

```
┌──────────────────────────────── 前端（SSE 事件流，不改交互） ─────────────────────────┐
│  case.html：plan / handoff / critic / recovery 事件实时追加；报告页消费 charts         │
└──────────────────────────────────────────────▲───────────────────────────────────────┘
                                               │ SSE: progress / result / error / done
┌──────────────────────────── main.py（FastAPI，薄编排层） ────────────────────────────┐
│  POST /query/confirm（HITL 闸+budget）  GET /retrieval/stream（起 run）  /events      │
│  worker 线程：Supervisor.run() ──事件入队 SSE + 写 EventStore ── result 帧落终态       │
└──────────────────────────────────────────────▲───────────────────────────────────────┘
                                               │
┌────────────────────────────── ChaxinHarness（app/harness/） ─────────────────────────┐
│                                                                                        │
│   ┌──────────────────────────────────────────────────────────────────────────────┐    │
│   │                     Supervisor（planner.py + supervisor.py）                   │    │
│   │  持有 PlanDAG；ready 节点直接执行（零 LLM）；blocked 才调 flash 修订            │    │
│   │  循环：take ready → handoff 给 team → 收 HandoffResult → fold → checkpoint      │    │
│   └───────┬───────────────────────────────────┬───────────────────────────┬────────┘    │
│           │ handoff(goal/budget/tools/schema) │                           │             │
│           ▼                                   ▼                           ▼             │
│  ┌─────────────────┐            ┌─────────────────────┐       ┌──────────────────┐     │
│  │ Search Team     │            │ Evidence Team       │       │ Review Team(v6.1)│     │
│  │ teams/search    │            │ teams/evidence      │       │ Critic+Graph     │     │
│  │ Lead→Workers×N │            │ Lead→Readers×N      │       │                  │     │
│  │ 各 worker 独立   │            │ 每篇专利独立窗口      │       └──────────────────┘     │
│  │ flash 上下文     │            │ flash 9k/4.5k 窗口   │                                  │
│  └────────┬────────┘            └──────────┬──────────┘                                  │
│           │ intent                          │ intent                                      │
│           ▼                                ▼                                             │
│  ┌──────────────────────── Tool Router（router.py） ───────────────────────────┐       │
│  │ intent → capability/cost/availability 规则打分 → 选执行器 → 失败 fallback     │       │
│  │ local.parse │ aminer.search(free) │ aminer.info(free) │ aminer.detail(paid)   │       │
│  └────────┬───────────────────────────────┬───────────────────────┬───────────────┘       │
│           ▼                               ▼                       ▼                        │
│  ┌──────────────┐              ┌──────────────────────┐   ┌─────────────────┐            │
│  │ LLMClient     │              │ providers/aminer      │   │ retrieval/      │            │
│  │ (flash)       │              │ search/info/detail    │   │ CNF编译器/pipeline│           │
│  └──────────────┘              └──────────────────────┘   └─────────────────┘            │
│                                                                                        │
│  横切：recovery.py（错误分类→策略）│ guardrails.py（G1-G8）│ policy.py（HITL/budget）   │
│        context.py（3 层压缩+缓存边界）│ memory.py/skills.py（v6.4 建议态）                 │
│        trace.py（token/付费/时延）                                                     │
│  基座：events.py（append-only + fold）│ checkpoint.py（resume/幂等）│ replay.py（fixtures）│
└────────────────────────────────────────────────────────────────────────────────────────┘
                                               │
                          ┌────────────────────┴────────────────────┐
                          ▼                                         ▼
                  SQLite（cases / retrieval /            data/files（报告）
                  case_events append-only / read_idem）
```

---

## 图 1 · Plan DAG 生命周期（M1 核心）

```
                         初始 DAG（静态模板生成，不花 LLM）
   parse(done)                                                          ┌── critique(v6.1)
      │                                                                 │      │ pass
      ▼                                                                 │      ▼
 anchor_search ◀──── ready：handoff Search Team（fanout 18 路）          └── submit
      │ step.done → fold pool → checkpoint                              ▲
      ▼                                                                 │
  调度器：有 ready? ── 有 ──▶ 直接执行（不调 flash）                       │
      │ 没有（blocked/failed/刚收尾一批）                                 │
      ▼                                                                 │
 flash 规划（输入：压缩 State + open_gaps + 失败信号）                    │
      │ 输出 revisions: insert/retry/skip/update_args + reason           │
      ▼                                                                 │
 apply_revisions（环检测/同词拦截/≤3 次闸门）                             │
      │                                                                 │
      ├─ insert gap_search「热失控预警」────────▶ 新节点跑 fanout         │
      │        0 新增 → Recovery(ZERO_RESULT) → 判区别特征，skip 该枝     │
      ├─ insert targeted_read（critic 驱动，v6.1）───────────────────────┘
      └─ advance deep_read ──▶ handoff Evidence Team
```

**节点状态机**：`pending → ready → running → done`；异常支路 `running→failed→(retry|skipped)`。
节点带 `origin`（initial / inserted_by_critic / inserted_by_recovery / retry）与 `origin_ref`（因果事件 seq）。

---

## 图 2 · Handoff 时序（M2，一次 read 的完整交接）

```
Supervisor                EventStore           Evidence Team            Reader(worker)     Tool Router
    │  ready=deep_read                              │                        │                 │
    │──handoff.opened──────────▶ 写事件             │                        │                 │
    │  {budget:3, tools:[read],                    │                        │                 │
    │   return_schema:ChartsV1}                     │                        │                 │
    │──────────────────────────────────────────────▶│ 收到任务，按 pool 取 Top3 族             │
    │                                               │ intent=read ────────────────────────────▶│
    │                                               │                        │  detail×3(paid) │
    │                                               │                        │  幂等键查重       │
    │                                               │ 派 worker（每篇独立 flash 窗口）─────────▶│
    │                                               │                        │ chart JSON      │
    │                                               │◀───── 9k失败→4.5k重试→骨架降级            │
    │                                               │ charts 聚合/schema 校验                  │
    │◀──────────HandoffResult{charts_ref, paid_used, tokens, requests:[]}                     │
    │──handoff.returned──────────▶ 写事件             │                        │                 │
    │  fold charts 入 State/Graph                    │                        │                 │
    │──checkpoint.written─────────▶ 写事件             │                        │                 │
```

**越界处理**：Reader 想多读一篇/换专利 → 不自行执行，放入 `HandoffResult.requests[]`
→ Supervisor 下轮决定（转成 ask_human 或 DAG 新节点）。

---

## 图 3 · Tool Router 决策流（M3）

```
 intent（parse | coarse_search | bibliographic | fulltext）
    │
    ▼
 注册执行器按 capability 过滤
    │
    ▼
 熔断过滤（CircuitState.open? 近窗失败率）
    │
    ▼
 规则打分：capability(0/.5/1) → cost（free 0 < tokens < paid）
    │ （paid 额外受 budget_gate：剩余额度=0 直接不可选）
    ▼
 selected ──执行成功──▶ observation + RouteDecision 写 trace
    │
    └─执行失败──▶ Recovery Engine 分类
                    ├ TOOL_TRANSIENT → 退避重试×3 → 下一候选执行器
                    ├ TOOL_BUDGET    → 免费执行器降级（info/摘要档）
                    ├ TOOL_AUTH      → 终止告警
                    └ TOOL_TRANSIENT 全熔断 → LLM_DEGRADED → v2 管道
```

执行器清单（v6.0 只有 4 个真实候选；remote/MCP 是预留槽，无真实源不接）：
`local.parse / aminer.search / aminer.info / aminer.detail`。

---

## 图 4 · 事件 → State fold → Checkpoint（R0/R1 数据流）

```
组件产出动作/观察
      │ append(etype, actor, payload, causation_id=当前seq)
      ▼
case_events 表（append-only，16 枚举类型，带因果 seq）
      │                                    ┌── SSE 实时帧（前端）
      ├─────────────▶ 广播队列 ────────────┤
      │                                    └── trace（度量）
      ▼
step.done 时：fold(事件流) ──▶ FoldedState{plan,pool,charts,graph,counters}
      │
      ▼
checkpoint 表{run_id, seq, plan_snapshot, counters}
      │
   resume：fold 到 cursor ──▶ read 先查 read_idem（族集合→已拉全文）──▶ 从下一 ready 节点继续
   replay：ReplayClient 拦截出站 → fixtures 命中表 → 零外网/零付费重放整条轨迹
```

---

## 图 5 · Critique → Repair 闭环（v6.1 预览，M4）

```
charts ──确定性投影──▶ Evidence Graph（Claim→Feature→Evidence→Patent/Grade，内存图）
                              │
                              ▼
                    Critic（flash，独立窗口 ≤3k：图摘要+仅相关段落+rubric）
                              │ Critique{gaps[], conflicts[], passed}
              ┌───────────────┴────────────────┐
        passed=true                      有 gap/conflict
              │                                │ critique_handoff（反向）
              ▼                                ▼
        Guardrails G1-G8             Supervisor 插入 repair 节点
        （确定性法律 lint）                    │ targeted_read / targeted_search
              │ pass                          ▼
              ▼                         Reader 补证 → 图更新
           submit                                │
                                          Critic 复验原 gap（≤2 轮）
                                          仍不过 → G8 保守措辞 + 人工标注
注：法律形式问题（X 须单篇全要素/日期/同族）由 Guardrails 规则判，Critic 只管内容缺口。
```

---

## 图 6 · 上下文构造（C1/C2，全 flash）

```
每次 LLM 调用的消息结构：
┌────────────────────────── 静态前缀（字节稳定，prompt cache 命中区） ─────────────────────────┐
│ 角色定义 + Action/Revision JSON schema + 规则（禁 AND 多角/同词不重发/预算约束）+ rubric      │
│ （工具说明固定；会话内永不切模型；全角色 flash）                                              │
└─────────────────────────────── ▲ DYNAMIC BOUNDARY ▲ ───────────────────────────────────────┘
┌────────────────────────── 动态区（每轮重建，不进缓存） ────────────────────────────────────┐
│ L0 观察帽：Top15 标题平铺 / hits≤50 / 单观察≤1500tok / 空结果固定话术                        │
│ L1 引用回收：未被 chart/决策引用的旧观察丢弃；旧标题表→统计行                                │
│ PlanDAG 精简视图（节点 id/intent/status/depends + open_gaps）                                │
│ L2 compact：超阈值时 flash 一次性结构化进展摘要（已确认事实/预算/图状态）                    │
└──────────────────────────────────────────────────────────────────────────────────────────┘
Reader 全文（9k/4.5k 字）只在子 agent 独立窗口，永不进主上下文。
```

---

## 图 7 · Failure Recovery 决策树（M5）

```
任何组件异常
   │
   ▼
Failure Classifier（确定性规则分类，不靠 LLM）
   ├ TOOL_TRANSIENT/TIMEOUT ─▶ 退避×3 ─成功▶继续  ─失败▶换执行器 ─再失败▶LLM_DEGRADED
   ├ TOOL_AUTH 401/403      ─▶ 不重试，run 失败明确告警
   ├ TOOL_BUDGET 5002       ─▶ 免费降级(info/摘要档)；需要全文 ─▶ ask_human 加额
   ├ ZERO_RESULT            ─▶ 空结果话术回 Planner；同词重发拦截；换角度≤2 ─▶仍0则 skip
   ├ EVIDENCE_GAP(v6.1)     ─▶ critic repair 节点 ≤2 ─▶ 保守标注
   ├ GRADE_CONFLICT(v6.1)   ─▶ 规则仲裁，X→Y 保守降级
   ├ PLAN_INVALID           ─▶ 三要素错误(code+被拒内容+替代)回喂 flash 修订 ×2 ─▶ 模板 fallback
   └ LLM_DEGRADED           ─▶ parser→规则 stub；reader→骨架；planner→v2 固定管道
原则：可恢复错误 withholding（不提前终止 SSE）；每类独立熔断计数器持久化进 State。
```

---

## 图 8 · 现有代码映射（复用 vs 新建）

```
复用（v2 已验证）                          新建（app/harness/）
─────────────────────────────             ─────────────────────────────────
llm/client.py（chat_json/usage）  ──被调──  planner.py     Plan DAG + flash 修订
providers/aminer_client.py         ──被调──  supervisor.py  主循环/调度
retrieval/query_compiler.py（CNF）  ──被调──  teams/search.py  Lead+worker 派发/并发
retrieval/pipeline.py（coarse/fine）──被调── teams/evidence.py read 调度/族幂等
providers/normalize.py（清洗/段落） ──被调──  handoff.py     契约校验/事件化
agents/reader_agent.py（flash 精读）──改造── router.py      4 执行器打分/fallback
agents/disclosure_agent.py         ──复用── recovery.py     分类+策略矩阵
services/report_service.py         ──复用── events.py       16 事件/append/fold
services/comparison_service.py（骨架降级）── guardrails.py   G1-G8（v6.1 图查询）
db.py（扩 case_events/checkpoint 表）      checkpoint.py / replay.py
main.py SSE worker 改调 Supervisor          context.py / trace.py
```

降级链（LLM 不可用时层层回退）：
`Supervisor(flash规划) → 模板推进器（确定性 fanout+read） → v2 orchestrator（固定管道）`。

---

## 图 9 · v6.0 交付边界（本次实现范围，绿=做，灰=后续）

```
✅ R0 EventStore 扩展（causation + 16 枚举 + fold）
✅ R1 Checkpoint/resume（read 幂等）+ replay fixtures
✅ M1 Plan DAG（初始模板/ready 调度/flash 修订/三闸门）
✅ M2 Handoff + Search/Evidence 两团队（worker 契约内自救）
✅ M3 Tool Router（4 执行器规则打分 + fallback）
✅ C1 三层压缩基础 + SSE 新事件文案 + 降级回 v2
⬜ M4 Critic/Graph、M6 Memory、MCP adapter、skill 注册表（v6.1/6.4）
⬜ M7 Trajectory Eval 消融（v6.3，但事件结构现在就按可计算格式落库）
```

**v6.0 演示验收（必须真实可跑）**：
1. 审批后前端看到 `plan.created(5 nodes)` → search 节点 done → `plan.revised(+gap_search)`
   → deep_read handoff（含 budget/tools）→ charts → `run.finished(stop_reason=submitted)`；
2. SSE 中途断开重连：cursor 补帧不丢事件；
3. resume（重新请求）：已读族不重复扣 detail（paid_used 不增加）；
4. 注入一次 ZERO_RESULT/一次 LLM 坏 JSON：恢复路径事件可见，run 不死；
5. LLM key 失效：自动降级 v2 管道出结果。
