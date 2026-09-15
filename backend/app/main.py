# -*- coding: utf-8 -*-
"""
Patent-Agent FastAPI 主应用（按 API_CONTRACT.md v1 实现）。
运行：.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
"""
import json
import queue
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, seed, config
from .agents.disclosure_agent import parse_disclosure
from .agents.orchestrator import run_agent
from .harness.supervisor import run_supervisor, HarnessDegraded
from .harness import memory
from .llm.client import LLMClient, LLMError, is_configured as llm_configured
from .providers.aminer_client import AminerAuthError, AminerBalanceError, AminerError
from .retrieval.query_compiler import QueryCompileError, compile_groups, parse_boolean
from .services import case_service, comparison_service, report_service
from .services.retrieval_service import run_retrieval

app = FastAPI(title="Patent-Agent 后端", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 案件级运行锁（同案不并发检索）
_run_locks: dict[str, threading.Lock] = {}
_run_locks_guard = threading.Lock()

INTERNAL_HIT_FIELDS = {"_aminer_ids"}


# ---------- 信封 ----------
def ok(data):
    return {"code": 0, "msg": "ok", "data": data}


def err(code: int, msg: str):
    return {"code": code, "msg": msg, "data": None}


def _upstream_error_code(e: Exception) -> tuple[int, str]:
    if isinstance(e, config.CredentialMissing):
        return 5004, str(e)
    if isinstance(e, AminerAuthError):
        return 5001, f"数据源鉴权失败：{e}"
    if isinstance(e, AminerBalanceError):
        return 5002, f"数据源余额不足：{e}"
    if isinstance(e, AminerError):
        return 5003, f"数据源调用失败：{e}"
    return 5000, f"内部错误：{e}"


def _public_hits(hits: list[dict]) -> list[dict]:
    return [{k: v for k, v in h.items() if k not in INTERNAL_HIT_FIELDS} for h in hits]


def _list_item(case: dict) -> dict:
    events = case.get("eventsRun") or case.get("events") or []
    agent = events[-1]["msg"] if events else "—"
    return {
        "id": case["id"], "title": case["title"], "client": case["client"],
        "field": case.get("field", ""), "status": case["status"],
        "progress": case.get("progress", 0), "updated": case.get("updated", ""),
        "agent": agent[:48],
    }


def _public_case(case: dict) -> dict:
    out = dict(case)
    out["hits"] = _public_hits(case.get("hits", []))
    return out


def _get_lock(case_id: str) -> threading.Lock:
    with _run_locks_guard:
        return _run_locks.setdefault(case_id, threading.Lock())


# ---------- 请求模型 ----------
class CreateCaseReq(BaseModel):
    title: str
    client: str
    disclosureText: str = ""


class ConfirmQueryReq(BaseModel):
    query: str | dict
    edited: bool = False
    groups: list[list[str]] | None = None
    budget: int | None = None


class GradeCorrectionReq(BaseModel):
    pubNo: str
    fromGrade: str
    toGrade: str
    reason: str = ""


class ExperienceReviewReq(BaseModel):
    approve: bool


# ---------- 端点 1/2：案件列表 / 新建 ----------
@app.get("/api/cases")
def api_list_cases():
    return ok([_list_item(c) for c in db.list_cases()])


@app.post("/api/cases")
def api_create_case(req: CreateCaseReq):
    # LLM 解析交底书；未配置/调用失败时 parse_disclosure 内部降级规则 stub
    llm = None
    try:
        llm = LLMClient()
    except LLMError:
        llm = None
    disclosure, query_kwargs, used_llm = parse_disclosure(
        req.disclosureText, req.title, llm)
    case = case_service.assemble_case(
        req.title, req.client, req.disclosureText, disclosure, query_kwargs, used_llm)
    return ok({"id": case["id"], "title": case["title"],
               "client": case["client"], "status": case["status"]})


# ---------- 端点 3：案件详情 ----------
@app.get("/api/cases/{case_id}")
def api_get_case(case_id: str):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    return ok(_public_case(case))


# ---------- 端点 4：HITL 检索式审批 ----------
@app.post("/api/cases/{case_id}/query/confirm")
def api_confirm_query(case_id: str, req: ConfirmQueryReq):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")

    if isinstance(req.query, dict):
        expr = req.query.get("expr", "")
        groups = req.query.get("groups")
    else:
        expr, groups = req.query, None
    groups = req.groups or groups
    if not groups:
        groups = parse_boolean(expr)

    try:
        subqueries, meta = compile_groups(groups)
    except QueryCompileError as e:
        return err(4009, f"检索式编译失败：{e}")

    # M6：审批即 HITL 修正信号（只记录词项增删差，不含交底书原文）；
    # 经验记录失败不得阻断审批主流程
    try:
        before_groups = case.get("query", {}).get("groups") or []
        memory.record_query_correction(case, before_groups, meta["groups"])
    except Exception as e:
        print(f"[memory] query_edit 记录失败（已忽略）：{e}")

    case["query"]["expr"] = expr
    case["query"]["groups"] = meta["groups"]
    case["query"]["lint"] = (
        f"CNF 编译通过 · {meta['group_count']} 组 {meta['subquery_count']} 个子查询 "
        f"· 词袋 fan-out（粗检免费）"
        + (" · ⚠️ 同义词超量已截断" if meta["truncated"] else ""))
    if req.budget:
        case["budget"] = req.budget
    case["status"] = "检索中"
    case["progress"] = 45
    case["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.upsert_case(case)

    return ok({
        "resumedAt": datetime.now().isoformat(timespec="seconds"),
        "checkpointId": f"cp_{case_id.lower().replace('-', '')}_{datetime.now().strftime('%H%M%S')}",
        "compiled": {
            "subqueryCount": meta["subquery_count"],
            "groupCount": meta["group_count"],
            "truncated": meta["truncated"],
            "droppedTerms": meta["dropped_terms"],
        },
    })


# ---------- 端点 5：检索终态读取 ----------
@app.get("/api/cases/{case_id}/retrieval")
def api_get_retrieval(case_id: str):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    result = db.get_retrieval(case_id)
    if not result:
        return err(4090, "检索尚未执行，请先审批检索式")
    return ok({"summary": result["summary"], "hits": _public_hits(result["hits"]),
               "events": result["events"]})


# ---------- 端点 6：SSE 检索流 ----------
def _sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/cases/{case_id}/retrieval/stream")
def api_retrieval_stream(case_id: str, request: Request,
                         budget: int = 20, min_score: int = 2, size: int = 100):
    case = db.get_case(case_id)
    if not case:
        async def not_found():
            yield _sse_frame("error", err(4004, f"案件不存在：{case_id}"))
            yield _sse_frame("done", {"reason": "error"})
        return StreamingResponse(not_found(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    lock = _get_lock(case_id)

    def generator():
        if not lock.acquire(blocking=False):
            yield _sse_frame("error", err(4090, "该案件已有检索任务在执行中"))
            yield _sse_frame("done", {"reason": "error"})
            return

        q: queue.Queue = queue.Queue()
        SENTINEL = object()

        def worker():
            run_id = "rs_" + datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                effective_budget = case.get("budget") or budget

                def on_event(ui):
                    """v6 EventSink 已自行写库；这里只负责推 SSE 队列。"""
                    q.put(("progress", ui))

                def on_event_legacy(ui):
                    """v2/v1 管道无 EventSink：写 append-only 日志 + 推队列。"""
                    db.append_event(case_id, run_id, ui)
                    q.put(("progress", ui))

                # v6 ChaxinHarness（Plan DAG + Handoff + Router + Recovery）；
                # LLM 不可用/规划连续失败 → 降级 v2 Agent Loop → 再不行 v1 确定性管道
                try:
                    llm = LLMClient()
                except LLMError:
                    llm = None

                try:
                    if llm is not None:
                        result = run_supervisor(case, budget=effective_budget,
                                                min_score=min_score, size=size,
                                                llm=llm, progress_cb=on_event,
                                                run_id=run_id)
                    else:
                        raise HarnessDegraded("LLM 未配置")
                except HarnessDegraded as hd:
                    on_event({"ts": "", "tag": "guard.retry", "type": "verify",
                              "msg": f"Harness 智能规划不可用（{str(hd)[:50]}）→ 降级 v2 Agent 管道"})
                    try:
                        llm2 = llm or LLMClient()
                        result = run_agent(case, budget=effective_budget,
                                           min_score=min_score, size=size,
                                           max_iterations=2, llm=llm2,
                                           progress_cb=on_event_legacy)
                    except Exception:
                        r1 = run_retrieval(case, budget=effective_budget,
                                           min_score=min_score, size=size,
                                           progress_cb=on_event_legacy)
                        result = {**r1,
                                  "prior_art": comparison_service.build_comparison(
                                      case, r1["_patents"]),
                                  "patents": r1["_patents"]}
                prior_art = result["prior_art"]
                patents = result["patents"]
                run_events = result.get("events") or db.list_events(case_id, run_id)

                stored = {"summary": result["summary"],
                          "hits": result["hits"],
                          "events": run_events,
                          "priorArt": prior_art,
                          "patents": patents,
                          "trace": result.get("trace"),
                          "runId": run_id}
                db.save_retrieval(case_id, stored)

                fresh = db.get_case(case_id)
                fresh["hits"] = result["hits"]
                fresh["priorArt"] = prior_art
                fresh["retrievalSummary"] = result["summary"]
                fresh["eventsRun"] = run_events
                fresh["lastRunId"] = run_id
                fresh["status"] = "对比分析"
                fresh["progress"] = 78
                fresh["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                db.upsert_case(fresh)

                q.put(("result", {"summary": result["summary"],
                                  "hits": _public_hits(result["hits"]),
                                  "events": run_events}))
                q.put(("done", {"reason": "completed"}))
            except Exception as e:
                code, msg = _upstream_error_code(e)
                q.put(("error", err(code, msg)))
                q.put(("done", {"reason": "error"}))
            finally:
                q.put(SENTINEL)
                lock.release()

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = q.get()
            if item is SENTINEL:
                break
            yield _sse_frame(item[0], item[1])

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ---------- 端点 6b：append-only 事件日志回放（审计 / HITL 恢复重放） ----------
@app.get("/api/cases/{case_id}/events")
def api_case_events(case_id: str, run: str | None = None):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    return ok(db.list_events(case_id, run_id=run))


# ---------- 端点 6c：trace 树（L7 可观测） ----------
@app.get("/api/cases/{case_id}/trace")
def api_case_trace(case_id: str):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    stored = db.get_retrieval(case_id) or {}
    trace = stored.get("trace")
    if not trace:
        return err(4090, "该案件尚无 trace（未执行过 v6 run）")
    return ok(trace)


# ---------- 端点 6d：HITL 分级修正（M6 经验来源之二）----------
@app.post("/api/cases/{case_id}/corrections")
def api_record_correction(case_id: str, req: GradeCorrectionReq):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    fg, tg = req.fromGrade.strip().upper(), req.toGrade.strip().upper()
    if fg not in memory.VALID_GRADES or tg not in memory.VALID_GRADES:
        return err(4009, "分级只支持 X / Y / A")
    if fg == tg:
        return err(4009, "修正后分级与原分级相同，无需记录")
    stored = db.get_retrieval(case_id) or {}
    chart = next((a for a in (stored.get("priorArt") or [])
                  if a.get("pubNo") == req.pubNo), None)
    if chart is None:
        return err(4040, "该专利不在本次查新对比文件中")
    if chart.get("grade") != fg:
        return err(4009, f"原分级不匹配：记录为 {chart.get('grade')}，提交为 {fg}")
    cid = memory.record_grade_correction(
        case, pub_no=req.pubNo, from_grade=fg, to_grade=tg, reason=req.reason)
    return ok({"correctionId": cid, "kind": "grade_fix",
               "hint": "已入修正队列，离线抽取并经人工审核后才会成为经验建议"})


# ---------- 端点 6e：经验库审阅（M6 可审计）----------
@app.get("/api/experiences")
def api_list_experiences(status: str = "pending"):
    st = None if status == "all" else status
    if st and st not in ("pending", "approved", "rejected"):
        return err(4009, "status 只支持 pending/approved/rejected/all")
    items = db.list_experiences(status=st)
    return ok({"items": items,
               "approvedCount": memory.approved_count(),
               "minToEnable": memory.MIN_APPROVED,
               "enabled": memory.enabled()})


@app.post("/api/experiences/{exp_id}/review")
def api_review_experience(exp_id: str, req: ExperienceReviewReq):
    if not db.get_experience(exp_id):
        return err(4040, f"经验不存在：{exp_id}")
    memory.review_experience(exp_id, req.approve)
    return ok({"expId": exp_id, "status": "approved" if req.approve else "rejected",
               "enabled": memory.enabled()})


# ---------- 端点 7：对比分析 ----------
@app.get("/api/cases/{case_id}/comparison")
def api_comparison(case_id: str):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    return ok(case.get("priorArt", []))


# ---------- 端点 8：导出报告 ----------
@app.post("/api/cases/{case_id}/report/export")
def api_export_report(case_id: str):
    case = db.get_case(case_id)
    if not case:
        return err(4004, f"案件不存在：{case_id}")
    result = db.get_retrieval(case_id)
    if not result:
        return err(4090, "检索尚未执行，无法生成报告")

    report_no = f"QX-{case_id.replace('CN', '')}-01"
    case["reportMeta"] = {
        "no": report_no,
        "date": datetime.now().date().isoformat(),
        "searcher": "Patent-Agent · 初筛",
        "reviewer": "代理师（人工复核）",
        "db": "中国专利数据库（AMiner · 摘要 + 全文两阶段）",
        "hitsTotal": result["summary"]["coarseTotal"],
        "readDeep": result["summary"]["finePatents"],
    }
    url = report_service.generate_report_file(
        case, result, case.get("priorArt", []))
    case["status"] = "已完成"
    case["progress"] = 100
    case["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.upsert_case(case)
    return ok({"reportNo": report_no, "url": url, "format": "html"})


# ---------- 健康检查 / 凭证状态（克隆后自检用） ----------
@app.get("/api/health")
def api_health():
    return ok({"status": "running",
               "credentials": {
                   "aminer": config.aminer_configured(),
                   "llm": llm_configured(),
               },
               "hint": "凭证缺失时：AMiner 设环境变量 AMINER_TOKEN 或 probe/config.local.json；"
                       "LLM 设 DEEPSEEK_API_KEY 或 backend/config.local.json"})


# ---------- 静态托管（报告文件 + 原型，必须在 API 路由之后挂载） ----------
_FILES_DIR = Path(__file__).resolve().parents[1] / "data" / "files"
_FILES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=str(_FILES_DIR)), name="files")

_PROTO_DIR = Path(__file__).resolve().parents[2] / "patent-agent-prototype"
if _PROTO_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_PROTO_DIR), html=True), name="prototype")


@app.on_event("startup")
def _startup():
    seed.seed_if_empty()
