# Patent-Agent · ChaxinHarness

> 面向小型专利代理所的**专利查新（新颖性检索）Agent**：上传发明交底书 → Agent 解析技术要素并生成检索式（人工 HITL 审批）→ 两阶段专利检索 → 多 Agent 隔离精读与 X/Y/A 分级 → 产出带**段落级证据引用**的查新报告。
>
> 底层为**零第三方框架的自研 Agent 运行时 ChaxinHarness**（Dynamic Plan DAG、Typed Handoff、Evidence Graph Critic-Repair、故障恢复、轨迹评测消融），核心代码仅使用 Python 标准库。

---

## 功能特性

- **交底书智能解析**：自动提取技术问题/方案/效果、关键术语、同义词组与 IPC 分类建议（LLM 不可用时降级为规则解析）。
- **检索式 HITL 审批**：术语 chips 可视化、可人工编辑；审批是唯一强制人工闸，含付费精读预算。
- **真实 API 两阶段检索**：
  - 摘要阶段：布尔检索式服务端 CNF 编译为多路词袋子查询并发 fan-out（免费接口），命中子查询数即可解释粗排分；
  - 全文阶段：按申请号同族合并、预算内付费精读，断点重跑幂等不重复扣费。
- **Agentic 迭代**：覆盖率自评 → 按缺失角度独立改写补检 → 自评充分即收敛。
- **多 Agent 隔离精读**：每篇对比文件一个独立上下文子代理并行精读，输出 claim chart 与 X/Y/A 分级；所有结论强制挂「公开号 + 章节 + 段落号」证据，quote 做原文定位校验防杜撰。
- **自研 Agent Runtime（核心亮点）**：
  - **M1 Dynamic Plan DAG**：运行时可改写的有界计划图；常规节点零 LLM 推进，仅 blocked 时调 Planner，修订过三闸门（环检测/悬挂依赖/重复插入）；
  - **M2 Typed Handoff**：Supervisor↔团队强类型契约（目标/预算/工具白名单/返回 schema），越界动作 fail-closed；
  - **M3 Capability-aware Tool Router**：规则打分 + 熔断 + fallback（不做伪学习），统一 Native/MCP Tool Adapter；
  - **M4 Evidence Graph + Critic-Repair**：claim chart 确定性投影为证据图，确定性法律护栏与 LLM 内容质控双轨，X→Y 保守仲裁，repair 节点插入与复验；
  - **M5 Recovery Engine**：9 类故障 → 差异化恢复策略，附故障注入自动化回归（6/6）；
  - **M6 Experience Memory**：从人工勘误离线抽取经验，样本 <10 不启用，Planner 建议态注入（可忽略，绝不自动生效）；
  - **M7 Trajectory Evaluation**：fixture 录制/确定性回放 + E0-E3/E7/E8 消融矩阵 + 零依赖 SVG 柱状图。
- **可观测**：append-only 强类型事件日志（因果可回放）+ OpenTelemetry 风格 span 树 + SSE 实时事件流。
- **逐级降级**：LLM 全链路不可用时自动从 v6 Runtime 降级到单 Agent 循环，再降级到确定性检索管道，服务不中断。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.10+ / FastAPI / Pydantic / SQLite / SSE |
| Agent/检索内核 | **纯 Python 标准库**（无 LangChain/LangGraph/向量库） |
| 前端 | 原生 HTML/CSS/JS 四页应用（FastAPI 静态托管，无需 Node） |
| LLM | 任意 OpenAI 兼容 Chat Completions 服务（默认 DeepSeek `deepseek-flash`） |
| 专利数据源 | AMiner 开放平台（search/info 免费，detail 全文付费） |

## 目录结构

```
PatentAgent/
├── patent-agent-prototype/     # 前端工作台（4 页面，原生静态）
├── probe/                      # AMiner API 探针脚本与真实响应样本（开发期工具）
├── aminer-openapi.md           # AMiner 接口实测文档
└── backend/
    ├── app/
    │   ├── main.py             # FastAPI 入口（API + SSE + 静态托管）
    │   ├── config.py           # 凭证与默认参数（环境变量优先）
    │   ├── db.py               # SQLite：案件/事件日志/检查点/幂等/经验库
    │   ├── providers/          # AMiner 客户端 + 数据清洗/同族合并
    │   ├── retrieval/          # CNF 编译器、两阶段检索管道
    │   ├── services/           # 案件/检索编排/对比骨架/报告生成
    │   ├── agents/             # 交底书解析、Reader 子代理、单 Agent 循环（降级路径）
    │   ├── llm/                # OpenAI 兼容客户端（结构化输出/退避/用量统计）
    │   └── harness/            # ★ ChaxinHarness：plan/handoff/router/graph/
    │                           #   teams/review/recovery/skills/memory/trace
    ├── eval/                   # M7 评测：fixtures/golden/合成宇宙/指标/消融矩阵
    ├── tests/                  # 单元测试
    ├── scripts/                # 冒烟、故障注入、fixture 录制、经验抽取
    └── requirements.txt
```

## 快速开始

### 1. 环境准备

需要 Python 3.10+（开发环境为 3.14）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置凭证（二选一或混用）

**方式 A：环境变量（推荐）**

| 变量 | 用途 |
|---|---|
| `AMINER_TOKEN` | AMiner 开放平台 JWT（检索必需） |
| `DEEPSEEK_API_KEY` | LLM Key（交底解析/精读/质控必需） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 可选，默认 `https://api.deepseek.com` / `deepseek-flash` |

**方式 B：本地配置文件（已被 .gitignore，不会提交）**

```powershell
Copy-Item probe\config.local.example.json probe\config.local.json   # 填 aminer_token
Copy-Item backend\config.local.example.json backend\config.local.json  # 填 deepseek_api_key
```

> ⚠️ 不配置凭证也能启动服务、浏览界面与演示案件；执行真实检索/解析时会返回明确的缺凭证错误（错误码 5004）。可用 `GET /api/health` 自检凭证状态。
> 💰 AMiner 的 detail（专利全文）为**付费接口**，每次检索的调用次数受 HITL 审批页「精检预算」控制。

### 3. 启动

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

打开浏览器：

- 工作台：<http://127.0.0.1:8000/>
- API 文档：<http://127.0.0.1:8000/docs>
- 凭证自检：<http://127.0.0.1:8000/api/health>
- 演示案件：<http://127.0.0.1:8000/case.html?id=CN2026-0881>

首次启动自动创建 SQLite 并写入演示案件。重置演示数据：删除 `backend/data/chaxin.db` 后重启。

### 4. 使用流程

1. 工作台「新建案件」，粘贴交底书文本 → LLM 解析三要素与检索式；
2. 案件详情页核对/编辑检索式与精检预算，点击审批；
3. SSE 实时观察 Plan DAG 执行、子查询 fan-out、子 Agent 精读与 Critic 质控事件；
4. 对比分析页查看特征比对表、X/Y/A 分级与段落级证据；
5. 查新报告页预览并导出 HTML 报告（AI 初筛，代理师复核制）。

## 评测与消融（零外部成本）

评测层内置 2 个**合成专利宇宙**（明确标注 synthetic，仅用于验证管线机制，不代表真实统计结论）：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\record_fixtures.py   # 录制合成 fixture（不花一分钱）
.\.venv\Scripts\python.exe -m eval.run_eval             # 确定性回放全矩阵 + 出 SVG
```

产物在 `eval/data/runs/<时间戳>/`：每案 scorecard JSON、`summary.json`、`ablation.svg`。

| 变体 | 配置 |
|---|---|
| E0 | v2 确定性管道（无 LLM/计划/handoff/critic） |
| E1 | 单 Agent think-act 循环 |
| E2 | Supervisor + Workers（无 critic） |
| E3 | Full：+ Evidence Graph Critic-Repair |
| E7 | Full + critic 三票共识 |
| E8 | Full + reader 独立模型路由 |

缺录硬闸（R1）：回放命中任何未录制的外部请求即报错并阻断，禁止用半截 fixture 出对比结论。

## 测试与验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests   # 单元测试
.\.venv\Scripts\python.exe scripts\fault_injection.py       # 6 条故障恢复路径
```

真实 API 冒烟脚本（需要凭证，注意付费成本）：`scripts/smoke_pipeline.py`、`scripts/smoke_agent.py`、`scripts/smoke_v6.py`。

## API 摘要

完整交互式文档见 `/docs`。主要端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务与凭证配置自检 |
| GET/POST | `/api/cases` | 案件列表 / 新建（交底书解析） |
| GET | `/api/cases/{id}` | 案件详情 |
| POST | `/api/cases/{id}/query/confirm` | HITL 检索式审批 |
| GET | `/api/cases/{id}/retrieval/stream` | SSE 检索执行事件流 |
| GET | `/api/cases/{id}/comparison` | 精读对比结果 |
| GET | `/api/cases/{id}/events` | 事件日志回放 |
| GET | `/api/cases/{id}/trace` | span 追踪树 |
| POST | `/api/cases/{id}/corrections` | 人工分级勘误（经验库信号） |
| POST | `/api/cases/{id}/report/export` | 导出 HTML 报告 |

## 已知限制 / 路线图

- 评测目前跑在合成宇宙（管线验证），真实案件 golden set 的 live 录制未启用——这是刻意的 R1 状态，不把合成结果包装成真实结论；
- 仅验证过单模型（flash）；reader/pro 模型路由的代码路径已具备，但缺少双模型的成本/质量实测；
- mid-run 游标级 checkpoint resume 的表结构已就绪、接线未完成（当前重跑依靠幂等不重复扣费）；
- 报告导出为 HTML（A4 打印友好），DOCX 导出未实现；
- 检索覆盖中文专利（AMiner 数据源），无外部法律状态数据库。

## License

[MIT](LICENSE)
