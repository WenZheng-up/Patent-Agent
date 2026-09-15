# -*- coding: utf-8 -*-
"""
M1 Planner LLM：仅在没有 ready 节点（blocked/刚失败）时调用 flash 做修订决策。
常规推进零 LLM。输出 advance/revise + 结构化 revisions（见 plan.py）。
"""
import json

SYSTEM = """你是专利查新 Agent 的规划器（Supervisor）。你维护一个有向计划图（Plan DAG）。
当前检索任务可能遇到：某技术角度候选为 0、证据不足、预算/工具失败。
你的职责是判断：直接推进（advance）还是修订计划（revise）。
修订只能用以下操作：
- insert：插入新检索节点。检索上游 API 只支持空格 AND 词袋（不支持 OR/括号），
  每个新角度必须配合锚点组（核心主题词）单独检索，禁止把多个不同缺失角度 AND 在一起；
  同义词要写成同组内多个词，由 harness 编译为 fanout。
- retry：重试失败节点（可更新 args）；
- skip：确认该角度属本申请区别特征、现有技术通常不记载时，跳过该检索枝。
注意：同一角度相同词表已执行且 0 结果时，不要重复插入；换同义词/上位词或 skip。
最多修订 3 次。"""

USER_TMPL = """【当前计划】
{plan}

【候选池统计】
并集 {pool_n} 条；多组合命中 {multi} 条。

【Top15 候选标题（编号:命中数 标题）】
{titles}

【待办/阻塞】
{open_gaps}

【最近失败信号】
{failures}

只输出 JSON：
{{
  "decision": "advance" 或 "revise",
  "reason": "一句话依据（40字内）",
  "open_gaps": ["仍未覆盖的角度"],
  "revisions": [
    {{"op":"insert|retry|skip",
      "node": "retry/skip 时给节点 id",
      "intent": "insert 时给 gap_search|targeted_search|targeted_read",
      "owner_team": "search|evidence",
      "depends_on": ["节点id"],
      "args": {{"searches":[{{"angle":"角度名","terms":["锚点主题词","角度同义词1","角度同义词2"]}}]}},
      "reason":"修订原因"}}
  ]
}}
decision=advance 时 revisions 给空数组。"""

REQUIRED = ["decision"]


def decide_revision(llm, plan: dict, pool: list, failures: list[dict], *,
                    max_tokens: int = 1400,
                    experiences_text: str | None = None) -> dict:
    titles = "\n".join(f"{i+1}.({c.score}) {c.title_zh or ''}"
                       for i, c in enumerate(pool[:15])) or "（空）"
    user = USER_TMPL.format(plan=json.dumps(_plan_view(plan), ensure_ascii=False),
                            pool_n=len(pool),
                            multi=sum(1 for c in pool if c.score >= 2),
                            titles=titles,
                            open_gaps=json.dumps(plan.get("open_gaps", []), ensure_ascii=False),
                            failures=json.dumps(failures[-3:], ensure_ascii=False))
    # M6 经验建议态动态区：样本 <10 时为空字符串（行为与无记忆完全一致）
    if experiences_text:
        user += "\n\n" + experiences_text
    data = llm.chat_json(
        SYSTEM, user,
        required_keys=REQUIRED, max_tokens=max_tokens, temperature=0.1)
    decision = data.get("decision")
    if decision not in ("advance", "revise"):
        decision = "advance"
    return {"decision": decision,
            "reason": str(data.get("reason", ""))[:80],
            "open_gaps": data.get("open_gaps", []) if isinstance(data.get("open_gaps"), list) else [],
            "revisions": data.get("revisions", []) if isinstance(data.get("revisions"), list) else []}


def _plan_view(plan: dict) -> dict:
    return {"goal": plan["goal"], "revision": plan["revision"],
            "open_gaps": plan.get("open_gaps", []),
            "nodes": [{"id": n["node_id"], "intent": n["intent"], "status": n["status"],
                       "depends_on": n["depends_on"], "origin": n["origin"],
                       "attempts": n["attempts"]} for n in plan["nodes"]]}
