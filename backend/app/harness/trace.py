# -*- coding: utf-8 -*-
"""
L7 Trace：run 级 span 树（OpenTelemetry 风格的轻量内置版，零外部依赖）。

span 类型：run / plan / handoff / tool / llm / subagent / guard / recovery。
每个 span 记录：name、开始/结束、耗时 ms、状态（ok/error/degraded/skipped）、
属性（tokens/paid/model/tool/intent…）。父子关系用 parent_span_id 串成树。

用法：
    tracer = Tracer(run_id)
    with tracer.span("tool", "aminer.search") as s:
        s.set_attr("subqueries", 18)
    tracer.to_dict()   # 落库/出端点
"""
import time
import uuid
from contextlib import contextmanager


class Span:
    def __init__(self, span_id: str, parent: str | None, kind: str, name: str):
        self.id = span_id
        self.parent = parent
        self.kind = kind
        self.name = name
        self.start = time.monotonic()
        self.end = None
        self.status = "ok"
        self.attrs: dict = {}

    def set_attr(self, key: str, value) -> "Span":
        self.attrs[key] = value
        return self

    def set_status(self, status: str) -> "Span":
        self.status = status
        return self

    @property
    def duration_ms(self) -> float | None:
        if self.end is None:
            return None
        return round((self.end - self.start) * 1000, 1)

    def to_dict(self) -> dict:
        return {"id": self.id, "parent": self.parent, "kind": self.kind,
                "name": self.name,
                "duration_ms": self.duration_ms,
                "status": self.status, "attrs": self.attrs}


class Tracer:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.spans: list[Span] = []
        self._stack: list[str] = []
        self.root = Span("root", None, "run", run_id)
        self.spans.append(self.root)
        self._stack.append("root")

    @contextmanager
    def span(self, kind: str, name: str, **attrs):
        parent = self._stack[-1] if self._stack else "root"
        s = Span(uuid.uuid4().hex[:10], parent, kind, name)
        for k, v in attrs.items():
            s.set_attr(k, v)
        self.spans.append(s)
        self._stack.append(s.id)
        try:
            yield s
        except Exception as e:
            s.set_status("error").set_attr("error", str(e)[:150])
            raise
        finally:
            s.end = time.monotonic()
            self._stack.pop()

    def event(self, kind: str, name: str, status: str = "ok", **attrs) -> Span:
        """零耗时的瞬时标记（如 guard 裁决、recovery 选择）。"""
        parent = self._stack[-1] if self._stack else "root"
        s = Span(uuid.uuid4().hex[:10], parent, kind, name)
        s.set_status(status)
        for k, v in attrs.items():
            s.set_attr(k, v)
        s.start = s.end = time.monotonic()
        self.spans.append(s)
        return s

    def finalize(self, status: str = "ok", **summary) -> dict:
        self.root.set_status(status)
        if self.root.end is None:
            self.root.end = time.monotonic()
        for k, v in summary.items():
            self.root.set_attr(k, v)
        return self.to_dict()

    # ---------- 度量汇总 ----------

    def to_dict(self) -> dict:
        return {"run_id": self.run_id,
                "spans": [s.to_dict() for s in self.spans]}

    def metrics(self) -> dict:
        spans = [s for s in self.spans if s.kind != "run"]
        by_kind: dict[str, int] = {}
        errors, degraded = 0, 0
        tool_ms, llm_ms = 0, 0
        paid = 0
        for s in spans:
            by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
            if s.status == "error":
                errors += 1
            if s.status == "degraded":
                degraded += 1
            if s.kind == "tool":
                tool_ms += s.duration_ms or 0
                paid += s.attrs.get("paid", 0) or 0
            if s.kind == "llm":
                llm_ms += s.duration_ms or 0
        return {"span_count": len(spans), "by_kind": by_kind,
                "errors": errors, "degraded": degraded,
                "tool_ms": round(tool_ms), "llm_ms": round(llm_ms),
                "wall_ms": self.root.to_dict()["duration_ms"],
                "paid_calls": paid}
