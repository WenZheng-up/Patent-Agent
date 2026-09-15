# -*- coding: utf-8 -*-
"""
M3 Capability-aware Tool Router（确定性规则打分，不做伪学习）。
intent → capability 过滤 → 熔断过滤 → cost 排序 → 执行；失败交 Recovery 分类后 fallback。

v6.4：工具交互统一走 toolbase 三对象协议（ToolSchema/ToolCall/ToolResult），
执行器经 Adapter 接入（native 进程内 / mcp 远程传输骨架）。
本文件仍向后暴露：SPECS（list-of-dict，由注册表派生）、ToolError、ToolRouter 旧接口。
"""
import time
from collections import defaultdict

from . import toolbase
from .toolbase import (
    ToolError, ToolSchema, ToolCall, ToolResult,
    NativeAdapter, MCPClientAdapter,
    FCLASS_AUTH, FCLASS_BUDGET, FCLASS_TRANSIENT,
)

# 向后兼容：旧 SPECS list-of-dict 形状（顺序 = 注册表顺序，cost_rank 相同时稳定）
SPECS = [s.to_legacy_dict() for s in toolbase.all_schemas()]

_FAIL_WINDOW = 60.0   # 秒
_FAIL_THRESHOLD = 5   # 窗口内失败次数熔断


class ToolRouter:
    def __init__(self, executors: dict, budget_left: int | None = None, client=None,
                 adapters: dict | None = None, registry: dict | None = None):
        """
        executors: {tool_name: callable(**kwargs)} —— native 执行器（向后兼容入口）。
        budget_left: 剩余付费次数（None 不限）。
        client: 绑定的数据源客户端（团队层直接复用其限速/会话）。
        adapters: {source: adapter}，默认 {"native": NativeAdapter(executors)}；
                  接入真实 MCP server 时传 {"mcp": MCPClientAdapter(...)}。
        registry: {name: ToolSchema}，默认 toolbase.REGISTRY（测试可替身注入）。
        """
        self.executors = executors
        self.budget_left = budget_left
        self.client = client
        self.registry = registry if registry is not None else toolbase.REGISTRY
        self.adapters: dict = {"native": NativeAdapter(executors)}
        if adapters:
            self.adapters.update(adapters)
        self.fails: dict[str, list[float]] = defaultdict(list)
        self.decisions: list[dict] = []

    # ---------- 注册/直调（协议入口）----------

    def bind_native(self, name: str, fn) -> None:
        self.executors[name] = fn
        self.adapters["native"].bind(name, fn)

    def invoke(self, tool_name: str, **kwargs) -> ToolResult:
        """按工具名直调（不经 intent 选择）：仍受熔断/预算/契约边界约束。"""
        schema = self.registry.get(tool_name)
        if schema is None:
            return ToolResult.failure(ToolCall(tool_name, kwargs),
                                      f"工具未注册: {tool_name}",
                                      fclass="PLAN_INVALID", retriable=False)
        ok, reason = self._available(schema)
        call = ToolCall(tool_name, kwargs)
        if not ok:
            fclass = FCLASS_BUDGET if reason == "no_budget" else FCLASS_TRANSIENT
            return ToolResult.failure(call, f"{tool_name} 不可用: {reason}",
                                      fclass=fclass, retriable=reason != "no_budget")
        result = self._through_adapter(schema, call)
        if result.ok and schema.cost_kind == "paid" and self.budget_left is not None:
            self.budget_left -= 1
        return result

    # ---------- intent 路由（v6.0 既有行为）----------

    def _circuit_open(self, name: str) -> bool:
        now = time.monotonic()
        self.fails[name] = [t for t in self.fails[name] if now - t < _FAIL_WINDOW]
        return len(self.fails[name]) >= _FAIL_THRESHOLD

    def _available(self, schema: ToolSchema) -> tuple[bool, str]:
        if schema.source == "native":
            if schema.name not in self.executors:
                return False, "no_executor"
        elif schema.source not in self.adapters:
            return False, "no_adapter"
        if self._circuit_open(schema.name):
            return False, "circuit_open"
        if schema.cost_kind == "paid" and self.budget_left is not None and self.budget_left <= 0:
            return False, "no_budget"
        return True, ""

    def candidates(self, intent: str) -> list[dict]:
        cands = []
        for schema in self.registry.values():
            if intent not in schema.capabilities:
                continue
            ok, reason = self._available(schema)
            cands.append({"spec": schema.to_legacy_dict(), "schema": schema,
                          "available": ok, "unavail_reason": reason,
                          "cost_rank": schema.cost_rank if ok else 99})
        return sorted(cands, key=lambda c: c["cost_rank"])

    def _through_adapter(self, schema: ToolSchema, call: ToolCall) -> ToolResult:
        adapter = self.adapters.get(schema.source)
        if adapter is None:
            return ToolResult.failure(call, f"无 {schema.source} adapter",
                                      fclass=FCLASS_TRANSIENT, retriable=True)
        return adapter.invoke(call, timeout_ms=schema.timeout_ms)

    def execute(self, intent: str, **kwargs):
        """
        选最优可用执行器执行；失败按顺序尝试后续候选。
        全部失败抛 ToolError（fclass 已分类），交 Recovery Engine。
        返回形状保持 v6.0：dict 展开 + _tool 标记。
        """
        cands = self.candidates(intent)
        tried = []
        for c in cands:
            schema = c["schema"]
            if not c["available"]:
                continue
            call = ToolCall(schema.name, kwargs, intent=intent)
            result = self._through_adapter(schema, call)
            if result.ok:
                if schema.cost_kind == "paid" and self.budget_left is not None:
                    self.budget_left -= 1
                self.decisions.append({"intent": intent, "selected": schema.name,
                                       "result": "ok", "latency_ms": result.latency_ms})
                return result.to_legacy()
            # 鉴权/预算类失败：换候选无意义，立即终止上抛 Recovery 语义
            if result.fclass == FCLASS_AUTH:
                raise ToolError(FCLASS_AUTH, result.error or "鉴权失败", retriable=False)
            if result.fclass == FCLASS_BUDGET:
                raise ToolError(FCLASS_BUDGET, result.error or "余额不足", retriable=False)
            self.fails[schema.name].append(time.monotonic())
            tried.append((schema.name, str(result.error)[:120]))
            self.decisions.append({"intent": intent, "selected": schema.name,
                                   "result": "fail", "detail": str(result.error)[:120]})
            continue
        if any(c["unavail_reason"] == "no_budget" for c in self.candidates(intent)):
            raise ToolError("TOOL_BUDGET", f"无可用付费额度，候选尝试：{tried}", retriable=False)
        raise ToolError("TOOL_TRANSIENT", f"全部执行器失败：{tried}", retriable=True)
