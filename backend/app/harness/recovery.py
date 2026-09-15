# -*- coding: utf-8 -*-
"""
M5 Failure Recovery Engine。
统一错误分类（确定性）+ 策略矩阵；事件化 failure.classified / recovery.selected；
可恢复错误 withholding；每类熔断计数器持久化进 State。
"""
from ..llm.client import LLMError
from ..providers.aminer_client import AminerAuthError, AminerBalanceError, AminerError
from .router import ToolError
from .plan import PlanError

# Failure 类别
TOOL_TRANSIENT = "TOOL_TRANSIENT"
TOOL_AUTH = "TOOL_AUTH"
TOOL_BUDGET = "TOOL_BUDGET"
ZERO_RESULT = "ZERO_RESULT"
EVIDENCE_GAP = "EVIDENCE_GAP"
GRADE_CONFLICT = "GRADE_CONFLICT"
PLAN_INVALID = "PLAN_INVALID"
LLM_DEGRADED = "LLM_DEGRADED"


def classify(exc: Exception) -> str:
    if isinstance(exc, AminerAuthError):
        return TOOL_AUTH
    if isinstance(exc, AminerBalanceError):
        return TOOL_BUDGET
    if isinstance(exc, ToolError):
        return exc.fclass
    if isinstance(exc, PlanError):
        return PLAN_INVALID
    if isinstance(exc, LLMError):
        return LLM_DEGRADED
    if isinstance(exc, AminerError):
        return TOOL_TRANSIENT
    msg = str(exc)
    if "401" in msg or "403" in msg:
        return TOOL_AUTH
    if "余额" in msg or "5002" in msg:
        return TOOL_BUDGET
    if "429" in msg or "5" in msg[:3] or "timeout" in msg.lower():
        return TOOL_TRANSIENT
    return TOOL_TRANSIENT


# 每类熔断上限
LIMITS = {TOOL_TRANSIENT: 3, LLM_DEGRADED: 2, PLAN_INVALID: 2}


class RecoveryEngine:
    """在 Supervisor 里以"分类→选择策略→记账"方式使用；实际动作由 Supervisor 执行。"""

    def __init__(self, sink):
        self.sink = sink
        self.counts: dict[str, int] = {}

    def note(self, fclass: str, causation: int | None = None) -> int:
        self.counts[fclass] = self.counts.get(fclass, 0) + 1
        return self.sink.emit(
            f"失败分类：{fclass}（第 {self.counts[fclass]} 次）",
            tag="failure.classified", etype="failure.classified",
            etype_ui="verify", actor="recovery",
            payload={"fclass": fclass, "count": self.counts[fclass]},
            causation=causation)

    def select(self, fclass: str, causation: int | None = None) -> str:
        """返回策略名（Supervisor 解释执行）。"""
        strategy = {
            TOOL_TRANSIENT: "retry_backoff_then_fallback",
            TOOL_AUTH: "abort_with_message",
            TOOL_BUDGET: "free_path_or_ask_human",
            ZERO_RESULT: "rewrite_angle_via_planner",
            EVIDENCE_GAP: "insert_targeted_action",
            GRADE_CONFLICT: "arbitrate_conservative",
            PLAN_INVALID: "feed_three_part_error_to_planner",
            LLM_DEGRADED: "deterministic_fallback_pipeline",
        }[fclass]
        self.sink.emit(f"恢复策略：{strategy}", tag="recovery.selected",
                       etype="recovery.selected", etype_ui="verify",
                       actor="recovery", payload={"fclass": fclass, "strategy": strategy},
                       causation=causation)
        return strategy

    def exhausted(self, fclass: str) -> bool:
        return self.counts.get(fclass, 0) > LIMITS.get(fclass, 2)
