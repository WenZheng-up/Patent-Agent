# -*- coding: utf-8 -*-
"""
M4 Review Team / Critic（flash）。
Generate → Critique → Repair → Verify 闭环中的 Critique 环节：

输入：Evidence Graph（charts 确定性投影）+ 各专利仅相关段落（按 evidence 反查，≤3k 字）
双轨批判：
  1. 确定性图查询（graph.grade_conflicts / gaps）：法律形式与证据完备性，必查；
  2. LLM critic（flash）：内容充分性——「部分公开」是否其实接近「是」、
     theirs 概述是否被 quote 支持、是否存在该读而未读的角度。只产结构化 critique，不直接改 grade。
输出 Critique：passed + gaps[] + conflicts[] + suggested_actions[]，交 Supervisor 决策 repair。
"""
from ..llm.client import LLMError

SYSTEM = """你是专利查新审查质控员（Critic）。你不做检索、不改分级标签，只做证据审查。
基于「Claim→Feature→Evidence 图」判断 reader 给出的特征比对是否站得住：
- 证据缺口：标注为「是/部分公开」但没有可核验段落证据，或摘录与公开内容概述不符；
- 分级风险：单篇文件含明显未覆盖要素却建议 X；或文件仅背景技术却建议 Y；
- 内容不足：「部分公开」的区别点描述空洞（没有说清到底差什么）。
你可以建议的修复动作只有：targeted_read（补读某专利更多段落）或 targeted_search（补检某角度）。
法律硬规则（X 须单篇全覆盖、日期、同族）由系统规则判定，你不要重复。"""

USER_TMPL = """【本申请特征】
{features}

【Evidence Graph 视图】
{graph_view}

【系统规则已发现的问题】
{rule_findings}

【各专利相关段落摘录（按证据反查）】
{paragraphs}

只输出 JSON：
{{
  "passed": true 或 false,
  "gaps": [{{"pub_no":"公开号或空(全局角度)", "fid":"F编号或空",
             "problem":"missing_evidence|weak_quote|thin_distinction|uncovered_angle",
             "detail":"一句话说明（40字内）",
             "suggested_action":{{"intent":"targeted_read|targeted_search",
                                  "args":{{"patent_ids":["公开号"] 或 "searches":[{{"angle":"角度","terms":["词"]}}]}}}}}}],
  "conflicts": [{{"pub_no":"...", "issue":"...", "suggested_grade":"X|Y|A|null"}}],
  "summary": "总体质控意见（60字内）"
}}
全部通过时 gaps/conflicts 给空数组、passed=true。"""

MAX_ROUND = 2


def critique(features: list[str], graph, patents_by_pub: dict, llm, *,
             rule_findings: list[dict] | None = None) -> dict:
    """
    返回结构化 Critique。LLM 失败时仅用规则发现（fail-open，不阻塞 run）。
    """
    rule_findings = rule_findings or []
    paras = _relevant_paragraphs(graph, patents_by_pub)
    data = None
    try:
        data = llm.chat_json(
            SYSTEM,
            USER_TMPL.format(
                features="\n".join(f"{i}. {t}" for i, t in enumerate(features)),
                graph_view=graph.summary_for_critic(),
                rule_findings=_fmt_findings(rule_findings),
                paragraphs=paras),
            required_keys=["passed"], max_tokens=1800, temperature=0.05)
    except LLMError:
        data = None

    gaps = list(graph.gaps())  # 确定性缺口必入
    conflicts = list(rule_findings)  # 规则冲突必入

    if data:
        for g in data.get("gaps", []) or []:
            if isinstance(g, dict) and g.get("problem"):
                gaps.append({"pub_no": g.get("pub_no", ""), "fid": g.get("fid", ""),
                             "feature": g.get("detail", ""), "level": "",
                             "problem": g.get("problem"),
                             "suggested_action": g.get("suggested_action")})
        for c in data.get("conflicts", []) or []:
            if isinstance(c, dict) and c.get("pub_no"):
                conflicts.append({"pub_no": c["pub_no"],
                                  "reader_grade": graph.patents.get(c["pub_no"],
                                       type("P", (), {"reader_grade": None})()).reader_grade,
                                  "issue": str(c.get("issue", ""))[:100],
                                  "suggested_grade": c.get("suggested_grade")})

    # 去重（pub+problem）
    gaps = _dedup(gaps, ("pub_no", "problem", "fid"))
    conflicts = _dedup(conflicts, ("pub_no", "issue"))

    passed = not gaps and not conflicts
    return {"passed": passed, "gaps": gaps[:6], "conflicts": conflicts[:6],
            "summary": (data or {}).get("summary", "") if data else
                       ("规则质控通过" if passed else f"发现 {len(gaps)} 个证据缺口/{len(conflicts)} 个分级冲突")}


def apply_grade_arbitration(graph, critique: dict) -> list[dict]:
    """
    根据 critic/规则冲突做保守分级仲裁（确定性）：X→Y、A→Y；回写图节点。
    返回实际调整列表（供事件展示）。
    """
    adjusted = []
    for c in critique.get("conflicts", []):
        pub = c.get("pub_no")
        node = graph.patents.get(pub)
        if not node:
            continue
        target = c.get("suggested_grade")
        cur = node.reader_grade
        # 保守规则：X 有任何不完整 → Y；A 有正面公开 → Y；其余维持
        bad_x, _ = graph.x_invalid(pub)
        bad_a, _ = graph.a_invalid(pub)
        if cur == "X" and bad_x:
            node.critic_verdict = "Y"
            adjusted.append({"pub_no": pub, "from": "X", "to": "Y", "reason": c.get("issue", "")})
        elif cur == "A" and bad_a:
            node.critic_verdict = "Y"
            adjusted.append({"pub_no": pub, "from": "A", "to": "Y", "reason": c.get("issue", "")})
        elif target in ("X", "Y", "A") and target != cur and not (cur == "X" and target == "X"):
            node.critic_verdict = target
            adjusted.append({"pub_no": pub, "from": cur, "to": target, "reason": c.get("issue", "")})
    return adjusted


def repair_actions(critique: dict) -> list[dict]:
    """从 critique gaps 抽取可执行 repair 动作（供 Supervisor 插 DAG 节点）。"""
    out = []
    for g in critique.get("gaps", []):
        sa = g.get("suggested_action")
        if isinstance(sa, dict) and sa.get("intent") in ("targeted_read", "targeted_search"):
            out.append({"intent": sa["intent"], "args": sa.get("args") or {},
                        "reason": g.get("feature") or g.get("problem", "")})
    return out


def merge_votes(votes: list[dict], voters: int) -> dict:
    """
    E7 consensus-vote：N 票 critique 的多数决合并。
    - gaps 按 (pub_no, problem, fid) 计票，conflicts 按 (pub_no, suggested_grade) 计票；
    - 规则必发现项在每票中都出现，天然全票；LLM 分歧项需多数（>=ceil(n/2)）才生效；
    - passed 取多数。预期：高风险分级分歧很小，投票主要放大成本（消融 E7 如实报告）。
    """
    if voters <= 1:
        return votes[0]
    quorum = voters // 2 + 1

    def key_g(g):
        return (str(g.get("pub_no", "")), str(g.get("problem", "")), str(g.get("fid", "")))

    def key_c(c):
        return (str(c.get("pub_no", "")), str(c.get("suggested_grade")))

    gap_box: dict = {}
    con_box: dict = {}
    for v in votes:
        for g in v.get("gaps", []):
            bucket = gap_box.setdefault(key_g(g), {"item": g, "n": 0})
            bucket["n"] += 1
        for c in v.get("conflicts", []):
            bucket = con_box.setdefault(key_c(c), {"item": c, "n": 0})
            bucket["n"] += 1

    gaps = [b["item"] for b in gap_box.values() if b["n"] >= quorum]
    conflicts = [b["item"] for b in con_box.values() if b["n"] >= quorum]
    passed_votes = sum(1 for v in votes if v.get("passed"))
    base = votes[0]
    return {
        "passed": passed_votes >= quorum and not gaps and not conflicts,
        "gaps": gaps[:6], "conflicts": conflicts[:6],
        "summary": f"共识投票 {voters} 票（多数阈 {quorum}）：缺口 {len(gaps)}/"
                   f"冲突 {len(conflicts)} 项达多数；{base.get('summary', '')}"[:120],
        "_vote_stats": {"voters": voters, "quorum": quorum,
                        "gaps_kept": len(gaps), "gaps_rejected": len(gap_box) - len(gaps),
                        "conflicts_kept": len(conflicts),
                        "conflicts_rejected": len(con_box) - len(conflicts),
                        "passed_votes": passed_votes},
    }


# ---------- helpers ----------

def _relevant_paragraphs(graph, patents_by_pub: dict, max_chars: int = 3000) -> str:
    """按 evidence 的 paragraphIndex 反查，只给 critic 相关段落（上下文隔离）。"""
    want: dict[str, set] = {}
    for e in graph.edges:
        if e.evidence:
            want.setdefault(e.pub_no, set()).add(e.evidence.paragraph_index)
    buf, used = [], 0
    for pub, idxs in want.items():
        doc = patents_by_pub.get(pub)
        if not doc:
            continue
        buf.append(f"# {pub}")
        for p in doc.get("paragraphs", []):
            if p["index"] in idxs:
                line = f"[{p['index']}]{('§'+p['section']) if p.get('section') else ''} {p['text'][:300]}"
                if used + len(line) > max_chars:
                    return "\n".join(buf)
                buf.append(line)
                used += len(line)
    return "\n".join(buf) or "（无可用段落）"


def _fmt_findings(findings: list[dict]) -> str:
    if not findings:
        return "（无）"
    return "\n".join(f"- {f.get('pub_no')}: {f.get('issue')}" for f in findings)


def _dedup(items: list[dict], keys: tuple) -> list[dict]:
    seen, out = set(), []
    for x in items:
        k = tuple(str(x.get(kk, "")) for kk in keys)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out
