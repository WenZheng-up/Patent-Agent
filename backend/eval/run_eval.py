# -*- coding: utf-8 -*-
"""
M7 Trajectory Evaluation 入口：消融矩阵 replay + scorecard + 汇总表 + SVG 柱状图。

用法（cwd=backend）：
  .\\.venv\\Scripts\\python.exe -m eval.run_eval            # 全部就绪案件 × 全变体
  .\\.venv\\Scripts\\python.exe -m eval.run_eval --cases SYN-BATT-001

硬闸（R1）：
- 回放命中未录制请求 → FixtureMissing，整个案件判 R1_BLOCKED，不出任何对比结论；
- pending_recording 的真实槽位只报缺录拦截（演示用）；
- synthetic 案件的产物全部带水印，禁止据此宣称"多 agent 有效"。
"""
import argparse
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from app import db
from app.harness import teams
from app.harness.supervisor import run_supervisor
from app.agents.orchestrator import run_agent
from app.services.retrieval_service import run_retrieval

from . import golden as goldenmod
from .fixtures import (Store, FixtureMissing, ReplayAminer, ReplayLLM,
                       RecordingAminer, RecordingLLM, DATA_ROOT)
from .metrics import build_scorecard

MODEL_FLASH = "synthetic-flash"   # 合成宇宙录制模型名；真实案件录制时换真实模型 id

# (id, 说明, 构造参数)
VARIANTS = [
    ("E0", "v2 确定性管道（无 LLM/计划/handoff/critic）", {}),
    ("E1", "单 Agent think-act（覆盖率自评，无团队/计划图）", {}),
    ("E2", "Supervisor+Workers（无 critic）", {"mode": "sup", "enable_critic": False}),
    ("E3", "Full：+critic 分级仲裁/repair", {"mode": "sup", "enable_critic": True, "consensus": 1}),
    ("E7", "Full + critic 3 票共识", {"mode": "sup", "enable_critic": True, "consensus": 3}),
    ("E8", "Full + reader 独立模型路由（本宇宙模型未分化=全闪）",
     {"mode": "sup", "enable_critic": True, "consensus": 1, "split_reader": True}),
]


# ---------------- 依赖装配（record / replay 同一边界） ----------------

def make_aminer(store: Store, *, record: bool = False, real=None):
    return RecordingAminer(real, store) if record else ReplayAminer(store)


def make_llms(store: Store, *, record: bool = False, real=None,
              model: str = MODEL_FLASH):
    """返回 (main_llm, reader_getter)。E8 的 reader 为独立包装（同模型）。"""
    def _one():
        return RecordingLLM(real, store, model=model) if record \
            else ReplayLLM(store, model)
    main = _one()

    def reader(split: bool):
        if not split:
            return main
        r = _one()  # 独立 usage 账户；本宇宙同模型（E8 路由验证，非模型分化收益）
        return r

    return main, reader


# ---------------- 单变体执行 ----------------

def _fresh_db() -> None:
    db.DB_PATH = Path(tempfile.mkdtemp(prefix="chaxin_eval_")) / "eval.db"
    db._conn = None  # 触发懒重建（新库新 schema）
    # 清 Evidence Team 进程内缓存（同案件跨变体必须重跑，杜绝 chart 串味）
    run_evidence_team = getattr(teams, "run_evidence_team")
    run_evidence_team._cache = {}


def run_case_variant(variant_id: str, spec: dict, g: dict, *,
                     store: Store, record: bool = False,
                     real_aminer=None, real_llm=None, model: str = MODEL_FLASH):
    """跑一个 (案件 × 变体)。返回 scorecard；缺录抛 FixtureMissing。"""
    case = goldenmod.to_case(g)
    rpar = g["run"]
    _fresh_db()
    store.reset_counters()

    aminer = make_aminer(store, record=record, real=real_aminer)
    main_llm, reader_get = make_llms(store, record=record, real=real_llm, model=model)
    reader_llm = main_llm
    extra_accounts = []

    t0 = time.perf_counter()
    llm_note = ""
    if variant_id == "E0":
        result = run_retrieval(case, budget=rpar["budget"], min_score=rpar["min_score"],
                               size=rpar["size"], client=aminer)
        result["stop_reason"] = "submitted"
        events = result.get("events", [])
        trace = None
    elif variant_id == "E1":
        result = run_agent(case, budget=rpar["budget"], min_score=rpar["min_score"],
                           size=rpar["size"], llm=main_llm, client=aminer)
        result["stop_reason"] = "submitted"
        events = result.get("events", [])
        trace = None
    else:
        if spec.get("split_reader"):
            # 独立 usage 账户；本宇宙同模型（E8 验证的是路由，不是模型分化收益）
            reader_llm = reader_get(True)
            extra_accounts.append(reader_llm)
            llm_note = "reader 走独立模型路由；本宇宙仅 flash 一种模型，pro 列空缺"
        rid = f"eval_{g['case_id']}_{variant_id}"
        result = run_supervisor(
            case, budget=rpar["budget"], min_score=rpar["min_score"],
            size=rpar["size"], llm=main_llm, client=aminer,
            run_id=rid, enable_critic=spec.get("enable_critic", True),
            consensus=spec.get("consensus", 1), reader_llm=reader_llm)
        events = db.list_event_records(g["case_id"], rid)
        trace = result.get("trace")
    wall = (time.perf_counter() - t0) * 1000

    cost = _collect_cost([main_llm] + extra_accounts,
                         synthetic=bool(g.get("synthetic")), note=llm_note)

    return build_scorecard(g, variant_id, result, events=events, trace=trace,
                           cost=cost, wall_ms=wall,
                           notes=spec.get("desc", ""))


def _collect_cost(accounts: list, *, synthetic: bool, note: str = "") -> dict:
    totals = {"prompt_tokens": 0, "completion_tokens": 0,
              "cached_tokens": 0, "calls": 0}
    by_role: dict[str, dict] = {}
    for acc in accounts:
        u = acc.usage
        for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "calls"):
            totals[k] += u.get(k, 0)
        for role, v in acc.usage_by_role.items():
            bucket = by_role.setdefault(
                role, {"prompt_tokens": 0, "completion_tokens": 0,
                       "cached_tokens": 0, "calls": 0})
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "calls"):
                bucket[k] += v.get(k, 0)
    tokens_total = totals["prompt_tokens"] + totals["completion_tokens"]
    return {
        "prompt_tokens": totals["prompt_tokens"],
        "completion_tokens": totals["completion_tokens"],
        "cached_tokens": totals["cached_tokens"],
        "tokens_total": tokens_total,
        "calls": totals["calls"],
        "by_role": by_role,
        "models": {"flash_tokens": tokens_total, "pro_tokens": None},
        "token_basis": "字符估算（合成宇宙）" if synthetic else "API 实测量",
        "note": note,
    }


# ---------------- 案件级编排 ----------------

def evaluate_case(g: dict, out_dir: Path, *, record: bool = False,
                  real_aminer=None, real_llm=None,
                  model: str = MODEL_FLASH) -> dict:
    """返回 {case_id, status, cards:[...]}；pending/缺录有独立 status。"""
    cid = g["case_id"]
    if g.get("status") == "pending_recording":
        store = Store(cid)
        status = "R1_BLOCKED_NO_FIXTURE" if not store.is_complete_shell() \
            else "R1_BLOCKED_NO_GOLDEN"
        return {"case_id": cid, "synthetic": False, "status": status,
                "cards": [], "note": g.get("notes", "")}

    store = Store(cid)
    cards, blocked = [], None
    for vid, desc, spec in VARIANTS:
        spec = dict(spec, desc=desc)
        try:
            card = run_case_variant(vid, spec, g, store=store, record=record,
                                    real_aminer=real_aminer, real_llm=real_llm,
                                    model=model)
        except FixtureMissing as e:
            blocked = str(e)
            break
        cards.append(card)
        cdir = out_dir / "scorecards"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / f"{cid}__{vid}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "ok" if blocked is None else "R1_BLOCKED_MISSING_FIXTURE"
    return {"case_id": cid, "synthetic": bool(g.get("synthetic")),
            "status": status, "cards": cards, "blocked_reason": blocked}


# ---------------- 汇总 ----------------

_QUALITY_METRICS = [
    ("result", "recall@20", "recall@20"),
    ("result", "fine_precision", "finePrec"),
    ("result", "grade_accuracy", "gradeAcc"),
    ("result", "evidence_precision", "evidPrec"),
]
_PROC_METRICS = [
    ("process", "planning_efficiency", "planEff"),
    ("process", "handoff_efficiency", "handEff"),
]


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def summarize(reports: list[dict]) -> dict:
    """仅对 status=ok 的案件按变体聚合（synthetic 单独成组并打水印）。"""
    ready = [r for r in reports if r["status"] == "ok"]
    variants = [v[0] for v in VARIANTS]
    table = {}
    for vid in variants:
        rows = []
        for r in ready:
            card = next((c for c in r["cards"] if c["variant"] == vid), None)
            if card:
                rows.append(card)
        if not rows:
            continue
        line = {"variant": vid, "cases": len(rows)}
        for section, key, _ in _QUALITY_METRICS + _PROC_METRICS:
            line[key] = _mean([c[section].get(key) for c in rows])
        line["tokens_total"] = _mean([c["cost"]["tokens_total"] for c in rows])
        line["calls"] = _mean([c["cost"]["calls"] for c in rows])
        line["wall_ms"] = _mean([c["wall_ms"] for c in rows if c["wall_ms"] is not None])
        table[vid] = line
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "cases_total": len(reports),
            "cases_ready": len(ready),
            "synthetic_only": all(r.get("synthetic") for r in ready) if ready else None,
            "variants": table,
            "blocked": [{"case_id": r["case_id"], "status": r["status"],
                         "note": r.get("note") or r.get("blocked_reason")}
                        for r in reports if r["status"] != "ok"]}


def print_table(summary: dict) -> None:
    cols = [("recall@20", 9), ("fine_precision", 9), ("grade_accuracy", 9),
            ("evidence_precision", 9), ("planning_efficiency", 9),
            ("handoff_efficiency", 9), ("tokens_total", 8), ("calls", 6)]
    header = "variant  " + "".join(f"{name:>{w}}" for name, w in cols)
    print("\n=== Trajectory Eval 汇总"
          + ("（合成宇宙，仅验证管线，禁止外推）" if summary.get("synthetic_only") else "")
          + " ===")
    print(header)
    for vid in [v[0] for v in VARIANTS]:
        row = summary["variants"].get(vid)
        if not row:
            print(f"{vid:<8}  （无就绪 fixture）")
            continue
        cells = ""
        for name, w in cols:
            v = row.get(name)
            cells += f"{('—' if v is None else v):>{w}}"
        print(f"{vid:<8}  {cells}")
    for b in summary["blocked"]:
        print(f"[R1] {b['case_id']}: {b['status']} —— {b['note'] or ''}")


# ---------------- 零依赖 SVG 柱状图 ----------------

def render_svg(summary: dict, path: Path) -> None:
    variants = [v[0] for v in VARIANTS if v[0] in summary["variants"]]
    metric_series = [("recall@20", "#3b82f6"), ("fine_precision", "#22c55e"),
                     ("grade_accuracy", "#f59e0b"), ("evidence_precision", "#a855f7")]
    W, H = 920, 560
    pad_l, pad_b, top, group_w = 60, 90, 40, 110
    plot_w = W - pad_l - 30
    plot_h = 260
    n = len(variants)
    gw = plot_w / max(n, 1)
    bar_w = gw / (len(metric_series) + 1)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="Microsoft YaHei, sans-serif">',
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             '<text x="60" y="24" font-size="16" font-weight="bold">'
             'ChaxinHarness 消融矩阵（replay，温度0，同 fixtures）</text>']
    if summary.get("synthetic_only"):
        parts.append('<text x="60" y="42" font-size="12" fill="#b91c1c">'
                     '⚠ 合成宇宙数据：仅验证评测管线，不代表真实结论</text>')

    # y 轴网格
    for gy in range(0, 6):
        y = top + plot_h - gy * plot_h / 5
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W-30}" y2="{y:.1f}" '
                     f'stroke="#e5e7eb"/>')
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" font-size="10" '
                     f'text-anchor="end" fill="#6b7280">{gy/5:.1f}</text>')
    # 柱
    for i, vid in enumerate(variants):
        row = summary["variants"][vid]
        x0 = pad_l + i * gw + 10
        for j, (mkey, color) in enumerate(metric_series):
            v = row.get(mkey)
            if v is None:
                continue
            h = float(v) * plot_h
            x = x0 + j * bar_w
            y = top + plot_h - h
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w-3:.1f}" '
                         f'height="{h:.1f}" fill="{color}"/>')
            parts.append(f'<text x="{x+bar_w/2-1:.1f}" y="{y-3:.1f}" font-size="9" '
                         f'text-anchor="middle">{v:.2f}</text>')
        parts.append(f'<text x="{x0 + gw/2 - 12:.1f}" y="{top+plot_h+18}" font-size="12" '
                     f'text-anchor="middle">{vid}</text>')
    # 图例
    lx = pad_l
    for j, (name, color) in enumerate(metric_series):
        parts.append(f'<rect x="{lx + j*120}" y="{top+plot_h+34}" width="12" '
                     f'height="12" fill="{color}"/>')
        parts.append(f'<text x="{lx + j*120 + 17}" y="{top+plot_h+44}" font-size="11" '
                     f'fill="#374151">{name}</text>')

    # 下方面板：tokens（每变体均值，归一化到 E3=1）
    panel_top = top + plot_h + 70
    e3 = (summary["variants"].get("E3") or {}).get("tokens_total") or 1
    parts.append(f'<text x="60" y="{panel_top-8}" font-size="13" font-weight="bold">'
                 f'平均 token 用量（相对 E3，合成字符估算）</text>')
    p_w = plot_w
    base_y = panel_top + 120
    for i, vid in enumerate(variants):
        t = summary["variants"][vid].get("tokens_total")
        if t is None:
            continue
        ratio = t / e3 if e3 else 0
        h = min(ratio, 2.0) * 50
        x = pad_l + i * gw + 20
        parts.append(f'<rect x="{x:.1f}" y="{base_y-h:.1f}" width="34" height="{h:.1f}" '
                     f'fill="#94a3b8"/>')
        parts.append(f'<text x="{x+17:.1f}" y="{base_y-h-4:.1f}" font-size="10" '
                     f'text-anchor="middle">{ratio:.2f}×</text>')
        parts.append(f'<text x="{x+17:.1f}" y="{base_y+15}" font-size="11" '
                     f'text-anchor="middle">{vid}</text>')
    parts.append(f'<text x="60" y="{H-12}" font-size="10" fill="#6b7280">'
                 f'ready cases={summary["cases_ready"]}/{summary["cases_total"]} · '
                 f'生成 {summary["generated_at"]}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


# ---------------- main ----------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*", help="指定 case_id（默认全部 golden）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    goldens = goldenmod.all_goldens()
    if args.cases:
        wanted = set(args.cases)
        goldens = [g for g in goldens if g["case_id"] in wanted]
    if not goldens:
        print("没有可用 golden 案件（eval/data/golden/*.json）")
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else DATA_ROOT / "runs" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    reports = [evaluate_case(g, out_dir) for g in goldens]
    summary = summarize(reports)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    render_svg(summary, out_dir / "ablation.svg")
    print_table(summary)
    print(f"\n产物目录：{out_dir}")
    # 有 R1 拦截仍返回 0（拦截是评测的一部分）；完全无就绪案件返回 1
    return 0 if summary["cases_ready"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
