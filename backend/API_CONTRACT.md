# Patent-Agent · 前后端接口契约 v1（冻结稿）

> 本文档是 FastAPI 实现与前端 `patent-agent-prototype/api.js` 改造的**唯一事实来源**。
> 所有 Schema 均以探针实测数据（2026-09-10，见 `../probe/samples/`）与已跑通的
> 后端核心引擎（`app/retrieval/pipeline.py`、`app/providers/normalize.py`）为准。
> 前端 7 个桩函数签名保持不变（UI 层零改动原则），仅 `runRetrieval` 内部
> 由「一次性 fetch」改为「SSE 订阅 + 终态落库」。

***

## 0. 通用约定

| 项             | 值                                                                   |
| ------------- | ------------------------------------------------------------------- |
| Base URL（开发期） | `http://127.0.0.1:8000`                                             |
| 静态原型          | 由 FastAPI 同源托管 `patent-agent-prototype/`（免跨域）；如分离部署则开 CORS `*`（仅开发） |
| 请求/响应编码       | UTF-8 JSON；`Content-Type: application/json`                         |
| 时间格式          | 日期 `YYYY-MM-DD`；事件时间戳 `HH:MM:SS`（服务端本地时区）                           |

### 0.1 响应信封

```json
{ "code": 0, "msg": "ok", "data": { } }
```

* `code = 0` 成功；非 0 为业务错误码（见 §7）。

* HTTP 状态码：成功 200；SSE 见 §5；业务错误统一 **HTTP 200 + 非 0 code**
  （前端桩只判断 `code`，不依赖 HTTP status；鉴权/余额类严重错误同此规则）。

### 0.2 状态机（案件 `status` 枚举）

```
待解析 → 待审批 → 检索中 → 对比分析 → 已完成
                ↑ HITL 中断点（POST /query/confirm 后进入「检索中」）
```

***

## 1. 端点总表

| # | 方法   | 路径                                 | 前端桩                   | 切片实现                    |
| - | ---- | ---------------------------------- | --------------------- | ----------------------- |
| 1 | GET  | `/api/cases`                       | `fetchCases()`        | ✅ 真实                    |
| 2 | POST | `/api/cases`                       | `createCase()`        | ⚠️ 解析为规则 stub（v2 接 LLM） |
| 3 | GET  | `/api/cases/{id}`                  | `fetchCase(id)`       | ✅ 真实                    |
| 4 | POST | `/api/cases/{id}/query/confirm`    | `confirmQuery()`      | ✅ 真实（CNF 编译）            |
| 5 | GET  | `/api/cases/{id}/retrieval`        | `runRetrieval(id)`    | ✅ 终态读取                  |
| 6 | GET  | `/api/cases/{id}/retrieval/stream` | （`runRetrieval` 内部）   | ✅ **SSE**，真实管道          |
| 7 | GET  | `/api/cases/{id}/comparison`       | `fetchComparison(id)` | ⚠️ 规则骨架（grade 待 LLM）    |
| 8 | POST | `/api/cases/{id}/report/export`    | `generateReport(id)`  | ⚠️ HTML 报告（docx 为 v2）   |

***

## 2. GET `/api/cases` — 案件列表

**响应** **`data`：**

```json
[
  {
    "id": "CN2026-0881",
    "title": "一种基于复合相变材料的动力电池热管理系统",
    "client": "星驰新能源科技",
    "field": "H01M 10/6566",
    "status": "检索中",
    "progress": 62,
    "updated": "2026-09-10 09:41",
    "agent": "粗检 18 子查询执行中 · 12/18"
  }
]
```

字段与前端工作台列一一对应；`agent` 为最新一条运行时事件的一句话摘要
（切片可由事件流末尾事件生成；无事件时给静态文案）。

***

## 3. POST `/api/cases` — 新建案件（解析交底书）

**请求：**

```json
{ "title": "一种基于复合相变材料的动力电池热管理系统",
  "client": "星驰新能源科技",
  "disclosureText": "（交底书全文纯文本）" }
```

**响应** **`data`：**

```json
{ "id": "CN2026-0882", "title": "...", "client": "...", "status": "待审批" }
```

行为：

* v1 切片：服务端持久化案件后，**规则 stub** 生成 `disclosure`（术语 = 中文分词高频词兜底）
  与 `query`（expr 为标题/术语拼成的单层 CNF）。结构与 §4 完全一致，`query.lint`
  标注「⚠️ 规则生成，待 v2 LLM 精化」。

* v2：LLM 提取三要素 + 同义词 + IPC 建议（接口形态不变，前端无感升级）。

***

## 4. GET `/api/cases/{id}` — 案件全量

**响应** **`data`（Case）：**

```json
{
  "id": "CN2026-0881",
  "title": "一种基于复合相变材料的动力电池热管理系统",
  "client": "星驰新能源科技",
  "clientContact": "周工 · 研发部",
  "field": "H01M 10/6566",
  "filed": "2026-08-30",
  "updated": "2026-09-10 09:41",
  "status": "待审批",
  "progress": 30,

  "disclosure": {
    "problem": "……",
    "solution": "……",
    "effect": "……",
    "terms": ["相变材料", "石墨烯复合", "微通道液冷板"],
    "wordCount": 4860
  },

  "query": {
    "topic": "动力电池 / 储能电池热管理",
    "keywords": ["相变材料", "液冷板", "热管理"],
    "synonyms": ["PCM", "相变储热", "液冷", "电池包"],
    "ipc": ["H01M 10/6566", "H01M 10/613", "H05K 7/20"],
    "dateFrom": "2018-01-01",
    "dateTo": "2026-09-10",
    "expr": "(相变材料 OR PCM OR 相变储热) AND (液冷板 OR 液冷) AND (动力电池 OR 电池包 OR 储能电池) AND 热管理",
    "lint": "CNF 编译通过 · 4 组 18 个子查询 · 词袋 fan-out（粗检免费）",
    "groups": [["相变材料", "PCM", "相变储热"], ["液冷板", "液冷"],
               ["动力电池", "电池包", "储能电池"], ["热管理"]]
  },

  "hits": [ /* Hit[]，结构同 §6.1；检索未执行时为 [] */ ],
  "priorArt": [ /* PriorArt[]，结构同 §8；对比未执行时为 [] */ ],
  "events": [ /* Event[]，HITL 前事件，§6.2 */ ],
  "eventsRun": [ /* Event[]，确认后事件；检索未执行时为 [] */ ],
  "reportMeta": null
}
```

`query.groups` 为结构化 CNF（供编译器直接消费；`expr` 仅为展示/审批文本）。
`lint` **禁止编造召回量级**——上游 search 无 total 字段，召回量只有执行后才知道。

***

## 5. POST `/api/cases/{id}/query/confirm` — HITL 审批

**请求：**

```json
{ "query": "(相变材料 OR PCM) AND (液冷板 OR 液冷) AND 动力电池 AND 热管理",
  "edited": true,
  "groups": [["相变材料", "PCM"], ["液冷板", "液冷"], ["动力电池"], ["热管理"]],
  "budget": 20 }
```

| 字段       | 必填 | 说明                                                               |
| -------- | -- | ---------------------------------------------------------------- |
| `query`  | 是  | 审批后的布尔表达式（字符串）。也允许传对象 `{expr, groups}`，形态同 §4                    |
| `edited` | 是  | 代理师是否修改过（审计用）                                                    |
| `groups` | 否  | 结构化 CNF；**缺省时服务端从** **`query`** **用** **`parse_boolean`** **解析** |
| `budget` | 否  | 本次精检付费 detail 上限，默认 20（成本闸门，代理师可见）                               |

**响应** **`data`：**

```json
{
  "resumedAt": "2026-09-10T09:43:18",
  "checkpointId": "cp_cn20260881_0003",
  "compiled": {
    "subqueryCount": 18,
    "groupCount": 4,
    "truncated": false,
    "droppedTerms": []
  }
}
```

编译失败（括号不配对 / 清洗后无有效词）返回 `code=4009`，案件保持「待审批」。

***

## 6. 检索执行：SSE 流 + 终态读取

`runRetrieval(id)` 前端内部改为：

```js
var es = new EventSource('/api/cases/' + id + '/retrieval/stream?budget=20&minScore=2');
es.addEventListener('progress', function (e) { appendEvent(JSON.parse(e.data)); });
es.addEventListener('result',  function (e) { renderHits(JSON.parse(e.data).hits); });
es.addEventListener('error',   function (e) { showError(JSON.parse(e.data)); es.close(); });
es.addEventListener('done',    function ()  { es.close(); });
```

### 6.1 GET `/api/cases/{id}/retrieval/stream` — SSE

**Query 参数：** `budget=20`（付费 detail 族数上限）、`minScore=2`（进入精检的最小命中子查询数）、`size=100`（每子查询粗检条数）。

**响应头：**

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

**帧格式（严格）：**

```
event: progress
data: {"ts":"09:43:19","tag":"tool.search","type":"tool","msg":"……"}

event: result
data: {"hits":[...], "summary":{...}, "events":[...]}

event: done
data: {"reason":"completed"}

```

* 每帧 `event: <类型>\n` + `data: <单行 JSON>\n\n`；事件类型仅四种：
  `progress` / `result` / `error` / `done`。

* 正常顺序：`progress*` → 恰好一个 `result` → 恰好一个 `done(reason=completed)`。

* 失败顺序：`progress*` → 恰好一个 `error`（data 为 §7 错误信封）→ `done(reason=error)`；
  **`error`** **帧后不再产出** **`result`**。

* 流内鉴权/余额致命错误也通过 `error` 帧下发（不中断 TCP），前端据 `code` 提示。

**`result.data`：**

```json
{
  "summary": {
    "subqueryCount": 18,
    "coarseTotal": 75,
    "coarseMultiHit": 54,
    "families": 53,
    "finePatents": 20,
    "paidDetailCalls": 20,
    "dateFiltered": 6,
    "truncated": false
  },
  "hits": [
    {
      "pubNo": "CN109449528A",
      "title": "一种相变储能液冷板、电池包主动热管理系统及控制方法",
      "assignee": "江苏大学",
      "date": "2019-03-07",
      "score": 0.5,
      "stage": "全文",
      "coarseScore": 8,
      "subqueryTotal": 16,
      "kinds": ["A", "B"],
      "ipcs": ["H01M 10/613", "B60L 58/26"],
      "ipcMatch": true,
      "dataCompleteness": { "hasAbstract": true, "descriptionParagraphs": 51,
                            "hasClaims": false, "ipcCount": 7 },
      "aminerId": "6335e013667297566c1a99df"
    }
  ],
  "events": [ /* 本次运行的全部 Event，供断线重放 */ ]
}
```

**Hit 字段映射规则（真实数据 → 前端形态）：**

| 前端字段               | 来源/规则                                                                |
| ------------------ | -------------------------------------------------------------------- |
| `pubNo`            | `country.toUpper() + pub_num + 主kind`（族合并后主版本 kind）；如 `CN109449528A` |
| `assignee`         | `assignees[0]`；多个时加「等」；空数组给 `""`（实测新申请常为 null）                       |
| `date`             | `pub_date`（已由 `{seconds}` 转为 `YYYY-MM-DD`）                           |
| `score`            | **`coarseScore / 有效子查询数`**（有效 = 返回非空的子查询数），保留 2 位                    |
| `coarseScore`      | 命中的子查询数（免费可解释粗排分，UI 可显示「命中 8/16 组合」）                                 |
| `stage`            | 精检取到全文 = `"全文"`；仅粗检候选 = `"摘要"`                                       |
| `kinds`            | 族合并出的全部公开类型（免费 info 即可得，如 `["A","B"]`）                               |
| `ipcs`             | 双格式清洗后的标准 IPC（`H01M 10/613` / `B60L 58/26`）                          |
| `ipcMatch`         | 与 `query.ipc` 是否有交集（**软标记不过滤**——IPC 仅付费后可得且可能缺失）                     |
| `dataCompleteness` | 摘要/段落数/权项/IPC 有无；权项缺失时前端展示「权项缺失，结论基于说明书」                             |
| `checked`          | 后端不下发；前端默认 `checked = (stage === '全文')`                              |

排序：`stage=全文` 优先，组内按 `score` 降序；列表上限 50（摘要档截断）。
**日期过滤**在粗检后用 search 免费返回的 `pub_year` 完成（`dateFrom/dateTo` 年份闭区间），
过滤数计入 `summary.dateFiltered`。

### 6.2 Event 结构（`progress` 帧与 `eventsRun` 同构）

```json
{ "ts": "09:43:26", "tag": "tool.search", "type": "tool",
  "msg": "粗检子查询 6/18：相变材料 液冷 储能电池 热管理 → 21 条 · 候选池 66" }
```

`type ∈ {"", "tool", "hitl", "verify", "sub"}` 与前端 CSS 类一一对应，禁止新增。

**管道事件 → UI 事件映射表：**

| 管道事件（pipeline.progress\_cb）   | tag           | type     | msg 模板                                                         |
| ----------------------------- | ------------- | -------- | -------------------------------------------------------------- |
| `subquery_done`               | `tool.search` | `tool`   | `粗检子查询 {i+1}/{n}：{query} → {returned} 条 · 候选池 {pool_size}`     |
| `coarse_done`                 | `loop.verify` | `verify` | `粗检完成：并集 {total} 条，{multi_hit} 条命中 ≥2 个子查询`                    |
| `fine_start`                  | `tool.detail` | `tool`   | `预算闸门：最多精检 {budget} 个专利族 · 免费号单建族中`                            |
| `families_built`              | `tool.detail` | `tool`   | `号单建族完成：{families} 个族（A/B 版本已合并）`                              |
| `family_done`                 | `tool.detail` | `tool`   | `精检 {index}/{total} 族完成（族内 {members} 版本）· 累计付费 {paid_calls} 次` |
| `detail_error` / `info_error` | `guard.retry` | `verify` | `版本 {aminerId} 无全文/调用失败，跳过`                                    |
| `fine_done`                   | `loop.verify` | `verify` | `精检完成：{patents} 篇全文就绪 · 付费 detail 共 {paid_calls} 次`            |

v2 LLM 对比阶段追加：`subagent.spawn`（`sub`）、`subagent.done`（`sub`）、
`guard.cite`（`verify`），tag 已在 mock 事件流中预留。

### 6.3 GET `/api/cases/{id}/retrieval` — 终态读取

* 流跑完后结果持久化服务端；本接口返回与 `result` 帧**完全相同**的 data。

* 尚未执行检索返回 `code=4090`（前端据此提示「请先审批检索式」）。

***

## 7. 错误码表

| code | 含义                   | 前端处理                  |
| ---- | -------------------- | --------------------- |
| 0    | 成功                   | —                     |
| 4004 | 案件不存在                | toast + 返回工作台         |
| 4009 | 检索式编译失败（无有效词/括号错误）   | 审批区红字提示，留在「待审批」       |
| 4090 | 状态冲突（如未审批就读取检索结果）    | 引导回上一步                |
| 5001 | 上游鉴权失败（AMiner token） | toast「数据源鉴权异常」，禁用检索按钮 |
| 5002 | 上游余额不足（detail 付费端点）  | toast + 引导充值；粗检结果仍可用  |
| 5003 | 上游限速/重试耗尽            | toast「数据源繁忙，稍后重试」     |

错误信封：`{ "code": 5002, "msg": "AMiner 余额不足：……", "data": null }`。

***

## 8. GET `/api/cases/{id}/comparison` — 对比分析

**响应** **`data: PriorArt[]`**（与 mock `priorArt` 同构，`cite` 升级为证据对象）：

```json
[
  {
    "pubNo": "CN109449528A",
    "title": "一种相变储能液冷板、电池包主动热管理系统及控制方法",
    "assignee": "江苏大学",
    "date": "2019-03-07",
    "grade": null,
    "score": 0.5,
    "abstract": "……",
    "features": [
      {
        "mine": "微通道液冷板与 PCM 被动层耦合",
        "theirs": "公开相变储能液冷板与主动热管理耦合，未明确微通道结构",
        "disclosed": "部分",
        "evidence": { "section": "§背景技术", "paragraphIndex": 7,
                      "quote": "……液冷板与相变层耦合……" }
      },
      {
        "mine": "热失控预警与冷却策略联动",
        "theirs": "未涉及",
        "disclosed": "否",
        "evidence": null
      }
    ],
    "conclusion": null
  }
]
```

| 字段           | 说明                                                                                                    |
| ------------ | ----------------------------------------------------------------------------------------------------- |
| `grade`      | `"X"` / `"Y"` / `"A"` / `null`。**v1 切片为 null**（分级需 LLM 精读）；v2 由 sub-agent 给出                          |
| `disclosed`  | `"是"` / `"部分"` / `"否"`（三态与前端 tag 一致）                                                                  |
| `evidence`   | **EvidenceRef 段落级引用**：`{section, paragraphIndex, quote}`，渲染为 `§背景技术 ¶7`；未定位到为 `null`（对应 mock 的 `"—"`） |
| `conclusion` | v1 切片为 `null`；v2 LLM 生成                                                                               |

v1 切片规则：`features` 由交底书三要素特征项 × 精检专利全文做关键词命中
（`normalize.locate_evidence` 定位段落），`disclosed` 按特征词命中率三档化；
`grade`/`conclusion` 留空并在 UI 标注「AI 精读 v2 待接入」。

***

## 9. POST `/api/cases/{id}/report/export` — 导出报告

**响应** **`data`：**

```json
{ "reportNo": "QX-2026-0881-01", "url": "/files/QX-2026-0881-01.html", "format": "html" }
```

* v1：生成可打印 HTML 报告（A4 纸感，引用渲染为 `§章节 ¶序号`），`url` 同源可下载。

* v2：`format: "docx"`（python-docx），形态不变。

* 报告正文数据 = Case 全量 + retrieval.summary + priorArt；报告页 `reportMeta` 由
  `summary` 派生：`hitsTotal = coarseTotal`、`readDeep = finePatents`。

***

## 10. 前端改动清单（契约冻结后执行）

1. `api.js`：7 个桩函数体替换为 fetch（签名不变）；`runRetrieval` 改为
   `EventSource` 订阅 `/retrieval/stream`，`progress` 帧追加事件流、
   `result` 帧渲染 hits、`done/error` 关闭；首屏仍可调 GET `/retrieval` 读终态。
2. `case.html`：`lint` 文案直接展示（不再有「1.2k–1.8k」预估）；hits 区
   `hits-meta` 用 `summary.coarseTotal / finePatents`；score-bar 用归一化 `score`，
   旁注「命中 8/16 组合」；`dataCompleteness.hasClaims=false` 时行内标灰提示。
3. `compare.html`：`cite` 字段从 `[0052]` 字符串改为 `evidence` 对象渲染
   （`§背景技术 ¶7`，点击/悬浮显示 `quote`）；`grade=null` 显示「待精读」tag。
4. `report.html`：引用格式同步；`hitsTotal/readDeep` 取真实 summary。
5. 新建案件 modal 不变；`budget` 在审批区增加一个轻量输入（默认 20，标注「付费精检篇数上限」）。

***

## 11. v1 切片边界（本次 FastAPI 实现范围）

| 能力                  | v1 切片                                         | v2                            |
| ------------------- | --------------------------------------------- | ----------------------------- |
| 案件存储                | SQLite（stdlib `sqlite3`，单文件 `data/chaxin.db`） | 同                             |
| 交底书解析               | 规则 stub（结构完整、文案标注）                            | LLM 三要素/术语/IPC                |
| CNF 编译 + fan-out 粗检 | ✅ 真实（已验证 75/54）                               | 同                             |
| 族感知预算闸门精检           | ✅ 真实（info 建族 + 付费 detail）                     | 向量/Cross-Encoder 重排           |
| 事件流                 | ✅ SSE 真实管道事件                                  | LLM verify/rewrite Agentic 迭代 |
| 对比分析                | 关键词命中骨架 + EvidenceRef                         | sub-agent 精读 + X/Y/A 分级       |
| 报告导出                | 可打印 HTML                                      | docx                          |

