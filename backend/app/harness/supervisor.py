# -*- coding: utf-8 -*-
"""
Supervisor：ChaxinHarness v6.0 主循环。

职责：
1. 持有 PlanDAG 与折叠 State（候选池合并/charts/计数器）；
2. ready 节点直接经 Tool Router + Team Handoff 执行（常规推进零规划 LLM）；
3. blocked 时调 flash Planner 输出修订（M1），三闸门 apply；
4. deep_read 走 Evidence Team（M2 + 付费幂等 R1）；
5. submit 前（v6.0）做基础护栏（证据存在/同族已在读取阶段处理），产出终态；
6. 任何失败经 Recovery Engine 分类；LLM 全链路不可用 → 抛信号由 main.py 降级 v2 管道。

输出：{summary, hits, events(UI), prior_art, patents, stop_reason}
"""
import copy
from datetime import datetime

from ..llm.client import LLMClient, LLMError
from ..providers.aminer_client import AminerClient
from ..services.retrieval_service import (
    _in_date_range, build_hits, _ipc_match)
from . import plan as planmod
from .events import EventSink
from .graph import EvidenceGraph
from .handoff import open_handoff, return_handoff
from .planner_llm import decide_revision
from .recovery import RecoveryEngine, classify, TOOL_BUDGET, TOOL_AUTH, PLAN_INVALID, LLM_DEGRADED, ZERO_RESULT
from .router import ToolRouter
from . import skills
from . import memory
from .toolbase import registry_names
from .teams import run_search_team, run_evidence_team
from . import teams_review
from .trace import Tracer
from ..services.comparison_service import _features_from_disclosure


class HarnessDegraded(Exception):
    """LLM 规划不可用且无法继续智能模式——调用方降级 v2 管道。"""


def run_supervisor(case: dict, *, budget: int, min_score: int = 2, size: int = 100,
                   llm: LLMClient | None = None, client: AminerClient | None = None,
                   progress_cb=None, run_id: str | None = None,
                   enable_critic: bool = True, consensus: int = 1,
                   reader_llm=None) -> dict:
    """
    enable_critic：False 为消融 E2（无 critic repair 闭环）。
    consensus：critic 分级仲裁投票数（1=单票=E3；3=E7 多数决）。
    reader_llm：精读/补读专用模型（E8 模型路由）；默认与规划同一 LLM。
    """
    case_id = case["case_id"] if "case_id" in case else case["id"]
    run_id = run_id or "rs_" + datetime.now().strftime("%Y%m%d%H%M%S")
    sink = EventSink(case_id, run_id, progress_cb)
    tracer = Tracer(run_id)
    sink.tracer = tracer
    llm = llm or LLMClient()
    reader_llm = reader_llm or llm
    client = client or AminerClient()

    router = ToolRouter({
        "aminer.search": client.search,
        "aminer.info": client.info,
        "aminer.detail": client.detail,
    }, budget_left=None, client=client)  # 付费预算在 evidence handoff 层控制
    last_fine = None

    recovery = RecoveryEngine(sink)
    query = case["query"]
    groups = query["groups"]
    features_need = 0  # submit 阶段校验用

    # ---------- 初始化 Plan + State ----------
    dag = planmod.initial_plan(case, with_critic=enable_critic)
    used_angle_keys: set = set()
    merged = None
    prior_art, patents = [], []
    graph: EvidenceGraph | None = None
    critique_round = 0
    failures: list[dict] = []
    stop_reason = None
    causation = sink.emit(
        f"HITL 审批确认 → checkpoint 恢复执行（预算 {budget} 篇，迭代上限 {planmod.MAX_REVISIONS}）",
        tag="hitl.resume", etype="human.approved", etype_ui="hitl", actor="human")

    # ---------- Skill 匹配（确定性包含匹配，零 LLM）----------
    # skill 声明本次流程的工具/团队外层边界；初始 DAG 模板也由 skill 选定。
    intent = case.get("intent") or case.get("task_type") or "prior_art_search"
    skill = skills.match(intent, {"skill": case.get("skill")})
    if skill:
        # 契约完整性：skill 声明的工具必须存在于 Tool 注册表（防白名单声明漂移）
        unknown_tools = [t for t in skill.required_tools if t not in registry_names()]
        if unknown_tools:
            sink.verify(f"skill {skill.name} 声明了未注册工具 {unknown_tools}"
                        f" → 对应 handoff 将 fail-closed（请先注册 ToolSchema）")
        dag["skill"] = {"name": skill.name, "version": skill.version,
                        "required_tools": list(skill.required_tools),
                        "allowed_teams": list(skill.allowed_teams),
                        "plan_template": skill.plan_template}
        # skill_tools=None 表示无边界（未匹配 skill，向后兼容）；命中后为交集白名单
        dag["skill_tools"] = list(skill.required_tools)
        sink.emit(f"Skill 匹配：{skill.name} v{skill.version} → 工具边界 "
                  f"{len(skill.required_tools)} 个 / 团队 {','.join(skill.allowed_teams)}"
                  f"（模板 {skill.plan_template}，确定性匹配零 LLM）",
                  tag="skill.matched", etype="skill.matched", etype_ui="verify",
                  actor="supervisor", causation=causation,
                  payload={"intent": intent, "name": skill.name, "version": skill.version,
                           "required_tools": skill.required_tools,
                           "allowed_teams": skill.allowed_teams,
                           "plan_template": skill.plan_template})
    else:
        dag["skill"] = None
        dag["skill_tools"] = None
        sink.verify(f"未匹配到声明式 skill（intent={intent}）→ 使用无外层工具边界的默认流程")

    sink.emit(f"Plan v1 创建：{len(dag['nodes'])} 个节点（anchor_search→deep_read→submit）",
              tag="plan.created", etype="plan.created", etype_ui="verify",
              actor="supervisor", payload={"plan": dag}, causation=causation)

    # ---------- M6 经验建议态门槛（approved <10 不启用，明示可审计）----------
    mem_situation = memory.situation_of(case)
    approved_n = memory.approved_count()
    if approved_n >= memory.MIN_APPROVED:
        sink.verify(f"经验建议态已启用（approved {approved_n} 条）：Planner 修订时"
                    f"注入 ≤{memory.MAX_INJECT} 条带来源建议（建议态，可忽略）")
    else:
        sink.verify(f"经验库样本 {approved_n}/{memory.MIN_APPROVED}：建议态未启用"
                    f"（小样本不注入，避免过拟合）")

    # 主循环（有界保护）
    for _guard in range(30):
        if planmod.is_finished(dag):
            stop_reason = "submitted"
            break

        ready = planmod.ready_nodes(dag)

        if ready:
            node = ready[0]
            try:
                with tracer.span("plan", f"node:{node['node_id']}:{node['intent']}",
                                 origin=node.get("origin")) as _nspan:
                    merged, prior_art, patents, graph, last_fine, stop_reason = _execute_node(
                        node, dag, case, groups, merged, prior_art, patents, graph,
                        critique_round, last_fine,
                        router, llm, client, sink, recovery, used_angle_keys,
                        budget, min_score, size, failures, case_id, run_id,
                        reader_llm=reader_llm, consensus=consensus, span=_nspan)
                if stop_reason:
                    break
            except _ForceStop as fs:
                stop_reason = fs.reason
                break
            except Exception as e:
                fclass = classify(e)
                cseq = recovery.note(fclass, causation=sink.last_seq)
                strategy = recovery.select(fclass, causation=cseq)
                failures.append({"fclass": fclass, "detail": str(e)[:150], "strategy": strategy,
                                 "node": node["node_id"]})
                node["attempts"] += 1

                if fclass == TOOL_AUTH:
                    raise
                if fclass == TOOL_BUDGET:
                    stop_reason = "force_submit_budget" if merged else "force_submit_empty"
                    break

                # 可重试类（工具瞬时/LLM 降级）：Recovery 直接重置节点，不打扰 Planner
                if fclass in ("TOOL_TRANSIENT", "LLM_DEGRADED") and node["attempts"] <= 2:
                    planmod._node(dag, node["node_id"])["status"] = "ready"
                    sink.verify(f"节点 {node['node_id']} 第 {node['attempts']} 次自动重试（{fclass}）")
                    continue
                # 重试用尽或策略性失败（ZERO_RESULT/EVIDENCE_GAP/PLAN_INVALID）
                # repair 节点失败不阻断提交：skip 该节点让 DAG 继续
                if node.get("origin") == "inserted_by_critic":
                    planmod._node(dag, node["node_id"])["status"] = "skipped"
                    sink.verify(f"repair 节点 {node['node_id']} 无法完成（{fclass}）→ 跳过，继续复验")
                    continue
                planmod.mark_failed(dag, node["node_id"])
                if recovery.exhausted(fclass):
                    stop_reason = "force_submit_budget" if merged else "force_submit_empty"
                    break
            continue

        # 没有 ready：blocked/failed → Planner 决策
        if planmod.failed_or_blocked(dag):
            # M6：建议态经验注入（门槛内 suggest 直接返回 []，零行为变化）
            suggestions = memory.suggest(mem_situation)
            exp_text = memory.render_planner_block(suggestions)
            if suggestions:
                sink.emit(f"Planner 注入 {len(suggestions)} 条历史经验建议"
                          f"（建议态，不自动生效，可忽略）",
                          tag="memory.suggested", etype="memory.suggested", etype_ui="verify",
                          actor="supervisor",
                          payload={"suggestions": [
                              {"exp_id": e["exp_id"], "source_case": e["source_case"],
                               "confidence": e["confidence"],
                               "attempted_strategy": e["attempted_strategy"]}
                              for e in suggestions]})
                memory.note_shown([e["exp_id"] for e in suggestions])
            try:
                with tracer.span("llm", "planner.revise", model="flash"):
                    verdict = decide_revision(llm, dag,
                                              merged.candidates if merged else [], failures,
                                              experiences_text=exp_text or None)
            except LLMError as e:
                seq = recovery.note(LLM_DEGRADED)
                recovery.select(LLM_DEGRADED, causation=seq)
                raise HarnessDegraded(str(e))

            if verdict["decision"] == "advance" or not verdict["revisions"]:
                # Planner 认为无需修订：失败节点 skip，推动后续
                _skip_blocked(dag, sink)
                if planmod.is_finished(dag):
                    stop_reason = "submitted" if prior_art else "force_submit_empty"
                    break
                continue

            seq = sink.emit(f"Plan 修订评估：{verdict['reason']} → 应用 {len(verdict['revisions'])} 项修订",
                            tag="plan.revised", etype="plan.revised", etype_ui="verify",
                            actor="supervisor",
                            payload={"revisions": verdict["revisions"], "reason": verdict["reason"]})
            try:
                applied = planmod.apply_revisions(
                    dag, verdict["revisions"], used_angle_keys=used_angle_keys,
                    origin="inserted_by_planner")
                planmod.set_gaps(dag, verdict["open_gaps"])
                for a in applied:
                    sink.emit(f"计划修订：{a['op']} {a.get('node_id', '')} {a.get('intent', '')}（{a['reason'][:40]}）",
                              tag="agent.rewrite" if a["op"] == "insert" else "loop.verify",
                              etype="plan.revised", etype_ui="" if a["op"] == "insert" else "verify",
                              actor="supervisor", causation=seq)
            except Exception as e:
                # 修订非法：回喂三要素，Planner 下轮重试（计数器内）
                cseq = recovery.note(PLAN_INVALID)
                recovery.select(PLAN_INVALID, causation=cseq)
                failures.append({"fclass": PLAN_INVALID, "detail": str(e)[:150],
                                 "strategy": "feed_three_part_error"})
                sink.emit(f"修订被拒绝：{str(e)[:100]}", tag="guard.retry",
                          etype="failure.classified", etype_ui="verify", actor="supervisor",
                          causation=cseq)
                if recovery.exhausted(PLAN_INVALID):
                    raise HarnessDegraded("plan 修订连续非法")
            continue

        stop_reason = "force_submit_empty"
        break

    # ---------- 终态汇总 ----------
    if merged is None:
        stop_reason = "force_submit_empty"

    year_from = int(query["dateFrom"][:4]) if query.get("dateFrom") else None
    year_to = int(query["dateTo"][:4]) if query.get("dateTo") else None
    if merged:
        before = len(merged.candidates)
        filtered = [c for c in merged.candidates if _in_date_range(c, year_from, year_to)]
        date_filtered = before - len(filtered)
        hits = build_hits(merged, filtered, last_fine or _LastFine(patents),
                          query.get("ipc", []))
    else:
        filtered, date_filtered, hits = [], 0, []

    grade_counts = {"X": 0, "Y": 0, "A": 0}
    for a in prior_art:
        if a.get("grade") in grade_counts:
            grade_counts[a["grade"]] += 1

    summary = {
        "subqueryCount": len(merged.subqueries) if merged else 0,
        "coarseTotal": len(filtered),
        "coarseMultiHit": sum(1 for c in filtered if c.score >= 2),
        "finePatents": len(patents),
        "paidDetailCalls": sum(1 for _ in patents),
        "dateFiltered": date_filtered,
        "iterations": dag["revision"],
        "grades": grade_counts,
        "planRevisions": dag["revision"] - 1,
        "stopReason": stop_reason,
        "skill": (dag.get("skill") or {}).get("name") if dag.get("skill") else None,
        "llmTokens": llm.usage.get("prompt_tokens", 0) + llm.usage.get("completion_tokens", 0),
        "cachedTokens": llm.usage.get("cached_tokens", 0),
    }
    sink.emit(f"Agent 收尾：plan revision {summary['planRevisions']} 次 · "
              f"X {grade_counts['X']}/Y {grade_counts['Y']}/A {grade_counts['A']} 篇 · "
              f"stop={stop_reason} · tokens {summary['llmTokens']}（缓存 {summary['cachedTokens']}）",
              tag="run.finished", etype="run.finished", etype_ui="verify", actor="supervisor")

    trace = tracer.finalize(
        stop_reason, grades=grade_counts,
        llm_tokens=summary["llmTokens"], cached=summary["cachedTokens"])
    summary["trace"] = tracer.metrics()

    return {"summary": summary, "hits": hits, "prior_art": prior_art,
            "patents": patents, "events": [], "stop_reason": stop_reason,
            "run_id": run_id, "trace": trace}


class _ForceStop(Exception):
    def __init__(self, reason):
        self.reason = reason


class _LastFine:
    """build_hits 需要 FineResult 形态；v6 终态只用到 infos_by_id/patents/score_by_id。"""
    def __init__(self, patents, fine=None):
        self.infos_by_id = getattr(fine, "infos_by_id", {}) if fine else {}
        self.patents = patents
        self.score_by_id = getattr(fine, "score_by_id", {}) if fine else {}


def _merge_coarse(acc, new):
    """多轮粗检候选并集：同 id 命中子查询累加（子查询序号跨轮偏移）。"""
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
    from ..retrieval.pipeline import CoarseResult
    return CoarseResult(subqueries=acc.subqueries + new.subqueries,
                        meta=merged_meta, candidates=candidates,
                        active_subqueries=acc.active_subqueries + new.active_subqueries)


def _skip_blocked(dag, sink):
    for n in dag["nodes"]:
        if n["status"] in ("failed",) :
            n["status"] = "skipped"
            sink.emit(f"节点 {n['node_id']}({n['intent']}) 跳过（Planner 判定无需修订）",
                      tag="loop.verify", etype="plan.revised", etype_ui="verify",
                      actor="supervisor")


def _execute_node(node, dag, case, groups, merged, prior_art, patents, graph,
                  critique_round, last_fine,
                  router, llm, client, sink, recovery, used_angle_keys,
                  budget, min_score, size, failures, case_id, run_id,
                  *, reader_llm=None, consensus: int = 1, span=None):
    """执行一个 ready 节点。
    返回 (merged, prior_art, patents, graph, last_fine, stop_reason)。
    critique 节点 repair 时直接在 DAG 插入 repair 节点并保持本节点 running→done。"""
    planmod.mark_running(dag, node["node_id"])
    intent = node["intent"]

    if intent in ("anchor_search", "gap_search", "targeted_search"):
        node_groups = node["args"].get("searches")
        # anchor：用审批 CNF；gap/targeted：args 里给的就是各组 terms（harness 编译 fanout）
        if intent == "anchor_search":
            eff_groups = groups
            goal = "审批检索式 fan-out 粗检"
        else:
            eff_groups = node_groups
            goal = f"补检：{node_args_angle(node)}"
        ho = open_handoff(sink, from_team="supervisor", to_team="search_team",
                          goal=goal,
                          constraints=["只支持词袋 AND，禁止多角度 AND", "免费接口"],
                          success_criteria=["返回候选并并入候选池"],
                          allowed_tools=skills.permit_tools(dag.get("skill_tools"),
                                                            ["aminer.search"]),
                          budget={"paid_reads": 0},
                          context_refs=[f"plan:{node['node_id']}"],
                          return_schema="CoarseResultV1")
        with sink.span("tool", "search_team.fanout", subqueries=len(eff_groups)):
            out = run_search_team(ho, eff_groups, router=router, size=size, sink=sink)
        return_handoff(sink, ho, status="ok", outputs_ref=f"coarse:{node['node_id']}",
                       tokens=dict(llm.usage), paid_used=0)
        new_coarse = out["coarse"]
        pool_before = len(merged.candidates) if merged else 0
        merged = _merge_coarse(merged, new_coarse) if merged else new_coarse
        gained = len(merged.candidates) - pool_before
        if span is not None:
            span.set_attr("gained_candidates", gained).set_attr(
                "productive", 1 if gained > 0 else 0)
        if intent != "anchor_search" and len(new_coarse.candidates) == 0:
            seq = recovery.note(ZERO_RESULT)
            recovery.select(ZERO_RESULT, causation=seq)
        planmod.mark_done(dag, node["node_id"], result_ref=f"coarse:{node['node_id']}")
        return merged, prior_art, patents, graph, last_fine, None

    if intent == "deep_read":
        if merged is None or not merged.candidates:
            planmod.mark_done(dag, node["node_id"])
            return merged, prior_art, patents, graph, last_fine, None
        year_from = int(case["query"]["dateFrom"][:4]) if case["query"].get("dateFrom") else None
        year_to = int(case["query"]["dateTo"][:4]) if case["query"].get("dateTo") else None
        filtered = [c for c in merged.candidates if _in_date_range(c, year_from, year_to)]
        if len(filtered) != len(merged.candidates):
            sink.verify(f"日期过滤 {len(merged.candidates) - len(filtered)} 条（{year_from}-{year_to}，"
                        f"免费 pub_year）→ 候选池 {len(filtered)} 条")
        ho = open_handoff(sink, from_team="supervisor", to_team="evidence_team",
                          goal=f"对 Top{budget} 专利族做全文精读与特征比对",
                          constraints=[f"付费 detail ≤ {budget} 次", "逐篇独立上下文",
                                       "越界动作回传 request"],
                          success_criteria=["每篇产出 claim chart 与 X/Y/A 建议"],
                          allowed_tools=skills.permit_tools(dag.get("skill_tools"),
                                                            ["aminer.detail"]),
                          budget={"paid_reads": budget, "tokens": 12000},
                          evidence_refs=[c.aminer_id for c in filtered[:budget * 2]],
                          return_schema="ChartsV1")
        with sink.span("tool", "evidence_team.read", budget=budget):
            ev = run_evidence_team(ho, case, filtered, router=router, min_score=min_score,
                                   llm=llm, case_id=case_id, sink=sink,
                                   reader_llm=reader_llm)
        return_handoff(sink, ho, status="partial" if ev.get("requests") else "ok",
                       outputs_ref="charts:deep_read",
                       tokens=dict(llm.usage), paid_used=ev.get("paid_used_this_run", 0),
                       requests=ev.get("requests", []))
        prior_art = ev.get("prior_art", [])
        patents = ev.get("patents", [])
        if ev.get("fine"):
            last_fine = ev["fine"]
        # score 统一归一化：命中子查询数 / 有效子查询总数
        active = max(merged.active_subqueries, 1)
        for p in patents:
            p["_ui_score"] = round(p["coarse_score"] / active, 2)
        for a in prior_art:
            pid_match = next((p for p in patents if _pub_of(p) == a.get("pubNo")), None)
            if pid_match:
                a["score"] = pid_match["_ui_score"]
        planmod.mark_done(dag, node["node_id"], result_ref="charts:deep_read")
        if span is not None:
            span.set_attr("charts", len(prior_art)).set_attr(
                "productive", 1 if prior_art else 0)
        sink.emit(f"引用完整性校验：{_cite_stats(prior_art)}", tag="guard.cite",
                  etype="guard.verdict", etype_ui="verify", actor="supervisor")
        return merged, prior_art, patents, graph, last_fine, None

    if intent in ("critique", "recritique"):
        rnd = int(node.get("args", {}).get("_round", critique_round))
        graph, stop = _run_critique(node, dag, case, prior_art, patents, graph,
                                    rnd, sink, llm, used_angle_keys,
                                    consensus=consensus, span=span)
        return merged, prior_art, patents, graph, last_fine, stop

    if intent in ("targeted_read", "targeted_search"):
        # Repair 节点：复用对应团队执行；完成后插入 recritique 复验
        if intent == "targeted_search":
            ho = open_handoff(sink, from_team="supervisor", to_team="search_team",
                              goal=f"Critic 定向补检：{node_args_angle(node)}",
                              constraints=["单角度+锚点", "免费"],
                              success_criteria=["返回候选并池"],
                              allowed_tools=skills.permit_tools(dag.get("skill_tools"),
                                                                ["aminer.search"]),
                              budget={"paid_reads": 0},
                              return_schema="CoarseResultV1")
            out = run_search_team(ho, node["args"].get("searches", []),
                                  router=router, size=size, sink=sink)
            return_handoff(sink, ho, status="ok", outputs_ref=f"coarse:{node['node_id']}")
            pool_before = len(merged.candidates) if merged else 0
            merged = _merge_coarse(merged, out["coarse"])
            if span is not None:
                gained = len(merged.candidates) - pool_before
                span.set_attr("gained_candidates", gained).set_attr(
                    "productive", 1 if gained > 0 else 0)
        else:
            # targeted_read：对已精检专利做补读——复用内存全文（不再付费 detail），
            # 仅重跑 flash reader（更全段落窗口），按 pubNo 精准定位
            ids = (node.get("args") or {}).get("patent_ids", [])
            ho = open_handoff(sink, from_team="supervisor", to_team="evidence_team",
                              goal=f"Critic 定向补读 {len(ids)} 篇（复用已拉全文，零付费）",
                              constraints=["只重读已有 patents，不新增 detail"],
                              success_criteria=["补齐证据缺口"],
                              allowed_tools=skills.permit_tools(dag.get("skill_tools"),
                                                                ["local.reread"]),
                              budget={"paid_reads": 0, "tokens": 12000},
                              return_schema="ChartsV1")
            target_patents = [p for p in patents if _pub_of(p) in ids] or patents
            try:
                new_art = _reread_patents(target_patents, case, reader_llm or llm, sink)
            except LLMError:
                # 补读是纯本地 LLM 动作：失败不重试为工具错误，直接跳过该 repair
                sink.verify(f"Critic 补读 LLM 暂不可用 → 跳过该 repair（不影响主结论）")
                new_art = []
            return_handoff(sink, ho, status="ok" if new_art else "partial",
                           outputs_ref="charts:targeted", paid_used=0)
            # 只覆盖成功补读的 pubNo；补读失败的保留原 chart（不丢失结论）
            old_scores = {a.get("pubNo"): a.get("score") for a in prior_art}
            for new_a in new_art:
                if new_a.get("grade") or any(
                        f.get("evidence") for f in new_a.get("features", [])):
                    new_a["score"] = old_scores.get(new_a.get("pubNo"), new_a.get("score", 0.5))
                    prior_art = [a for a in prior_art if a.get("pubNo") != new_a.get("pubNo")] + [new_a]
        # repair 完成 → 插入复验节点（submit 前）
        _insert_recritique(dag, sink, node)
        planmod.mark_done(dag, node["node_id"])
        if span is not None:
            if intent == "targeted_search":
                span.set_attr("productive", span.attrs.get("productive", 0))
            else:
                span.set_attr("productive", 1 if new_art else 0)
        return merged, prior_art, patents, graph, last_fine, None

    if intent == "submit":
        planmod.mark_done(dag, node["node_id"])
        if span is not None:
            span.set_attr("productive", 1)
        return merged, prior_art, patents, graph, last_fine, "submitted"

    raise ValueError(f"未知节点 intent: {intent}")


def _pub_of(patent: dict) -> str:
    pn = patent.get("pub_num") or ""
    return f"{(patent.get('country') or 'cn').upper()}{pn}{patent.get('pub_kind') or ''}"


def _reread_patents(target_patents: list[dict], case: dict, llm, sink) -> list[dict]:
    """Critic 定向补读：对内存中已有全文的专利重跑 reader（放宽到 12k 字窗口，零付费）。"""
    from ..services.comparison_service import _features_from_disclosure
    features = _features_from_disclosure(case.get("disclosure", {}))
    # 临时给更宽窗口：直接调 reader_agent.read_one 的 max_chars
    from ..agents import reader_agent
    results = []

    def _run(patent):
        try:
            return reader_agent.read_one(patent, features, case.get("title", ""), llm,
                                         max_chars=12000), True
        except Exception:
            return None, False

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        for fut in as_completed([pool.submit(_run, p) for p in target_patents]):
            art, ok = fut.result()
            if art:
                results.append(art)
                sink.sub_done(f"{art['pubNo']} 补读完成 → 证据已更新")
    return results


def _run_critique(node, dag, case, prior_art, patents, graph, critique_round,
                  sink, llm, used_angle_keys, *, consensus: int = 1, span=None) -> tuple:
    """Critique→Repair 决策。返回 (graph, stop_reason)；repair 时往 DAG 插入节点。"""
    features = _features_from_disclosure(case.get("disclosure", {}))
    patents_by_pub = {_pub_of(p): p for p in patents}
    graph = EvidenceGraph.from_charts(features, prior_art, patents_by_pub)

    rule_conflicts = graph.grade_conflicts()
    votes = []
    voters = max(1, int(consensus))
    for vi in range(voters):
        with sink.span("llm", "critic.review", model="flash",
                       round=critique_round + 1, vote=vi + 1, voters=voters):
            votes.append(teams_review.critique(features, graph, patents_by_pub, llm,
                                               rule_findings=rule_conflicts))
    crit = teams_review.merge_votes(votes, voters)
    if voters > 1:
        vs = crit.get("_vote_stats", {})
        sink.verify(f"E7 共识投票 {voters} 票：冲突保留 {vs.get('conflicts_kept')}/"
                    f"否决 {vs.get('conflicts_rejected')}，缺口保留 {vs.get('gaps_kept')}/"
                    f"否决 {vs.get('gaps_rejected')}（多数阈 {vs.get('quorum')}）")

    sink.emit(f"Critic 质控（第 {critique_round + 1} 轮"
              f"{'，' + str(voters) + ' 票共识' if voters > 1 else ''}）：{crit['summary']}；"
              f"缺口 {len(crit['gaps'])} / 冲突 {len(crit['conflicts'])}",
              tag="critic.issued", etype="critic.issued", etype_ui="sub",
              actor="review_team", payload={"critique": crit, "voters": voters})

    # 分级仲裁（确定性保守降级），回写 chart
    adjusted = teams_review.apply_grade_arbitration(graph, crit)
    for adj in adjusted:
        for a in prior_art:
            if a.get("pubNo") == adj["pub_no"]:
                a["grade"] = adj["to"]
                a.setdefault("gradeHistory", []).append(
                    {"from": adj["from"], "to": adj["to"],
                     "by": f"critic{'×'+str(voters) if voters>1 else ''}",
                     "reason": adj["reason"]})
        sink.verify(f"分级仲裁：{adj['pub_no']} {adj['from']}→{adj['to']}（{adj['reason'][:40]}）")

    planmod.mark_done(dag, node["node_id"], result_ref=f"critique:r{critique_round}")
    if span is not None:
        span.set_attr("voters", voters).set_attr(
            "arbitrations", len(adjusted)).set_attr(
            "productive", 1 if (adjusted or not crit["passed"]) else 0)

    # 无缺口/冲突 → 放行 submit
    if crit["passed"]:
        sink.sub_done("Critic 校验通过：证据充分、分级自洽 → 提交报告")
        return graph, None

    # 超过 repair 轮次：G8 保守收尾（结论降级措辞 + 标注人工复核），仍允许 submit
    if critique_round >= teams_review.MAX_ROUND:
        sink.verify(f"Critic 修复已达 {teams_review.MAX_ROUND} 轮上限 → 保守提交，"
                    f"残留缺口 {len(crit['gaps'])} 项在报告中标注人工复核")
        for a in prior_art:
            a["conclusion_note"] = "部分证据经 Critic 修复后仍不充分，结论需代理师复核"
        return graph, None

    # Repair：把 critique 建议动作插成 DAG 节点（submit 之前执行）
    repairs = teams_review.repair_actions(crit)
    inserted = 0
    submit_nodes = [n for n in dag["nodes"] if n["intent"] == "submit"
                    and n["status"] == "pending"]
    for rep in repairs[:3]:
        intent = rep["intent"]
        if intent not in ("targeted_read", "targeted_search"):
            continue
        nid = f"r{critique_round+1}_{inserted+1}"
        dag["nodes"].append({
            "node_id": nid, "intent": intent, "status": "ready",
            "depends_on": [node["node_id"]], "owner_team":
                "evidence" if intent == "targeted_read" else "search",
            "args": rep["args"], "result_ref": None,
            "origin": "inserted_by_critic", "origin_ref": node["node_id"],
            "attempts": 0})
        inserted += 1
    if inserted:
        _insert_recritique_after(dag, sink, node["node_id"], critique_round + 1)
        # 让 submit 依赖新的复验节点（ready_nodes 会按依赖自动排序）
        sink.verify(f"Critic 插入 {inserted} 个 repair 节点（{[r['intent'] for r in repairs[:inserted]]}），"
                    f"修复后进入第 {critique_round + 2} 轮复验")
    return graph, None


def _insert_recritique(dag, sink, repair_node):
    """单个 repair 节点完成后插复验（兼容入口）。"""
    _insert_recritique_after(dag, sink, repair_node["node_id"],
                             (repair_node.get("args") or {}).get("_round", 1))


def _insert_recritique_after(dag, sink, after_node_id: str, round_no: int) -> None:
    """在 after_node 之后插入 recritique，并让 submit 依赖它。"""
    nid = f"critic_r{round_no}"
    if any(n["node_id"] == nid for n in dag["nodes"]):
        return
    dag["nodes"].append({
        "node_id": nid, "intent": "recritique", "status": "pending",
        "depends_on": [after_node_id], "owner_team": "review",
        "args": {"_round": round_no}, "result_ref": None,
        "origin": "inserted_by_critic", "origin_ref": after_node_id, "attempts": 0})
    # submit 改依赖最新 critic
    for n in dag["nodes"]:
        if n["intent"] == "submit" and n["status"] == "pending":
            n["depends_on"] = [nid]


def node_args_angle(node) -> str:
    searches = (node.get("args") or {}).get("searches") or []
    if searches:
        return searches[0].get("angle", "补检")
    return "补检"


def _cite_stats(prior_art):
    need = have = 0
    for a in prior_art:
        for f in a.get("features", []):
            if f.get("disclosed") in ("是", "部分"):
                need += 1
                if f.get("evidence"):
                    have += 1
    if need == 0:
        return "无需挂证的公开特征（全部未公开）"
    return f"{have}/{need} 条公开结论已挂（专利+§章节¶序号）" + (" → 通过" if have == need else "")
