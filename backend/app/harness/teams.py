# -*- coding: utf-8 -*-
"""
M2 两个执行团队：
- SearchTeam：handoff 接收 CNF 组 → SearchWorker 按角度并发 fanout（Tool Router 统一执行）
- EvidenceTeam：handoff 接收候选 → 族感知付费精检（幂等）→ Reader worker（flash 独立窗口）出 charts

复用：retrieval.pipeline（coarse_search/fine_retrieve）、agents.reader_agent、normalize。
worker 只在契约内自救；越界请求放 requests 回 Supervisor。
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .. import db
from ..agents import reader_agent
from ..llm.client import LLMClient
from ..providers import normalize
from ..retrieval.pipeline import coarse_search, fine_retrieve
from ..services.comparison_service import _features_from_disclosure
from ..services.retrieval_service import _now_hms, translate_event
from .handoff import enforce_tools
from .router import ToolRouter


# ---------------- Search Team ----------------

def run_search_team(ho: dict, groups: list[list[str]], *, router: ToolRouter,
                    size: int, sink) -> dict:
    """执行一次检索 handoff。返回 {candidates: CoarseCandidate[], coarse: CoarseResult}。"""
    enforce_tools(ho, "aminer.search")
    client = router.client

    def pipe_cb(ev):
        ui = translate_event(ev)
        if ui:
            sink.emit(ui["msg"], tag=ui["tag"], etype="runtime.note",
                      etype_ui=ui["type"], actor="search_team")

    # 直接用绑定的 client（client 即 AminerClient 实例）
    coarse = coarse_search(client, groups, size=size, progress_cb=pipe_cb)
    return {"coarse": coarse, "candidates": coarse.candidates}


# ---------------- Evidence Team ----------------

def _idem_key(case_id: str, family_keys: list[str]) -> str:
    raw = case_id + "|" + ",".join(sorted(family_keys))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def run_evidence_team(ho: dict, case: dict, candidates: list, *,
                      router: ToolRouter, min_score: int, llm: LLMClient,
                      case_id: str, sink, allow_db_idem: bool = True,
                      reader_llm: LLMClient | None = None) -> dict:
    """
    精检 + 并行 reader。返回 {prior_art, patents, paid_used_this_run, reused_idem, requests}。
    付费 detail 走 read_idem 幂等：resume 时已读族不重复扣费。
    reader_llm 用于 E8 模型路由（reader 独立模型）；默认与规划 llm 相同。
    """
    reader_llm = reader_llm or llm
    enforce_tools(ho, "aminer.detail")
    client = router.client
    budget = ho["budget"].get("paid_reads", 3)

    # 幂等检查：同案件 + 同预算 + 同 Top 候选集合才视为同一批 read（resume 场景）
    top_ids = sorted({c.aminer_id for c in candidates[: max(budget * 3, 6)]})
    idem = db.get_read_idem(case_id, _idem_key(case_id, top_ids + [f"budget={budget}"])) \
        if allow_db_idem else None

    if idem:
        sink.sub_done(f"检测到已完成的精检记录（{idem['paid_calls']} 次付费），resume 直接复用，不重复扣费")
        stored = db.get_retrieval(case_id) or {}
        return {"resumed": True, "paid_used_this_run": 0,
                "prior_art": stored.get("priorArt", []),
                "patents": stored.get("patents", []),
                "fine": None, "idem_patents": stored.get("patents", [])}

    # 同一 run 进程内的内存复用（targeted_read 再进来时避免重复付费/重复精读）
    cache = getattr(run_evidence_team, "_cache", {})
    ck = (case_id, _idem_key(case_id, top_ids + [f"budget={budget}"]))
    if ck in cache:
        cached = cache[ck]
        sink.sub_done("同 run 内复用已精检专利全文（不重复付费）")
        return {"resumed": True, "paid_used_this_run": 0,
                "prior_art": cached["prior_art"], "patents": cached["patents"],
                "fine": cached.get("fine")}

    fine = fine_retrieve(client, candidates, budget=budget, min_score=min_score,
                         progress_cb=_fine_cb(sink))
    patents = [p for p in fine.patents if p.get("paragraphs") or p.get("abstract")]
    # _ui_score 由 Supervisor 按 merged.active_subqueries 统一归一化

    features = _features_from_disclosure(case.get("disclosure", {}))
    sink.sub(f"派生 {len(patents)} 个隔离子代理：逐篇全文精读（flash 独立上下文，并行）")

    prior_art, requests = _parallel_readers(patents, features, case, reader_llm, sink)

    # 幂等落库（patents 全文较大，存进 retrieval 终态由 Supervisor 统一写；idem 只存引用键+计数）
    db.put_read_idem(case_id, _idem_key(case_id, top_ids + [f"budget={budget}"]),
                     [{"pubNo": a.get("pubNo")} for a in prior_art],
                     fine.paid_detail_calls, datetime.now().isoformat(timespec="seconds"))

    result = {"resumed": False, "prior_art": prior_art, "patents": patents,
              "paid_used_this_run": fine.paid_detail_calls,
              "fine": fine, "requests": requests}
    if not hasattr(run_evidence_team, "_cache"):
        run_evidence_team._cache = {}
    run_evidence_team._cache[ck] = result
    return result


def _fine_cb(sink):
    from ..services.retrieval_service import translate_event

    def cb(ev):
        ui = translate_event(ev)
        if ui:
            sink.emit(ui["msg"], tag=ui["tag"], etype="runtime.note",
                      etype_ui=ui["type"], actor="evidence_team")
    return cb


def _parallel_readers(patents, features, case, llm, sink):
    """每篇专利一个独立 flash 窗口；失败→4.5k 重试→骨架降级；越界请求收集。"""
    results, requests = [], []

    def _run(patent):
        try:
            return reader_agent.read_one(patent, features, case.get("title", ""), llm), "llm", None
        except Exception:
            try:
                return reader_agent.read_one(patent, features, case.get("title", ""), llm,
                                             max_chars=4500), "llm_retry", None
            except Exception as e:
                from ..services import comparison_service
                skel = comparison_service.build_comparison(
                    {"disclosure": {"solution": "；".join(features), "terms": features}},
                    [patent])
                return (skel[0] if skel else None), "degraded", str(e)[:100]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(_run, p): p for p in patents}
        done_n = 0
        for fut in as_completed(futs):
            art, mode, err = fut.result()
            done_n += 1
            if art:
                results.append(art)
                note = {"llm": "特征比对表 + X/Y/A 建议已回传",
                        "llm_retry": "精简上下文重试成功",
                        "degraded": "LLM 不可用，降级关键词骨架"}[mode]
                sink.sub_done(f"{art['pubNo']} 精读完成 {done_n}/{len(patents)} → {note}")
            if err:
                requests.append({"type": "reader_failed", "error": err})

    order = {"X": 0, "Y": 1, "A": 2}
    results.sort(key=lambda a: (order.get(a.get("grade"), 3), -float(a.get("score") or 0)))
    return results, requests
