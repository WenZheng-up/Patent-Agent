# -*- coding: utf-8 -*-
"""
M7 Golden fixtures：人工核定标签 + 案件运行参数。

golden 文件：eval/data/golden/<case_id>.json
{
  "case_id", "title", "synthetic"(true 的案件仅验证管线，禁止进正式统计结论),
  "disclosure": {problem,solution,effect,terms},
  "query": {groups,expr,ipc,dateFrom,dateTo,...},
  "run": {budget,min_score,size},
  "labels": {"公开号": "X|Y|A"}   # 人工核定；X/Y 为应命中的相关文件，A 为背景文件
}

真实案件（synthetic=false）在 fixture 录制完成并人工核标签前，run_eval 跳过并明示 R1，
绝不用空标签出对比结论。
"""
import json
from pathlib import Path

GOLDEN_ROOT = Path(__file__).resolve().parent / "data" / "golden"
VALID_GRADES = ("X", "Y", "A")


class GoldenError(ValueError):
    pass


def golden_path(case_id: str) -> Path:
    return GOLDEN_ROOT / f"{case_id}.json"


def load_golden(case_id: str) -> dict:
    path = golden_path(case_id)
    if not path.is_file():
        raise GoldenError(f"golden 文件不存在：{path}")
    g = json.loads(path.read_text(encoding="utf-8"))
    validate(g)
    return g


def all_goldens() -> list[dict]:
    if not GOLDEN_ROOT.is_dir():
        return []
    out = []
    for p in sorted(GOLDEN_ROOT.glob("*.json")):
        g = json.loads(p.read_text(encoding="utf-8"))
        validate(g)
        out.append(g)
    return out


def validate(g: dict) -> None:
    cid = g.get("case_id")
    if not cid:
        raise GoldenError("golden 缺少 case_id")
    # 真实案件待录槽位：只占 R1 坑位，run_eval 见到后只做缺录拦截报告
    if g.get("status") == "pending_recording":
        if g.get("synthetic"):
            raise GoldenError(f"{cid}: synthetic 案件不应是 pending_recording")
        return
    if not g.get("query", {}).get("groups"):
        raise GoldenError(f"{cid}: query.groups 不能为空")
    if not g.get("disclosure", {}).get("solution"):
        raise GoldenError(f"{cid}: disclosure.solution 不能为空")
    run = g.get("run") or {}
    for k in ("budget", "min_score", "size"):
        if k not in run:
            raise GoldenError(f"{cid}: run.{k} 缺失")
    labels = g.get("labels") or {}
    if not isinstance(labels, dict):
        raise GoldenError(f"{cid}: labels 必须是 pubNo->grade 映射")
    for pub, grade in labels.items():
        if grade not in VALID_GRADES:
            raise GoldenError(f"{cid}: 非法 golden 分级 {pub}={grade}")
    if not g.get("synthetic") and not labels:
        raise GoldenError(f"{cid}: 真实案件 golden 标签为空——未人工核定前禁止评测")


def to_case(g: dict) -> dict:
    """golden 定义 -> runner 统一 case 形态（与 seed.DEMO_CASE 同构的最小集）。"""
    return {
        "id": g["case_id"],
        "case_id": g["case_id"],
        "title": g.get("title", ""),
        "field": (g.get("query", {}).get("ipc") or [""])[0],
        "status": "已审批",
        "disclosure": g["disclosure"],
        "query": g["query"],
    }


def relevant_pubnos(g: dict, grades: tuple = ("X", "Y")) -> set[str]:
    """golden 相关文件集合（默认 X/Y；召回分母）。"""
    return {pub for pub, grade in (g.get("labels") or {}).items() if grade in grades}
