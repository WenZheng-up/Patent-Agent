# -*- coding: utf-8 -*-
"""
Skill Registry（声明式 Agent Capability，v6 规格 §7.3）。

Skill 不是 prompt 文件，而是带契约的能力声明：
  name / when（触发意图）/ required_tools（工具子集）/ allowed_teams /
  input_schema / output_schema
Planner 在构建初始计划时按任务意图匹配 skill，skill 决定该流程允许的工具与团队边界。
当前仅注册 patent-cn-search（唯一真实 skill）；架构上支持后续
office-action-response 等以同构方式加入，不做伪复杂分类器。
"""
from dataclasses import dataclass, field


@dataclass
class SkillSpec:
    name: str
    version: str
    description: str
    when: list[str]                       # 触发意图关键词/标签
    required_tools: list[str]             # 该 skill 允许的工具
    allowed_teams: list[str]              # 可调度的团队
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    plan_template: str = "patent_default"  # Supervisor 用哪个初始 DAG 模板


# ---------- 注册表 ----------

_REGISTRY: dict[str, SkillSpec] = {}


def register(skill: SkillSpec) -> None:
    _REGISTRY[skill.name] = skill


def get(name: str) -> SkillSpec | None:
    return _REGISTRY.get(name)


def all_skills() -> list[SkillSpec]:
    return list(_REGISTRY.values())


def match(intent: str, context: dict | None = None) -> SkillSpec | None:
    """
    按任务意图匹配 skill。当前为确定性包含匹配（无 LLM 分类器）：
    意图标签命中 when 即选；多个命中取注册表顺序第一个（可显式 priority 扩展）。
    """
    intent = (intent or "").lower()
    context = context or {}
    # 显式指定优先
    named = context.get("skill")
    if named and named in _REGISTRY:
        return _REGISTRY[named]
    for skill in _REGISTRY.values():
        if any(w.lower() in intent or intent in w.lower() for w in skill.when):
            return skill
    return None


def effective_tools(skill: SkillSpec | None) -> list[str]:
    """该 skill 生效时的工具边界（handoff allowed_tools 的外层来源）。"""
    if skill is None:
        return []
    return list(skill.required_tools)


def permit_tools(skill_tools: list[str] | None, requested: list[str]) -> list[str]:
    """
    handoff 实际工具白名单 = 团队本次申请集 ∩ skill 工具边界。
    skill_tools=None（未匹配到 skill）→ 不做外层限制（向后兼容）。
    交集为空时 fail-closed：返回空表，后续 enforce_tools 会以 OUT_OF_CONTRACT 拒绝
    （越界必须显式声明，而非静默放行）。
    """
    if skill_tools is None:
        return list(requested)
    allowed = set(skill_tools)
    return [n for n in requested if n in allowed]


# ---------- 内置 skill ----------

register(SkillSpec(
    name="patent-cn-search",
    version="1.0",
    description="中国专利新颖性/创造性查新检索：CNF 检索式 fan-out 粗检→族感知精检→"
                "隔离精读→Critic 证据审查→查新报告",
    when=["prior_art_search", "novelty", "查新", "专利检索", "patent search"],
    required_tools=["aminer.search", "aminer.info", "aminer.detail",
                    "local.parse", "local.reread"],
    allowed_teams=["search", "evidence", "review"],
    input_schema={
        "disclosure_text": "交底书全文（string）",
        "approved_query": "HITL 审批后的 CNF 检索组 + IPC + 日期 + budget",
    },
    output_schema={
        "hits": "候选列表（含相关度/命中组合）",
        "priorArt": "claim chart（逐特征公开度+§¶证据+X/Y/A）",
        "report": "结构化查新报告",
    },
    plan_template="patent_default",
))

# 预留（v6.4 不实现，仅占位说明扩展位）：
# register(SkillSpec(name="office-action-response", ...))   # OA 答复辅助
