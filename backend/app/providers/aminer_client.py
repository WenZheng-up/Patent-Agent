# -*- coding: utf-8 -*-
"""
AMiner 专利 API 客户端（零第三方依赖，标准库实现）。

实测约束（见项目 probe/ 探针结论，2026-09-10）：
- 鉴权头直接放 token，无 Bearer 前缀；成功判据 code == 200；data 恒为 list
- search 仅支持「空格分隔的 AND 词袋」，括号/OR 均不可用（括号->0 条，OR->噪声）
- size 实测可到 200；无 total 字段；深翻页存在约 1% 跨页重复，需按 id 去重
- 限速 0.5s，40306 / 5xx 指数退避；余额不足时 HTTP 可能 500 且 msg 含「余额」
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config


class AminerError(Exception):
    """业务错误（不可重试）。"""


class AminerAuthError(AminerError):
    pass


class AminerBalanceError(AminerError):
    pass


class _RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


class AminerClient:
    def __init__(self, token: str | None = None,
                 base_url: str = config.AMINER_BASE_URL,
                 min_interval: float = config.MIN_CALL_INTERVAL,
                 max_retry: int = config.MAX_RETRY):
        self.token = token or config.load_aminer_token()
        self.base_url = base_url.rstrip("/")
        self.limiter = _RateLimiter(min_interval)
        self.max_retry = max_retry

    # ---------- 底层 HTTP ----------
    def _http(self, method: str, path: str, body=None, params=None) -> tuple[int, dict]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self.token}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _call(self, method: str, path: str, body=None, params=None):
        last_err = None
        for attempt in range(self.max_retry + 1):
            self.limiter.wait()
            try:
                status, payload = self._http(method, path, body, params)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {"msg": text[:300]}
                status = e.code
            except urllib.error.URLError as e:
                last_err = f"网络错误: {e.reason}"
                time.sleep(2 ** attempt)
                continue

            code = payload.get("code")
            msg = str(payload.get("msg", ""))
            if status == 200 and code in (None, 200):
                return payload
            if "余额" in msg:
                raise AminerBalanceError(f"AMiner 余额不足: {msg}")
            if code in (40301, 40302, 40307, 40308):
                raise AminerAuthError(f"鉴权失败({code}): {msg}")
            if code == 40306 or status >= 500:
                last_err = f"可重试错误({code or status}): {msg}"
                time.sleep(2 ** attempt)
                continue
            raise AminerError(f"AMiner 错误({code or status}): {msg}")
        raise AminerError(f"重试耗尽: {last_err}")

    # ---------- 三个端点 ----------
    def search(self, query: str, page: int = 0, size: int = 20) -> list[dict]:
        """关键词粗排检索（免费）。query 只能是空格分隔的 AND 词袋。"""
        payload = self._call("POST", "/patent/search",
                             body={"query": query, "page": page, "size": size})
        return payload.get("data") or []

    def info(self, patent_id: str) -> dict | None:
        """号单信息（免费）：公开号/申请号/国家/发明人/kind。"""
        payload = self._call("GET", "/patent/info", params={"id": patent_id})
        items = payload.get("data") or []
        return items[0] if items else None

    def detail(self, patent_id: str) -> dict | None:
        """全文详情（付费）：摘要/说明书/权利要求/ipcr。"""
        payload = self._call("GET", "/patent/detail", params={"id": patent_id})
        items = payload.get("data") or []
        return items[0] if items else None
