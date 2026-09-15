# -*- coding: utf-8 -*-
"""
事件总线辅助：harness 内所有组件通过 EventSink 发事件。
- 写 EventStore（强类型 etype/actor/causation）
- 转发为 UI 形态（ts/tag/type/msg）给 SSE progress_cb
事件是唯一状态来源；UI tag/type 沿用原型既有 CSS 语义。
"""
from contextlib import nullcontext

from .. import db
from ..services.retrieval_service import _now_hms


class EventSink:
    def __init__(self, case_id: str, run_id: str, progress_cb=None):
        self.case_id = case_id
        self.run_id = run_id
        self.progress_cb = progress_cb
        self.last_seq = -1
        self.tracer = None   # 由 Supervisor 注入 Tracer

    def emit(self, msg: str, tag: str = "runtime.note", etype: str = "runtime.note",
             etype_ui: str = "", actor: str = "harness", payload: dict | None = None,
             causation: int | None = None) -> int:
        ui = {"ts": _now_hms(), "tag": tag, "type": etype_ui, "msg": msg}
        seq = db.append_event(self.case_id, self.run_id, ui,
                              payload=payload, actor=actor, etype=etype,
                              causation_id=causation if causation is not None else self.last_seq)
        self.last_seq = seq
        if self.progress_cb:
            self.progress_cb(ui)
        return seq

    # 语义化快捷方法（统一 UI 样式映射）
    def tool(self, msg: str, **kw):
        return self.emit(msg, tag="tool.search", etype="runtime.note",
                         etype_ui="tool", actor="search_team", **kw)

    def verify(self, msg: str, **kw):
        return self.emit(msg, tag="loop.verify", etype="runtime.note",
                         etype_ui="verify", actor="supervisor", **kw)

    def sub(self, msg: str, **kw):
        return self.emit(msg, tag="subagent.spawn", etype="runtime.note",
                         etype_ui="sub", actor="evidence_team", **kw)

    def sub_done(self, msg: str, **kw):
        return self.emit(msg, tag="subagent.done", etype="runtime.note",
                         etype_ui="sub", actor="evidence_team", **kw)

    def span(self, kind: str, name: str, **attrs):
        """有 tracer 则开真 span，否则 nullcontext（业务代码无判空）。"""
        if self.tracer:
            return self.tracer.span(kind, name, **attrs)
        return nullcontext()
