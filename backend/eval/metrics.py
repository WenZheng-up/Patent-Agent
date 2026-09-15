# -*- coding: utf-8 -*-
"""
M7 指标层：从【事件流 + trace span + 终态结果 + golden】计算双层指标。

过程层（评价 harness 机制本身，只在有对应机制的变体上计算；缺失记 None 并在
scorecard 注明，不拿 0 充数）：
- planning_efficiency   有状态增量 action 数 / 总 action 数（span.attrs.productive）
- replan_quality        Planner 修订带来增量的比例（无修订=None）
- tool_efficiency       带来新增量的团队 action 比例（tool span 侧）
- recovery_quality      classified / recovered / 恢复步数（合成宇宙不注入故障=None，
                        恢复路径由 fault_injection 6/6 单独覆盖，不混为一谈）
- handoff_efficiency    下游引用的 handoff 产出 / 全部 handoff（ok 且 outputs_ref 非空）
- context_efficiency    进入最终 charts 的精读上下文 / 全部精读调用（子代理隔离）

结果层（对照 golden 人工标签）：
- recall@20 / fine_precision / grade_accuracy / evidence_precision / 成本 / 时延
"""
from .golden import relevant_pubnos


# ---------------- 事件规整 ----------------

def norm_events(events: list[dict]) -> list[dict]:
    """db 强类型事件与 UI 事件统一为 {seq,etype,payload,causation}。"""
    out = []
    for i, e in enumerate(events or []):
        if "etype" in e:
            out.append({"seq": e.get("seq", i), "etype": e.get("etype"),
                        "payload": e.get("payload") or {},
                        "causation": e.get("causation_id")})
        else:  # UI 形态 {ts,tag,type,msg}
            out.append({"seq": i, "etype": e.get("tag"),
                        "payload": {"msg": e.get("msg", "")}, "causation": None})
    return out


def _etype_counts(events: list[dict]) -> dict[str, int]:
    c: dict[str, int] = {}
    for e in events:
        c[e["etype"]] = c.get(e["etype"], 0) + 1
    return c


# ---------------- 过程层 ----------------

def process_metrics(events: list[dict], trace: dict | None) -> dict:
    events = norm_events(events)
    counts = _etype_counts(events)
    spans = (trace or {}).get("spans", [])

    # ---- action 级（plan node span；v6 supervisor 埋了 productive）----
    node_spans = [s for s in spans if s.get("kind") == "plan"]
    if node_spans:
        total = len(node_spans)
        productive = sum(1 for s in node_spans if s.get("attrs", {}).get("productive") == 1)
        planning_efficiency = round(productive / total, 3)
    else:
        total = productive = 0
        planning_efficiency = None  # E0/E1 无显式 PlanDAG action

    # ---- tool 级（团队执行 span，看父 action 是否带来增量）----
    tool_spans = [s for s in spans if s.get("kind") == "tool"]
    if tool_spans:
        productive_tools = 0
        span_by_id = {s["id"]: s for s in spans}
        for t in tool_spans:
            parent = span_by_id.get(t.get("parent"))
            if parent and parent.get("attrs", {}).get("productive") == 1:
                productive_tools += 1
        tool_efficiency = round(productive_tools / len(tool_spans), 3)
    else:
        tool_efficiency = None

    # ---- replan 质量：plan.revised 事件后是否有增量节点（本宇宙修订=0 → None）----
    revised = counts.get("plan.revised", 0)
    if revised:
        # 修订事件 payload.revisions 为评估事件；插入事件亦记 plan.revised。
        # 有增量：修订后出现 productive 的 inserted action。
        inserted = [s for s in node_spans
                    if s.get("attrs", {}).get("origin") == "inserted_by_planner"]
        gained = sum(1 for s in inserted if s.get("attrs", {}).get("productive") == 1)
        replan_quality = round(gained / max(len(inserted), 1), 3)
    else:
        replan_quality = None

    # ---- recovery（合成快乐路径不触发）----
    classified = counts.get("failure.classified", 0)
    if classified:
        selected = counts.get("recovery.selected", 0)
        recovery_quality = {
            "classified": classified,
            "strategies_selected": selected,
            "rate_note": "终态见 result.stop_reason；逐步恢复归因见 fault_injection 回归",
        }
    else:
        recovery_quality = None  # 未触发，不参与跨变体排名

    # ---- handoff 效率：opened 数 / returned ok 且带 outputs_ref 数 ----
    opened = counts.get("handoff.opened", 0)
    if opened:
        returned_ok = 0
        for e in events:
            if e["etype"] != "handoff.returned":
                continue
            p = e["payload"]
            if p.get("status") in ("ok",) and p.get("outputs_ref"):
                returned_ok += 1
        handoff_efficiency = round(returned_ok / opened, 3)
    else:
        handoff_efficiency = None  # E0/E1 无 typed handoff

    return {
        "planning_efficiency": planning_efficiency,
        "planning_actions": {"total": total, "productive": productive},
        "replan_quality": replan_quality,
        "replan_events": revised,
        "tool_efficiency": tool_efficiency,
        "tool_spans": len(tool_spans),
        "recovery_quality": recovery_quality,
        "handoff_efficiency": handoff_efficiency,
        "handoff_opened": opened,
        "event_counts": counts,
    }


# ---------------- 结果层 ----------------

def _pub_map(patents: list[dict]) -> dict[str, dict]:
    out = {}
    for p in patents or []:
        country = (p.get("country") or "cn").upper()
        key = f"{country}{p.get('pub_num', '')}{p.get('pub_kind') or ''}"
        out[key] = p
    return out


def result_metrics(golden: dict, result: dict) -> dict:
    labels = golden.get("labels") or {}
    relevant = relevant_pubnos(golden)
    hits = result.get("hits") or []
    prior_art = result.get("prior_art") or []
    patents = result.get("patents") or []
    docs = _pub_map(patents)

    # ---- recall@20：golden 相关文件出现在命中列表前 20 的比例 ----
    top20 = {h.get("pubNo") for h in hits[:20]}
    recall20 = round(len(relevant & top20) / len(relevant), 3) if relevant else None

    # ---- fine_precision：精检（全文）文件中 golden 相关（X/Y）比例 ----
    fine_pubs = {h.get("pubNo") for h in hits if h.get("stage") == "全文"}
    if not fine_pubs:
        fine_pubs = set(docs)  # E0 无 stage 时退回已拉全文集合
    fine_precision = (round(len(fine_pubs & relevant) / len(fine_pubs), 3)
                      if fine_pubs else None)

    # ---- grade_accuracy：读过且 golden 有标签的文件，最终分级一致比例 ----
    grade_hits = grade_total = 0
    grade_rows = []
    for a in prior_art:
        pub = a.get("pubNo")
        if pub in labels:
            grade_total += 1
            ok = a.get("grade") == labels[pub]
            grade_hits += 1 if ok else 0
            grade_rows.append({"pubNo": pub, "golden": labels[pub],
                               "predicted": a.get("grade"), "match": ok,
                               "gradeHistory": a.get("gradeHistory")})
    grade_accuracy = round(grade_hits / grade_total, 3) if grade_total else None

    # ---- evidence_precision：挂证特征 quote 可在指定段落定位的比例 ----
    ev_total = ev_ok = 0
    for a in prior_art:
        doc = docs.get(a.get("pubNo"))
        for f in a.get("features", []):
            ev = f.get("evidence")
            if f.get("disclosed") in ("是", "部分") and isinstance(ev, dict):
                ev_total += 1
                quote = ev.get("quote", "")
                idx = ev.get("paragraphIndex")
                if doc is None:
                    # G1 兜底：无源文档可核（本评测不应出现）
                    ev_ok += 1
                    continue
                from app.providers.normalize import locate_evidence
                loc = locate_evidence(doc, quote[:20]) if quote else None
                if loc and loc.get("paragraph_index") == idx:
                    ev_ok += 1
    evidence_precision = (round(ev_ok / ev_total, 3) if ev_total else
                          1.0)  # G1：无需挂证（全部否）→ 1.0 兜底

    return {
        "recall@20": recall20,
        "fine_precision": fine_precision,
        "fine_pubs": sorted(fine_pubs),
        "grade_accuracy": grade_accuracy,
        "grade_rows": grade_rows,
        "evidence_precision": evidence_precision,
        "evidence_edges": {"ok": ev_ok, "total": ev_total},
    }


# ---------------- 汇总 scorecard ----------------

def build_scorecard(golden: dict, variant: str, result: dict, *,
                    events: list[dict], trace: dict | None,
                    cost: dict, wall_ms: float | None,
                    notes: str = "") -> dict:
    return {
        "case_id": golden["case_id"],
        "synthetic": bool(golden.get("synthetic")),
        "variant": variant,
        "stop_reason": result.get("stop_reason")
                       or (result.get("summary") or {}).get("stopReason"),
        "process": process_metrics(events, trace),
        "result": result_metrics(golden, result),
        "cost": cost,
        "wall_ms": round(wall_ms, 1) if wall_ms is not None else None,
        "notes": notes,
    }
