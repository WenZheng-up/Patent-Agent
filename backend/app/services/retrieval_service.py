# -*- coding: utf-8 -*-
"""
检索编排服务：把管道事件翻译为 UI 事件、执行日期过滤（免费 pub_year）、
把族合并专利 + 摘要档候选构建为前端 Hit 列表、汇总 summary。
字段映射规则严格对照 API_CONTRACT.md §6.1。
"""
from datetime import datetime

from ..providers.aminer_client import AminerClient
from ..retrieval.pipeline import coarse_search, fine_retrieve


# ---------- 事件翻译（管道事件 -> 前端 Event） ----------
def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def translate_event(ev: dict) -> dict | None:
    """返回 {ts, tag, type, msg}；subquery_start 不向前端转发。"""
    t = ev.get("event")
    if t == "subquery_done":
        return {"ts": _now_hms(), "tag": "tool.search", "type": "tool",
                "msg": f"粗检子查询 {ev['index'] + 1}/{ev['total']}：{ev['query']} "
                       f"→ {ev['returned']} 条 · 候选池 {ev['pool_size']}"}
    if t == "coarse_done":
        return {"ts": _now_hms(), "tag": "loop.verify", "type": "verify",
                "msg": f"粗检完成：并集 {ev['total']} 条，"
                       f"{ev['multi_hit']} 条命中 ≥2 个子查询"}
    if t == "fine_start":
        return {"ts": _now_hms(), "tag": "tool.detail", "type": "tool",
                "msg": f"预算闸门：最多精检 {ev['budget']} 个专利族 · 免费号单建族中"}
    if t == "families_built":
        return {"ts": _now_hms(), "tag": "tool.detail", "type": "tool",
                "msg": f"号单建族完成：{ev['families']} 个族（A/B 版本已合并）"}
    if t == "family_done":
        return {"ts": _now_hms(), "tag": "tool.detail", "type": "tool",
                "msg": f"精检 {ev['index']}/{ev['total']} 族完成（族内 {ev['members']} "
                       f"版本）· 累计付费 {ev['paid_calls']} 次"}
    if t in ("detail_error", "info_error"):
        return {"ts": _now_hms(), "tag": "guard.retry", "type": "verify",
                "msg": f"版本 {ev.get('aminer_id', '')[:10]} 无全文/调用失败，跳过"}
    if t == "fine_done":
        return {"ts": _now_hms(), "tag": "loop.verify", "type": "verify",
                "msg": f"精检完成：{ev['patents']} 篇全文就绪 · "
                       f"付费 detail 共 {ev['paid_calls']} 次"}
    return None


# ---------- 日期过滤（search 免费返回 pub_year） ----------
def _in_date_range(cand, year_from: int | None, year_to: int | None) -> bool:
    y = cand.pub_year
    if not y:
        return True  # 缺年份不过滤（保守保留）
    try:
        y = int(y)
    except ValueError:
        return True
    if year_from and y < year_from:
        return False
    if year_to and y > year_to:
        return False
    return True


def _ipc_match(patent_ipcs: list[str], query_ipcs: list[str]) -> bool:
    """软标记：主类（/ 前，去空格）前缀相交即视为命中。"""
    def head(ipc: str) -> str:
        return ipc.replace(" ", "").split("/")[0]
    q = {head(x) for x in query_ipcs}
    return any(head(p) in q for p in patent_ipcs)


def _pub_no(country, pub_num, kind) -> str:
    if not pub_num:
        return ""
    return f"{(country or 'cn').upper()}{pub_num}{kind or ''}"


def _assignee(assignees: list[str]) -> str:
    if not assignees:
        return ""
    return assignees[0] + ("等" if len(assignees) > 1 else "")


def _hit_from_patent(patent: dict, active: int, query_ipcs: list[str]) -> dict:
    score = round(patent["coarse_score"] / active, 2) if active else 0
    return {
        "pubNo": _pub_no(patent.get("country"), patent.get("pub_num"),
                         patent.get("pub_kind")),
        "title": patent.get("title_zh") or "",
        "assignee": _assignee(patent.get("assignees", [])),
        "date": patent.get("pub_date") or "",
        "score": score,
        "stage": "全文",
        "coarseScore": patent["coarse_score"],
        "subqueryTotal": active,
        "kinds": patent.get("kinds", []),
        "ipcs": patent.get("ipcs", []),
        "ipcMatch": _ipc_match(patent.get("ipcs", []), query_ipcs),
        "dataCompleteness": {
            "hasAbstract": patent["data_completeness"]["has_abstract"],
            "descriptionParagraphs":
                patent["data_completeness"]["description_paragraphs"],
            "hasClaims": patent["data_completeness"]["has_claims"],
            "ipcCount": patent["data_completeness"]["ipc_count"],
        },
        "aminerId": (patent.get("aminer_ids") or [patent.get("aminer_id")])[0],
        "_aminer_ids": patent.get("aminer_ids", []),
    }


def _hit_from_coarse(cand, info: dict | None, active: int) -> dict:
    score = round(cand.score / active, 2) if active else 0
    return {
        "pubNo": _pub_no((info or {}).get("country"), (info or {}).get("pub_num"),
                         (info or {}).get("pub_kind")),
        "title": cand.title_zh or "",
        "assignee": "",  # info 端点无申请人字段
        "date": cand.pub_year or "",
        "score": score,
        "stage": "摘要",
        "coarseScore": cand.score,
        "subqueryTotal": active,
        "kinds": [info["pub_kind"]] if info and info.get("pub_kind") else [],
        "ipcs": [],
        "ipcMatch": False,
        "dataCompleteness": None,
        "aminerId": cand.aminer_id,
        "_aminer_ids": [cand.aminer_id],
    }


def build_hits(coarse, filtered_candidates, fine, query_ipcs, cap=50) -> list[dict]:
    """全文档（精检专利）在前，摘要档（仅粗检）在后，合计上限 cap。"""
    active = max(coarse.active_subqueries, 1)
    hits = [_hit_from_patent(p, active, query_ipcs) for p in fine.patents
            if p.get("paragraphs") or p.get("abstract")]
    full_ids = {mid for h in hits for mid in h["_aminer_ids"]}
    for cand in filtered_candidates:
        if len(hits) >= cap:
            break
        if cand.aminer_id in full_ids:
            continue
        hits.append(_hit_from_coarse(cand, fine.infos_by_id.get(cand.aminer_id), active))
    return hits


def run_retrieval(case: dict, *, budget: int, min_score: int, size: int,
                  progress_cb=None, client=None) -> dict:
    """
    执行完整两阶段检索。progress_cb 接收【前端形态】事件 {ts,tag,type,msg}。
    返回 {summary, hits, events}。异常向上抛（由路由层映射错误）。
    client 可注入（M7 消融 E0 的录制/回放包装）；默认真实 AminerClient。
    """
    query = case["query"]
    groups = query.get("groups")
    if not groups:
        from ..retrieval.query_compiler import parse_boolean
        groups = parse_boolean(query["expr"])

    ui_events: list[dict] = []

    def pipe_cb(ev: dict):
        ui = translate_event(ev)
        if ui:
            ui_events.append(ui)
            if progress_cb:
                progress_cb(ui)

    # HITL 恢复事件
    resume = {"ts": _now_hms(), "tag": "hitl.resume", "type": "hitl",
              "msg": f"代理人确认检索式 → 从 checkpoint 恢复执行（预算 {budget} 篇）"}
    ui_events.append(resume)
    if progress_cb:
        progress_cb(resume)

    client = client or AminerClient()
    coarse = coarse_search(client, groups, size=size, progress_cb=pipe_cb)

    year_from = int(query["dateFrom"][:4]) if query.get("dateFrom") else None
    year_to = int(query["dateTo"][:4]) if query.get("dateTo") else None
    before = len(coarse.candidates)
    filtered = [c for c in coarse.candidates
                if _in_date_range(c, year_from, year_to)]
    date_filtered = before - len(filtered)

    fine = fine_retrieve(client, filtered, budget=budget, min_score=min_score,
                         progress_cb=pipe_cb)

    hits = build_hits(coarse, filtered, fine, query.get("ipc", []))
    fine_patent_count = sum(1 for p in fine.patents
                            if p.get("paragraphs") or p.get("abstract"))
    summary = {
        "subqueryCount": len(coarse.subqueries),
        "coarseTotal": len(filtered),
        "coarseMultiHit": sum(1 for c in filtered if c.score >= 2),
        "families": fine.families_built,
        "finePatents": fine_patent_count,
        "paidDetailCalls": fine.paid_detail_calls,
        "dateFiltered": date_filtered,
        "truncated": coarse.meta.get("truncated", False),
    }
    return {"summary": summary, "hits": hits, "events": ui_events,
            "_patents": fine.patents}
