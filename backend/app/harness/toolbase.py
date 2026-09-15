# -*- coding: utf-8 -*-
"""
Tool Adapter 协议层（v6.4，对应规格 M3 §② 的 ToolSpec 三对象化）。

为什么需要：
v6.0 的 router.SPECS 是 list-of-dict，执行器是裸 callable：kwargs 直传、返回形状
靠约定。接入第二数据源（MCP server）前，先把工具交互统一成三对象，使
Router/团队只认协议，不感知工具来自进程内还是远程：

  ToolSchema  工具声明（能力 / 成本 / 只读 / 并发安全 / 超时 / 来源 adapter）
  ToolCall    一次调用（call_id / tool / args / intent / causation）
  ToolResult  统一结果（ok / data | error+fclass / retriable / latency_ms）

Adapter：
  NativeAdapter     进程内 Python callable（现有 aminer client / local 函数）
  MCPClientAdapter  MCP stdio / streamable-http 传输骨架（JSON-RPC 2.0 + content
                    block 转换）；不内置假 server/假数据，无真实第二数据源前
                    不注册 remote.* schema、不接线。

零第三方运行时依赖：stdio 用 subprocess，HTTP 用 urllib。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# Failure 类别字面量与 recovery 模块常量保持一致（该模块反向依赖本文件的 ToolError，
# 故此处不做反向导入）
FCLASS_TRANSIENT = "TOOL_TRANSIENT"
FCLASS_AUTH = "TOOL_AUTH"
FCLASS_BUDGET = "TOOL_BUDGET"
FCLASS_PLAN_INVALID = "PLAN_INVALID"

COST_KINDS = ("free", "paid", "tokens")


class ToolError(Exception):
    """工具层错误：带 Recovery 可直接消费的 fclass。"""

    def __init__(self, fclass: str, detail: str, retriable: bool = False):
        super().__init__(detail)
        self.fclass = fclass
        self.detail = detail
        self.retriable = retriable


# ---------------- 三对象协议 ----------------

@dataclass(frozen=True)
class ToolSchema:
    name: str
    capabilities: frozenset[str]
    cost_kind: str                      # free | paid | tokens
    cost_rank: int                     # 越小越优先
    read_only: bool = True
    concurrency_safe: bool = True
    timeout_ms: int = 30000
    source: str = "native"             # native | mcp
    params: dict = field(default_factory=dict)     # 可选 JSON Schema 摘要

    def to_legacy_dict(self) -> dict:
        """router 旧 SPECS 元素形状（candidates 消费者向后兼容）。"""
        return {"name": self.name, "capabilities": set(self.capabilities),
                "cost_kind": self.cost_kind, "cost_rank": self.cost_rank,
                "read_only": self.read_only, "concurrency_safe": self.concurrency_safe,
                "timeout_ms": self.timeout_ms, "source": self.source}


@dataclass
class ToolCall:
    tool: str
    args: dict = field(default_factory=dict)
    intent: str | None = None
    call_id: str = field(default_factory=lambda: "tc_" + uuid.uuid4().hex[:10])
    causation_id: int | None = None


@dataclass
class ToolResult:
    ok: bool
    tool: str
    call_id: str | None = None
    data: Any = None
    error: str | None = None
    fclass: str | None = None
    retriable: bool = False
    latency_ms: int | None = None
    cost_kind: str | None = None

    @classmethod
    def success(cls, call: ToolCall, data: Any, *, latency_ms: int,
                cost_kind: str | None = None) -> "ToolResult":
        return cls(True, tool=call.tool, call_id=call.call_id, data=data,
                   latency_ms=latency_ms, cost_kind=cost_kind)

    @classmethod
    def failure(cls, call: ToolCall, error: str, *, fclass: str,
                retriable: bool = True, latency_ms: int | None = None) -> "ToolResult":
        return cls(False, tool=call.tool, call_id=call.call_id, error=error,
                   fclass=fclass, retriable=retriable, latency_ms=latency_ms)

    def to_legacy(self) -> dict:
        """router.execute 旧返回形状：dict 展开 + _tool 标记；非 dict 包一层 data。"""
        d = self.data if isinstance(self.data, dict) else {"data": self.data}
        out = dict(d)
        out["_tool"] = self.tool
        return out


# ---------------- Schema 注册表 ----------------

REGISTRY: dict[str, ToolSchema] = {}


def register_schema(schema: ToolSchema) -> None:
    REGISTRY[schema.name] = schema


def get_schema(name: str) -> ToolSchema | None:
    return REGISTRY.get(name)


def all_schemas() -> list[ToolSchema]:
    """按注册顺序返回（cost_rank 相同时的稳定次序由 Router 再排）。"""
    return list(REGISTRY.values())


def registry_names() -> set[str]:
    return set(REGISTRY.keys())


def schemas_for(intent: str) -> list[ToolSchema]:
    return [s for s in REGISTRY.values() if intent in s.capabilities]


# v6.0 既有四件工具 + v6.4 Critic 零付费补读（与 handoff 白名单/skill 声明对齐）
register_schema(ToolSchema(
    "local.parse", frozenset({"parse"}), "tokens", 1))
register_schema(ToolSchema(
    "aminer.search", frozenset({"coarse_search"}), "free", 0))
register_schema(ToolSchema(
    "aminer.info", frozenset({"bibliographic"}), "free", 0))
register_schema(ToolSchema(
    "aminer.detail", frozenset({"fulltext"}), "paid", 2, concurrency_safe=False))
register_schema(ToolSchema(
    "local.reread", frozenset({"memory_reread"}), "tokens", 1))


# ---------------- Native 适配器 ----------------

def _native_fclass(exc: Exception) -> tuple[str, bool]:
    """进程内执行器异常 → Recovery fclass（延迟导入避免与 recovery/router 循环）。"""
    if isinstance(exc, ToolError):
        return exc.fclass or FCLASS_TRANSIENT, exc.retriable
    try:
        from ..providers.aminer_client import AminerAuthError, AminerBalanceError
        if isinstance(exc, AminerAuthError):
            return FCLASS_AUTH, False
        if isinstance(exc, AminerBalanceError):
            return FCLASS_BUDGET, False
    except ImportError:
        pass
    return FCLASS_TRANSIENT, True


class NativeAdapter:
    """进程内 callable 适配器：executors = {tool_name: fn(**kwargs)}。"""

    source = "native"

    def __init__(self, executors: dict[str, Callable[..., Any]] | None = None):
        self.executors: dict[str, Callable[..., Any]] = dict(executors or {})

    def bind(self, name: str, fn: Callable[..., Any]) -> None:
        self.executors[name] = fn

    def invoke(self, call: ToolCall, *, timeout_ms: int | None = None) -> ToolResult:
        # timeout_ms：native 调用不做强制中断（线程内杀调用不安全），仅透传声明；
        # 真实超时由数据源 client 自身的超时机制保证。
        fn = self.executors.get(call.tool)
        if fn is None:
            return ToolResult.failure(
                call, f"native 执行器未绑定: {call.tool}",
                fclass=FCLASS_PLAN_INVALID, retriable=False)
        t0 = time.perf_counter()
        try:
            data = fn(**call.args)
        except Exception as e:  # noqa: BLE001 - 适配器边界必须归一化所有异常
            fclass, retriable = _native_fclass(e)
            return ToolResult.failure(call, str(e)[:200], fclass=fclass,
                                      retriable=retriable,
                                      latency_ms=int((time.perf_counter() - t0) * 1000))
        return ToolResult.success(call, data,
                                  latency_ms=int((time.perf_counter() - t0) * 1000))


# ---------------- MCP 客户端适配器（传输骨架，不接假 server）----------------

class MCPClientAdapter:
    """
    MCP（Model Context Protocol）JSON-RPC 2.0 客户端骨架。

    - stdio：懒启动子进程，按行 JSONL 收发（reader 线程 + queue 实现超时）
    - http ：POST 到 streamable-http endpoint（urllib，零第三方依赖）

    仅交付传输与 schema 转换形状：不内置假工具/假数据。有真实第二数据源后，
    把其工具以 ToolSchema(source="mcp") 注册进 REGISTRY，并把本 adapter 实例
    交给 ToolRouter(adapters={"mcp": ...}) 即可被 capability 路由。
    """

    source = "mcp"

    def __init__(self, server_name: str, *, transport: str = "stdio",
                 command: str | None = None, args: list[str] | None = None,
                 url: str | None = None, headers: dict | None = None,
                 env: dict | None = None, timeout_ms: int = 30000):
        if transport not in ("stdio", "http"):
            raise ValueError(f"不支持的 MCP transport: {transport}")
        if transport == "stdio" and not command:
            raise ValueError("stdio transport 需要 command")
        if transport == "http" and not url:
            raise ValueError("http transport 需要 url")
        self.server_name = server_name
        self.transport = transport
        self.command = command
        self.args = list(args or [])
        self.url = url
        self.headers = dict(headers or {})
        self.env = env
        self.timeout_ms = timeout_ms
        self._proc = None
        self._rid = 0

    # ---- JSON-RPC 发送 ----

    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    def invoke(self, call: ToolCall, *, timeout_ms: int | None = None) -> ToolResult:
        rid = self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": call.tool, "arguments": call.args}}
        t0 = time.perf_counter()
        try:
            raw = (self._send_stdio if self.transport == "stdio" else self._send_http)(
                payload, rid, (timeout_ms or self.timeout_ms) / 1000.0)
        except _McpAuth as e:
            return ToolResult.failure(call, f"MCP 鉴权失败: {e}", fclass=FCLASS_AUTH,
                                      retriable=False,
                                      latency_ms=int((time.perf_counter() - t0) * 1000))
        except Exception as e:  # noqa: BLE001 - 传输异常归一化
            return ToolResult.failure(call, f"MCP 传输失败: {str(e)[:160]}",
                                      fclass=FCLASS_TRANSIENT, retriable=True,
                                      latency_ms=int((time.perf_counter() - t0) * 1000))
        return self._normalize(call, raw,
                               latency_ms=int((time.perf_counter() - t0) * 1000))

    def _send_stdio(self, payload: dict, rid: int, timeout_s: float) -> dict:
        import subprocess
        import sys
        from queue import Queue, Empty
        import threading

        if self._proc is None:
            kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                          bufsize=1)
            if self.env:
                import os
                env = dict(os.environ)
                env.update(self.env)
                kwargs["env"] = env
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen([self.command, *self.args], **kwargs)
            self._lines: Queue = Queue()

            def _reader():
                assert self._proc is not None and self._proc.stdout is not None
                for line in self._proc.stdout:
                    self._lines.put(line)

            threading.Thread(target=_reader, daemon=True).start()

        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP stdio 响应超时（{timeout_s}s）")
            try:
                line = self._lines.get(timeout=min(remaining, 0.5))
            except Empty:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 服务端通知/噪声行：等到 id 匹配的响应
            if msg.get("id") == rid:
                return msg

    def _send_http(self, payload: dict, rid: int, timeout_s: float) -> dict:
        import urllib.error
        import urllib.request

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.headers}
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise _McpAuth(str(e)) from e
            raise

    # ---- MCP content blocks → ToolResult ----

    def _normalize(self, call: ToolCall, raw: dict, *, latency_ms: int) -> ToolResult:
        if "error" in raw:
            err = raw["error"] or {}
            code = err.get("code")
            # 方法/参数级错误属于计划非法（工具不存在/参数不匹配），重试无益
            fclass = FCLASS_PLAN_INVALID if code in (-32601, -32602) else FCLASS_TRANSIENT
            return ToolResult.failure(
                call, f"MCP error {code}: {err.get('message', '')[:160]}",
                fclass=fclass, retriable=fclass == FCLASS_TRANSIENT,
                latency_ms=latency_ms)
        result = raw.get("result") or {}
        blocks = result.get("content") or []
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        if result.get("isError"):
            return ToolResult.failure(call, joined or "MCP 工具返回 isError",
                                      fclass=FCLASS_TRANSIENT, latency_ms=latency_ms)
        parsed: Any = None
        if len(texts) == 1:
            try:
                parsed = json.loads(texts[0])
            except (json.JSONDecodeError, TypeError):
                parsed = None
        return ToolResult.success(
            call, {"text": joined, "json": parsed, "content": blocks},
            latency_ms=latency_ms, cost_kind=None)

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            finally:
                self._proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _McpAuth(Exception):
    """MCP HTTP 401/403 内部信号。"""
