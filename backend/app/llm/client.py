# -*- coding: utf-8 -*-
"""
OpenAI 兼容 Chat Completions 客户端（零第三方依赖，标准库）。
实测目标：DeepSeek https://api.deepseek.com ，模型 deepseek-flash（支持 JSON 模式）。

设计要点（对应技术设计文档「结构化输出 + 模型路由 + 成本」）：
- chat_json：response_format=json_object 强约束 + 代码围栏剥离 + 一次「修复」重试；
- 进程内累计 token 用量（prompt/completion/cache 命中），供成本与模型路由观测；
- 401/403 -> LLMAuthError；429/5xx -> 退避重试；其他 -> LLMError；
- 业务层对 LLM 失败必须有确定性降级（见 case_service / orchestrator）。
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.local.json"


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    pass


def _load_local_config() -> dict:
    if _CONFIG_PATH.is_file():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def is_configured() -> bool:
    """是否已配置 LLM 凭证（供健康检查；不校验有效性）。"""
    import os
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return True
    return bool(_load_local_config().get("deepseek_api_key", "").strip())


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: int = 120, max_retry: int = 2):
        cfg = _load_local_config()
        self.api_key = api_key or __import__("os").environ.get("DEEPSEEK_API_KEY", "").strip() \
            or cfg.get("deepseek_api_key", "").strip()
        if not self.api_key:
            raise LLMError(
                "未配置 LLM 凭证：请设置环境变量 DEEPSEEK_API_KEY，"
                "或复制 backend/config.local.example.json 为 backend/config.local.json 并填入 Key")
        self.base_url = (base_url or __import__("os").environ.get("DEEPSEEK_BASE_URL")
                         or cfg.get("deepseek_base_url")
                         or "https://api.deepseek.com").rstrip("/")
        self.model = model or __import__("os").environ.get("DEEPSEEK_MODEL") \
            or cfg.get("deepseek_model") or "deepseek-flash"
        self.timeout = timeout
        self.max_retry = max_retry
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "cached_tokens": 0, "calls": 0}

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions", method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _call_raw(self, messages: list[dict], *, temperature: float,
                  max_tokens: int, json_mode: bool) -> dict:
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last = None
        for attempt in range(self.max_retry + 1):
            try:
                body = self._post(payload)
                self.usage["calls"] += 1
                u = body.get("usage") or {}
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                self.usage["completion_tokens"] += u.get("completion_tokens", 0)
                self.usage["cached_tokens"] += (
                    (u.get("prompt_tokens_details") or {}).get("cached_tokens")
                    or u.get("prompt_cache_hit_tokens") or 0)
                return body
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code in (401, 403):
                    raise LLMAuthError(f"LLM 鉴权失败({e.code})：{text[:200]}")
                last = f"HTTP {e.code}: {text[:200]}"
                if e.code in (429,) or e.code >= 500:
                    time.sleep(1.5 ** attempt)
                    continue
                raise LLMError(last)
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"网络错误: {e}"
                time.sleep(1.5 ** attempt)
        raise LLMError(f"LLM 调用重试耗尽：{last}")

    @staticmethod
    def _extract_json(text: str):
        text = text.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        return json.loads(text)

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1,
                  max_tokens: int = 2000, required_keys: list[str] | None = None,
                  repair_hint: str | None = None) -> dict:
        """
        强约束 JSON 输出。解析失败或缺键时带错误反馈再试一次；仍失败抛 LLMError。
        """
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        last_text = ""
        for attempt in range(2):
            body = self._call_raw(messages, temperature=temperature,
                                  max_tokens=max_tokens, json_mode=True)
            last_text = body["choices"][0]["message"]["content"]
            try:
                data = self._extract_json(last_text)
            except Exception:
                messages.append({"role": "assistant", "content": last_text[:800]})
                messages.append({"role": "user",
                                 "content": "你上一次的输出不是合法 JSON。请只输出一个合法 JSON 对象，"
                                            "不要有代码围栏或多余文字。"})
                continue
            missing = [k for k in (required_keys or []) if k not in data]
            if missing:
                messages.append({"role": "assistant", "content": last_text[:800]})
                messages.append({"role": "user",
                                 "content": f"JSON 缺少必填字段：{missing}。"
                                            f"请补齐后重新输出完整 JSON。{repair_hint or ''}"})
                continue
            return data
        raise LLMError(f"LLM 结构化输出修复失败：{last_text[:200]}")

    def chat_text(self, system: str, user: str, *, temperature: float = 0.3,
                  max_tokens: int = 1000) -> str:
        body = self._call_raw([{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                              temperature=temperature, max_tokens=max_tokens,
                              json_mode=False)
        return body["choices"][0]["message"]["content"].strip()
