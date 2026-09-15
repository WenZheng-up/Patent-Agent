# -*- coding: utf-8 -*-
"""
M2 Typed Handoff 协议（契约即事件）。
Team Lead 只接受带完整契约的任务；worker 越界请求不自行执行，随 HandoffResult 回 Supervisor。
"""
import itertools

_ho_seq = itertools.count(1)


def open_handoff(sink, *, from_team: str, to_team: str, goal: str,
                 constraints: list[str], success_criteria: list[str],
                 allowed_tools: list[str], budget: dict,
                 context_refs: list[str] | None = None,
                 evidence_refs: list[str] | None = None,
                 return_schema: str = "ResultV1", deadline_ms: int = 180000) -> dict:
    ho = {
        "handoff_id": f"ho_{next(_ho_seq):04d}",
        "from": from_team, "to": to_team,
        "task": {"goal": goal, "constraints": constraints,
                 "success_criteria": success_criteria},
        "context_refs": context_refs or [],
        "evidence_refs": evidence_refs or [],
        "allowed_tools": allowed_tools,
        "budget": budget,
        "deadline_ms": deadline_ms,
        "return_schema": return_schema,
    }
    sink.emit(f"Handoff → {to_team}：{goal}（预算 {budget}）",
              tag="handoff.opened", etype="handoff.opened",
              etype_ui="tool", actor=from_team, payload=ho)
    return ho


def return_handoff(sink, ho: dict, *, status: str, outputs_ref: str | None,
                   tokens: dict | None = None, paid_used: int = 0,
                   requests: list[dict] | None = None, actor: str | None = None) -> dict:
    result = {"ho_ref": ho["handoff_id"], "status": status,
              "outputs_ref": outputs_ref, "tokens": tokens or {},
              "paid_used": paid_used, "requests": requests or []}
    sink.emit(f"Handoff ← {ho['to']}：{status}（付费 {paid_used}，请求 {len(result['requests'])} 项）",
              tag="handoff.returned", etype="handoff.returned",
              etype_ui="tool", actor=actor or ho["to"], payload=result,
              causation=None)
    return result


def enforce_tools(ho: dict, tool_name: str) -> None:
    """worker 调用白名单外工具 → 拒绝（越界必须走 requests 上报）。"""
    if tool_name not in ho["allowed_tools"]:
        raise PermissionError(f"OUT_OF_CONTRACT: {tool_name} 不在 handoff {ho['handoff_id']} "
                              f"白名单 {ho['allowed_tools']}；请回传 request 由 Supervisor 决策")
