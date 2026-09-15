# AMiner 开放平台 · 专利检索接口文档

> 本文档基于 **PatentSage 项目实测**整理（2026-09-10 探针验证），覆盖项目实际使用的全部 3 个端点，可直接复用于其他项目。
>
> 数据源：[`backend/app/providers/aminer.py`](file:///d:/Desktop/BaiduSyncdisk/poirot/patentsage/backend/app/providers/aminer.py) + 真实 HTTP 响应采样。

---

## 0. 概览

| 端点 | 方法 | 费用 | 数据级 | 用途 |
|---|---|---|---|---|
| `/patent/search` | POST | 免费 | 粗排 | 关键词检索，返回 id/标题/年份/发明人 |
| `/patent/info` | GET | 免费 | 号单 | 补全公开号/申请号/国家/发明人列表 |
| `/patent/detail` | GET | **付费** | 全文 | 摘要/说明书/权利要求/IPC 分类号 |

- **Base URL**：`https://datacenter.aminer.cn/gateway/open_platform/api`
- **鉴权**：HTTP Header `Authorization: {token}`（直接放 token，**无 `Bearer` 前缀**）
- **Content-Type**：`application/json`（POST 时）
- **响应格式**：统一外层 `{code, msg, data, ...}`，`code=200` 为成功

## 1. 鉴权

### 获取 Token
1. 登录 [AMiner 开放平台](https://datacenter.aminer.cn)
2. 控制台 → API Key → 创建

### 请求头
```http
Authorization: sk-xxxxxxxxxxxxxxxxxxxx
Content-Type: application/json
```

> ⚠️ 与常见的 `Authorization: Bearer xxx` 不同，AMiner 直接把 token 放在 `Authorization` 头，无 `Bearer` 前缀。

## 2. 错误码与限速

### 业务错误码（响应体 `code` 字段）

| code | 含义 | 处理 |
|---|---|---|
| `200` | 成功 | 正常返回 |
| `40306` | 频率过快 | 退避重试（见下） |
| `40301` / `40302` / `40307` / `40308` | 鉴权失败 | 检查 token |
| 其他 | 业务错误 | 见 `msg` |

### 特殊情况
- **余额不足**：HTTP 状态可能是 `500`，但响应体 `msg` 含「余额」字样。`/patent/detail` 是付费端点，余额不足时所有 detail 调用都会失败。
- **HTTP 5xx**：按可重试处理。

### 限速策略
项目实测：连续高频调用会返回 `40306`。建议：
- **最小调用间隔**：0.5 秒（令牌间隔）
- **重试退避**：`2^attempt` 秒（attempt 从 0 开始），最多重试 3 次

```python
import time, threading

class RateLimiter:
    def __init__(self, min_interval=0.5):
        self._min = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._min:
                time.sleep(self._min - delta)
            self._last = time.monotonic()
```

## 3. 端点详情

### 3.1 专利检索 — `/patent/search`

按关键词检索专利，返回粗排级结果。

**请求**
```http
POST /patent/search
Content-Type: application/json
Authorization: {token}
```

```json
{
  "query": "机械臂 视觉 语言模型",
  "page": 0,
  "size": 20
}
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | 是 | — | 检索式，支持空格分隔的多词组合 |
| `page` | int | 否 | 0 | 页码，从 0 开始。项目实测上限约 5 |
| `size` | int | 否 | 20 | 每页条数，项目实测上限 20 |

**响应**（实测）
```json
{
  "code": 200,
  "msg": "操作成功",
  "status": "success",
  "data": [
    {
      "id": "6a0749dc01a22c7bca7f0619",
      "title": null,
      "title_zh": "视觉语言动作模型的训练方法和机械臂操作装置",
      "pub_year": "2026",
      "app_year": "2025",
      "inventor_name": "朱政"
    }
  ]
}
```

**响应字段说明**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 专利唯一 ID，用于后续 `info`/`detail` 调用 |
| `title` | string \| null | 英文标题（常为 null） |
| `title_zh` | string | 中文标题 |
| `pub_year` | string | 公开年份 |
| `app_year` | string | 申请年份 |
| `inventor_name` | string | 发明人姓名（多人时为拼接字符串） |

**调用示例（Python）**
```python
import httpx

client = httpx.Client(
    base_url="https://datacenter.aminer.cn/gateway/open_platform/api",
    headers={"Authorization": token},
    timeout=30,
)

resp = client.post("/patent/search", json={
    "query": "机械臂 视觉 语言模型",
    "page": 0,
    "size": 20,
})
body = resp.json()
if body["code"] == 200:
    items = body["data"]  # list[dict]
    for it in items:
        print(it["id"], it["title_zh"])
```

---

### 3.2 专利号单信息 — `/patent/info`

根据专利 ID 补全号单级信息（公开号/申请号/国家/发明人列表）。免费。

**请求**
```http
GET /patent/info?id=6a0749dc01a22c7bca7f0619
Authorization: {token}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 专利 ID（来自 `/patent/search`） |

**响应**（实测）
```json
{
  "code": 200,
  "success": true,
  "msg": "",
  "log_id": "...",
  "data": [
    {
      "id": "6a0749dc01a22c7bca7f0619",
      "app_num": "121267892",
      "app_year": "2025",
      "pub_num": "202511356681",
      "pub_year": "2026",
      "pub_kind": "A",
      "country": "cn",
      "inventor": [
        {"name": "朱政", "sequence": 1},
        {"name": "王啸峰", "sequence": 2}
      ],
      "title": {
        "zh": ["视觉语言动作模型的训练方法和机械臂操作装置"]
      }
    }
  ]
}
```

**响应字段说明**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 专利 ID |
| `app_num` | string | 申请号 |
| `app_year` | string | 申请年份 |
| `pub_num` | string | 公开号 |
| `pub_year` | string | 公开年份 |
| `pub_kind` | string | 公开类型（如 `A` 发明专利申请公开） |
| `country` | string | 国家/地区代码（`cn`/`us`/`ep` 等） |
| `inventor` | list\[{name, sequence}\] | 发明人列表，含顺序 |
| `title` | {zh: list\[string\]} | 标题，中文在 `zh` 数组（**注意：不是 `title_zh` 字符串**） |

> ⚠️ `info` 的 `title` 结构与 `search` 不同：`search` 是 `title`(en) + `title_zh`(string)，而 `info` 是 `{zh: [string]}`。

---

### 3.3 专利全文详情 — `/patent/detail`（付费）

获取说明书全文、摘要、权利要求、IPC 分类号等。**此端点消耗账户余额**。

**请求**
```http
GET /patent/detail?id=6a0749dc01a22c7bca7f0619
Authorization: {token}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 专利 ID |

**响应**（实测，字段已截断）
```json
{
  "code": 200,
  "success": true,
  "msg": "",
  "log_id": "...",
  "data": [
    {
      "id": "6a0749dc01a22c7bca7f0619",
      "country": "cn",
      "app_num": "121267892",
      "pub_num": "202511356681",
      "pub_kind": "A",
      "title": { "zh": ["视觉语言动作模型的训练方法和机械臂操作装置"] },
      "app_date": { "seconds": 1758499200 },
      "pub_date": { "seconds": 1767657600 },
      "inventor": [{ "name": "朱政", "sequence": 1 }],
      "assignee": null,
      "priority": null,
      "abstract": {
        "zh": ["本公开提供了一种视觉语言动作模型的训练方法..."]
      },
      "description": {
        "zh": [
          "技术领域",
          "本公开涉及深度学习技术...",
          "背景技术",
          "..."
        ]
      },
      "claims": {
        "zh": [
          "1.一种视觉语言动作模型的训练方法，其特征在于...",
          "2.根据权利要求1所述的方法..."
        ]
      },
      "ipcr": [
        { "l1": "B", "l2": "BB25", "l3": "BB25B25J", "l4": "BB25B25J9/16" },
        { "l1": "G", "l2": "GG06", "l3": "GG06G06V", "l4": "GG06G06V20/40" }
      ]
    }
  ]
}
```

**响应字段说明**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 专利 ID |
| `country` | string | 国家代码 |
| `app_num` / `pub_num` | string | 申请号 / 公开号 |
| `pub_kind` | string | 公开类型 |
| `title` | {zh: list\[string\]} | 标题 |
| `app_date` / `pub_date` | {seconds: int} | 申请/公开日期，**Unix 秒级时间戳** |
| `inventor` | list\[{name, sequence}\] | 发明人列表 |
| `assignee` | list\[{name, ...}\] \| null | 申请人/专利权人（可能为 null） |
| `priority` | list \| null | 优先权信息（可能为 null） |
| `abstract` | {zh: list\[string\]} | 摘要，通常 1 段 |
| `description` | {zh: list\[string\]} | 说明书全文，**每段一个字符串**，数组索引可用于段落级引用定位 |
| `claims` | {zh: list\[string\]} | 权利要求书，每条权利要求一个字符串 |
| `ipcr` | list\[{l1,l2,l3,l4}\] | IPC 分类号（**注意字段名是 `ipcr`，不是 `ipc`**），逐层嵌套 |

> ⚠️ **字段名陷阱**：IPC 分类号的字段是 `ipcr`（带 r），不是 `ipc`。很多项目（含本项目旧代码）会写错成 `ipc` 导致读不到分类号。

> 💡 **段落级引用**：`description.zh` 是段落数组，可直接用数组下标定位到具体段落，作为证据出处（SourceRef）的 locator。例如 `p3` 表示第 4 段（0-indexed 为 3）。

## 4. 统一响应外层结构

三个端点的外层结构略有差异，整合如下：

```json
{
  "code": 200,            // int，200 成功
  "msg": "操作成功",      // string，错误描述
  "success": true,        // bool，仅 info/detail 有
  "status": "success",    // string，仅 search 有
  "log_id": "...",        // string，仅 info/detail 有
  "data": [ ... ]         // list，实际数据
}
```

**解析要点**：
- 成功判据：`code == 200`（不要依赖 `success`/`status`，因为 search 没有 `success`）
- `data` 始终是 **list**：search 返回多条，info/detail 返回 1 条（取 `data[0]`）

## 5. 完整调用流程示例（Python）

```python
import httpx
import time
import threading

BASE = "https://datacenter.aminer.cn/gateway/open_platform/api"


class RateLimiter:
    def __init__(self, min_interval=0.5):
        self._min = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            d = now - self._last
            if d < self._min:
                time.sleep(self._min - d)
            self._last = time.monotonic()


class AminerClient:
    def __init__(self, token: str):
        self._token = token
        self._limiter = RateLimiter(min_interval=0.5)
        self._client = httpx.Client(timeout=30, headers={"Authorization": token})

    def _call(self, method, path, **kwargs):
        last_err = None
        for attempt in range(4):  # 最多重试 3 次
            self._limiter.wait()
            try:
                resp = self._client.request(method, f"{BASE}{path}", **kwargs)
            except httpx.HTTPError as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue

            data = resp.json() if resp.content else {}
            code = data.get("code")

            if resp.status_code == 200 and code in (None, 200):
                return data

            msg = data.get("msg") or resp.text
            if code in {40301, 40302, 40307, 40308}:
                raise Exception(f"鉴权失败({code}): {msg}")
            if "余额" in str(msg):
                raise Exception(f"余额不足: {msg}")
            if code == 40306 or resp.status_code >= 500:
                last_err = Exception(f"可重试错误({code or resp.status_code}): {msg}")
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"AMiner 错误({code or resp.status_code}): {msg}")

        raise Exception(f"重试耗尽: {last_err}")

    def search(self, query, page=0, size=20):
        data = self._call("POST", "/patent/search", json={"query": query, "page": page, "size": size})
        return data.get("data", [])

    def info(self, patent_id):
        data = self._call("GET", "/patent/info", params={"id": patent_id})
        items = data.get("data", [])
        return items[0] if items else None

    def detail(self, patent_id):
        data = self._call("GET", "/patent/detail", params={"id": patent_id})
        items = data.get("data", [])
        return items[0] if items else None


# 使用
client = AminerClient(token="sk-xxx")

# 1. 检索
items = client.search("机械臂 视觉 语言模型", size=10)
pid = items[0]["id"]

# 2. 补全号单
info = client.info(pid)
print(info["pub_num"], info["country"])

# 3. 拿全文（付费）
detail = client.detail(pid)
abstract = detail["abstract"]["zh"][0]
paragraphs = detail["description"]["zh"]  # 段落数组
claims = detail["claims"]["zh"]           # 权利要求数组
ipcs = [c["l4"] for c in detail["ipcr"]]  # IPC 分类号
```

## 6. 字段映射速查表

从原始响应字段到业务字段的映射（基于项目实测）：

| 业务概念 | search | info | detail |
|---|---|---|---|
| 专利 ID | `id` | `id` | `id` |
| 中文标题 | `title_zh` | `title.zh[0]` | `title.zh[0]` |
| 英文标题 | `title` | — | — |
| 公开号 | — | `pub_num` | `pub_num` |
| 申请号 | — | `app_num` | `app_num` |
| 公开年份 | `pub_year` | `pub_year` | — |
| 申请年份 | `app_year` | `app_year` | — |
| 国家 | — | `country` | `country` |
| 发明人（字符串） | `inventor_name` | — | — |
| 发明人（列表） | — | `inventor[].name` | `inventor[].name` |
| 摘要 | — | — | `abstract.zh[0]` |
| 说明书 | — | — | `description.zh[]` |
| 权利要求 | — | — | `claims.zh[]` |
| IPC 分类号 | — | — | `ipcr[].l4` |
| 申请日期 | — | — | `app_date.seconds` |
| 公开日期 | — | — | `pub_date.seconds` |
| 申请人 | — | — | `assignee[].name`（可 null） |

## 7. 注意事项

1. **`data` 始终是 list**：即使 info/detail 只返回 1 条，也在 `data[0]` 里。
2. **`title` 结构不一致**：search 用 `title`+`title_zh`，info/detail 用 `{zh: [...]}`。
3. **IPC 字段是 `ipcr`**：不是 `ipc`，写错会拿不到分类号。
4. **日期是 Unix 秒**：`app_date`/`pub_date` 是 `{seconds: int}`，需 `datetime.fromtimestamp(seconds)` 转换。
5. **detail 付费**：余额不足时 HTTP 可能返回 500 + msg 含「余额」，不要按 HTTP 500 盲目重试。
6. **限速**：免费接口也有限速，建议 0.5s 间隔 + 指数退避。
7. **`assignee`/`priority` 可能为 null**：部分专利没有这些字段，需做空值保护。
8. **description 段落数组**：可直接用数组下标做证据段落定位，无需自行切分。

---

*文档生成时间：2026-09-10，基于 PatentSage 项目探针实测。*
