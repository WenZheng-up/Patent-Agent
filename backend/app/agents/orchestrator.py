# -*- coding: utf-8 -*-
"""
查新检索 Agent Loop 编排器（自研 Harness 的核心）。

与 v1 确定性管道的本质区别：检索不是一次性 fan-out，而是
  observe（粗检候选池）
   → think（LLM 评估技术特征覆盖率）
   → act（充分则收尾；不足则 LLM 改写检索式，带着新 CNF 再 fan-out）
  最多 max_iterations 轮；随后预算精检 → sub-agent 并行精读 → 引用护栏。

每一步都通过 progress_cb 发出 UI 形态事件（与 API_CONTRACT §6.2 同构），
由 FastAPI 层 SSE 推给前端并写入 append-only 事件日志（Event Sourcing）。
LLM 任何环节失败都有确定性降级：自评失败按单轮处理、精读失败降级关键词骨架。
"""
import copy

from ..llm.client import LLMClient, LLMError
from ..providers.aminer_client import AminerClient
from ..retrieval.pipeline import CoarseResult, coarse_search, fine_retrieve
from ..services.comparison_service import _features_from_disclosure
from ..services.retrieval_service import (
    _in_date_range, build_hits, translate_event, _now_hms)
from . import reader_agent

EVAL_SYSTEM = """你是专利检索质量评估器。根据查新检索的候选专利标题（粗检结果），
判断本申请的技术特征是否已被候选池充分覆盖，决定是否需要改写检索式补检。
标题信息有限时，对明确出现核心构思组合的应判充分；只有在某必要技术角度完全没有候选时才补检。"""

EVAL_USER = """【本申请技术特征】
{features}

【检索式】
{expr}

【粗检候选标题 Top {n}】
{titles}

只输出 JSON：
{{
  "decision": "sufficient" 或 "rewrite",
  "reason": "一句话评估依据（40字内）",
  "missing": ["未被覆盖的技术角度"],
  "searches": [
    {{"angle": "缺失角度短名",
      "terms": ["该角度的 1-3 个检索词/同义词（专利文献常见表述）"]}}
  ]
}}
规则：
- 每个 search 只针对一个缺失角度，给该角度专有的词，不要 AND 多个不同角度（否则检不到）；
- 缺失角度属于区别特征、专利文献通常不会完整记载时，判 sufficient，不要为补检而补检；
- decision=sufficient 时 searches 给空数组。"""


def _ui_event(tag, etype, msg):
    return {"ts": _now_hms(), "tag": tag, "type": etype, "msg": msg}


def _merge_coarse(acc: CoarseResult | None, new: CoarseResult) -> CoarseResult:
    """合并多轮粗检：同 id 候选的命中子查询累加（子查询序号跨轮偏移）。"""
    if acc is None:
        return new
    offset = len(acc.subqueries)
    pool = {c.aminer_id: copy.deepcopy(c) for c in acc.candidates}
    for cand in new.candidates:
        old = pool.get(cand.aminer_id)
        if old is None:
            shifted = copy.deepcopy(cand)
            shifted.hits = [(i + offset, r) for i, r in shifted.hits]
            pool[cand.aminer_id] = shifted
        else:
            old.hits.extend((i + offset, r) for i, r in cand.hits)
            if not old.title_zh and cand.title_zh:
                old.title_zh = cand.title_zh
            if not old.pub_year and cand.pub_year:
                old.pub_year = cand.pub_year
    merged_meta = dict(acc.meta)
    merged_meta["truncated"] = acc.meta.get("truncated") or new.meta.get("truncated")
    candidates = sorted(pool.values(),
                        key=lambda c: (-c.score, c.best_rank, c.title_zh or ""))
    return CoarseResult(subqueries=acc.subqueries + new.subqueries,
                        meta=merged_meta, candidates=candidates,
                        active_subqueries=acc.active_subqueries + new.active_subqueries)


def evaluate_coverage(llm, features: list[str], expr: str,
                      top_candidates: list[CoarseCandidate]) -> dict:
    titles = "\n".join(f"{i+1}. {c.title_zh or ''}" for i, c in enumerate(top_candidates))
    data = llm.chat_json(
        EVAL_SYSTEM,
        EVAL_USER.format(features="\n".join(f"{i+1}. {f}" for i, f in enumerate(features)),
                         expr=expr, n=len(top_candidates), titles=titles),
        required_keys=["decision"], max_tokens=1600, temperature=0.05)
    decision = data.get("decision") if data.get("decision") in ("sufficient", "rewrite") \
        else "sufficient"
    searches = []
    for s in data.get("searches", []) or []:
        if isinstance(s, dict) and isinstance(s.get("terms"), list):
            terms = [str(t).strip() for t in s["terms"] if str(t).strip()]
            if terms:
                searches.append({"angle": str(s.get("angle", "补检"))[:20],
                                 "terms": terms[:3]})
    return {"decision": decision, "reason": str(data.get("reason", ""))[:80],
            "missing": [str(x)[:30] for x in data.get("missing", []) if x][:4],
            "searches": searches[:3]}


def build_rewrite_cnfs(verdict_searches: list[dict],
                       anchor_groups: list[list[str]]) -> list[tuple[str, list[list[str]]]]:
    """
    补检计划：每个缺失角度构成一个独立 CNF（核心锚点组 + 该角度词），
    各角度分别 fan-out 后并集——避免「多角度 AND」互相收窄为 0 命中。
    返回 [(angle, groups), ...]，每个 groups 供一次 coarse_search。
    """
    anchor = [g[:3] for g in anchor_groups[:2]] or anchor_groups[:1]
    plans = []
    for s in verdict_searches:
        plans.append((s["angle"], [g[:] for g in anchor] + [s["terms"]]))
    return plans


def _citation_guard(prior_art: list[dict]) -> tuple[int, int]:
    """引用完整性护栏：公开程度为 是/部分 的特征必须挂段落证据。返回 (已挂, 应挂)。"""
    need, have = 0, 0
    for art in prior_art:
        for f in art.get("features", []):
            if f.get("disclosed") in ("是", "部分"):
                need += 1
                if f.get("evidence"):
                    have += 1
    return have, need


def run_agent(case: dict, *, budget: int, min_score: int = 2, size: int = 100,
              max_iterations: int = 2, llm: LLMClient | None = None,
              client: AminerClient | None = None, progress_cb=None) -> dict:
    """
    执行 Agentic 两阶段检索 + 精读。返回 {summary, hits, events, prior_art, patents}。
    """
    def emit(tag, etype, msg):
        ev = _ui_event(tag, etype, msg)
        events.append(ev)
        if progress_cb:
            progress_cb(ev)

    events: list[dict] = []
    llm = llm or LLMClient()
    client = client or AminerClient()
    query = case["query"]
    groups = query.get("groups")
    features = _features_from_disclosure(case.get("disclosure", {}))

    emit("hitl.resume", "hitl",
         f"代理人确认检索式 → checkpoint 恢复执行（精检预算 {budget} 篇，迭代上限 {max_iterations}）")

    # ---------- Agentic 粗检循环（think-act-observe） ----------
    def pipe_cb(ev):
        ui = translate_event(ev)
        if ui:
            emit(ui["tag"], ui["type"], ui["msg"])

    # 基础粗检（审批检索式的完整 CNF）
    merged = coarse_search(client, groups, size=size, progress_cb=pipe_cb)
    iterations = 1

    # Agentic 迭代：observe 候选池 → think 覆盖率自评 → act 分角度补检
    for rnd in range(max_iterations):
        if len(merged.candidates) < 5:
            break
        emit("loop.verify", "verify",
             f"覆盖率自评（迭代 {rnd + 1}/{max_iterations}）：对照 {len(features)} 个技术特征分析 Top15 候选…")
        try:
            verdict = evaluate_coverage(llm, features, query.get("expr", ""),
                                        merged.candidates[:15])
        except LLMError as e:
            emit("loop.verify", "verify", f"自评服务不可用（{str(e)[:40]}）→ 按现有候选继续")
            break
        if verdict["decision"] == "sufficient" or not verdict["searches"]:
            emit("loop.verify", "verify",
                 f"质量评估：{verdict['reason']} → 召回充分，进入精检")
            break
        plans = build_rewrite_cnfs(verdict["searches"], groups)
        emit("loop.verify", "verify",
             f"质量评估：{verdict['reason']}；缺口 {'、'.join(verdict['missing']) or '—'} → 触发 "
             f"{len(plans)} 个角度补检")
        any_new = False
        for angle, plan_groups in plans:
            emit("agent.rewrite", "",
                 f"补检角度「{angle}」：{len(plan_groups)} 组 CNF fan-out")
            try:
                extra = coarse_search(client, plan_groups, size=size, progress_cb=pipe_cb)
            except Exception as e:
                emit("guard.retry", "verify", f"补检「{angle}」失败：{str(e)[:40]}，跳过")
                continue
            before = len(merged.candidates)
            merged = _merge_coarse(merged, extra)
            iterations += 1
            if len(merged.candidates) > before:
                any_new = True
        if not any_new:
            emit("loop.verify", "verify", "补检未带来新候选 → 采纳现有候选进入精检")
            break
    else:
        emit("loop.verify", "verify", f"已达迭代上限 {max_iterations} 轮 → 进入精检")

    # ---------- 日期过滤（免费 pub_year） ----------
    year_from = int(query["dateFrom"][:4]) if query.get("dateFrom") else None
    year_to = int(query["dateTo"][:4]) if query.get("dateTo") else None
    before = len(merged.candidates)
    filtered = [c for c in merged.candidates if _in_date_range(c, year_from, year_to)]
    date_filtered = before - len(filtered)
    emit("loop.verify", "verify",
         f"粗检 {iterations} 轮共 {len(merged.subqueries)} 子查询：并集 {before} 条"
         + (f"，日期过滤 {date_filtered} 条" if date_filtered else "")
         + f"，{sum(1 for c in filtered if c.score >= 2)} 条命中 ≥2 组合")

    # ---------- 预算精检（族感知，付费） ----------
    fine = fine_retrieve(client, filtered, budget=budget, min_score=min_score,
                         progress_cb=pipe_cb)
    fine_patents = [p for p in fine.patents if p.get("paragraphs") or p.get("abstract")]

    # ---------- Sub-agent 并行精读 ----------
    emit("subagent.spawn", "sub",
         f"派生 {len(fine_patents)} 个隔离子代理：逐篇全文精读（独立上下文，并行）")
    for p in fine_patents:
        p["_ui_score"] = round(p["coarse_score"] / max(merged.active_subqueries, 1), 2)

    def reader_progress(done_n, total, ok, pub_no):
        emit("subagent.done", "sub",
             f"{pub_no} 精读完成 {done_n}/{total} → "
             + ("特征比对表 + X/Y/A 建议已回传" if ok else "LLM 不可用，降级关键词骨架"))

    prior_art, reader_stats = reader_agent.read_all(
        fine_patents, features, case.get("title", ""), llm,
        max_workers=3, progress_cb=reader_progress)

    # ---------- 引用护栏 ----------
    have, need = _citation_guard(prior_art)
    if need == 0:
        guard_msg = "引用完整性校验：无需挂证的公开特征（全部未公开）"
    else:
        guard_msg = f"引用完整性校验：{have}/{need} 条公开结论已挂（专利+§章节¶序号）"
        if have < need:
            guard_msg += f"，{need - have} 条缺证已标记，需代理师复核"
    emit("guard.cite", "verify", guard_msg + " → 通过" if have == need else guard_msg)

    # ---------- 汇总 ----------
    hits = build_hits(merged, filtered, fine, query.get("ipc", []))
    grade_counts = {"X": 0, "Y": 0, "A": 0}
    for a in prior_art:
        if a.get("grade") in grade_counts:
            grade_counts[a["grade"]] += 1
    summary = {
        "subqueryCount": len(merged.subqueries),
        "coarseTotal": len(filtered),
        "coarseMultiHit": sum(1 for c in filtered if c.score >= 2),
        "families": fine.families_built,
        "finePatents": len(fine_patents),
        "paidDetailCalls": fine.paid_detail_calls,
        "dateFiltered": date_filtered,
        "truncated": merged.meta.get("truncated", False),
        "iterations": iterations,
        "grades": grade_counts,
        "llmRead": reader_stats["llm_read"],
        "degradedRead": reader_stats["degraded"],
        "llmTokens": reader_stats["usage"].get("prompt_tokens", 0)
                      + reader_stats["usage"].get("completion_tokens", 0),
        "cachedTokens": reader_stats["usage"].get("cached_tokens", 0),
    }
    emit("loop.verify", "verify",
         f"Agent 收尾：{iterations} 轮检索 · X {grade_counts['X']} / Y {grade_counts['Y']} / "
         f"A {grade_counts['A']} 篇 · LLM tokens {summary['llmTokens']}（缓存命中 {summary['cachedTokens']}）")

    return {"summary": summary, "hits": hits, "events": events,
            "prior_art": prior_art, "patents": fine_patents}
