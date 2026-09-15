# -*- coding: utf-8 -*-
"""新建案件 + 交底书解析。v1 为规则 stub（契约 §3），v2 替换为 LLM。"""
import re
from datetime import datetime

from .. import db

_STOP = ("一种", "方法", "装置", "系统", "以及", "包括", "用于", "通过", "根据",
         "其中", "本发", "明提", "供了", "可以", "进行", "具有", "所述")


def _extract_terms(text: str, topn: int = 8) -> list[str]:
    """粗粒度术语抽取：CJK 连续片段按词频/长度打分取 top。"""
    runs = re.findall(r"[一-鿿]{2,10}", text)
    score: dict[str, int] = {}
    for run in runs:
        for n in (4, 3, 2):
            for i in range(len(run) - n + 1):
                gram = run[i:i + n]
                if any(s in gram for s in _STOP):
                    continue
                score[gram] = score.get(gram, 0) + n
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    terms, picked = [], set()
    for gram, _ in ranked:
        if any(gram in p or p in gram for p in picked):
            continue
        terms.append(gram)
        picked.add(gram)
        if len(terms) >= topn:
            break
    return terms


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。；！？\n]", text) if s.strip()]


def parse_disclosure_stub(text: str) -> dict:
    sents = _split_sentences(text or "")
    terms = _extract_terms(text or "")
    problem = sents[0] if sents else ""
    solution = sents[1] if len(sents) > 1 else (text or "")[:200]
    effect = sents[-1] if len(sents) > 2 else ""
    return {
        "problem": problem,
        "solution": solution,
        "effect": effect,
        "terms": terms,
        "wordCount": len(text or ""),
        "_stub": True,
    }


def assemble_case(title: str, client: str, disclosure_text: str,
                  disclosure: dict, query_kwargs: dict, used_llm: bool) -> dict:
    """按解析产物装配并持久化新案件（LLM/规则两条解析路径共用）。"""
    from ..retrieval.query_compiler import compile_groups

    existing = db.list_cases()
    seq = 882 + len(existing)
    case_id = f"CN2026-{seq:04d}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    groups = query_kwargs.get("groups") or [[t] for t in disclosure.get("terms", [])[:4]]
    try:
        _, meta = compile_groups(groups)
        n_sub, n_group = meta["subquery_count"], meta["group_count"]
    except Exception:
        n_sub, n_group = len(groups), len(groups)
    parser_tag = "LLM 解析" if used_llm else "⚠️ 规则解析（LLM 不可用，建议人工修订）"
    lint = f"{parser_tag} · CNF 编译通过 · {n_group} 组 {n_sub} 个子查询 · 词袋 fan-out（粗检免费）"

    case = {
        "id": case_id,
        "title": title,
        "client": client,
        "clientContact": "",
        "field": (query_kwargs.get("ipc") or [""])[0],
        "filed": now[:10],
        "updated": now,
        "status": "待审批",
        "progress": 30,
        "disclosure": disclosure,
        "query": {
            "topic": query_kwargs.get("topic") or title,
            "keywords": query_kwargs.get("keywords") or groups[0],
            "synonyms": query_kwargs.get("synonyms") or [],
            "ipc": query_kwargs.get("ipc") or [],
            "dateFrom": f"{datetime.now().year - 8}-01-01",
            "dateTo": now[:10],
            "expr": query_kwargs.get("expr") or " AND ".join(g[0] for g in groups),
            "lint": lint,
            "groups": groups,
        },
        "hits": [],
        "priorArt": [],
        "events": [
            {"ts": now[11:], "tag": "loop.gather", "type": "",
             "msg": f"交底书已上传（{len(disclosure_text or '')} 字）→ "
                    + ("LLM 提取三要素 / 术语 / CNF 同义词组" if used_llm else "规则提取要素（降级）")},
            {"ts": now[11:], "tag": "tool.build_query", "type": "tool",
             "msg": f"检索式 v1 生成：{n_group} 组 {n_sub} 个 fan-out 子查询"
                    + (" · LLM 同义词/IPC 建议" if used_llm else " · 规则词表，建议人工修订")},
            {"ts": now[11:], "tag": "hitl.interrupt", "type": "hitl",
             "msg": "检索式送审 · interrupt 触发，checkpoint 已持久化（等待代理师）"},
        ],
        "eventsRun": [],
        "reportMeta": None,
        "budget": 20,
    }
    db.upsert_case(case)
    return case


def create_case(title: str, client: str, disclosure_text: str) -> dict:
    """纯规则路径（无 LLM 时）。"""
    disclosure = parse_disclosure_stub(disclosure_text)
    disclosure = {k: v for k, v in disclosure.items() if not k.startswith("_")}
    terms = disclosure["terms"] or (re.findall(r"[一-鿿]{2,}", title)[:3])
    groups = [[t] for t in terms[:4]]
    query_kwargs = {"topic": title, "keywords": terms[:3], "synonyms": terms[3:6],
                    "ipc": [], "groups": groups,
                    "expr": " AND ".join(g[0] for g in groups)}
    return assemble_case(title, client, disclosure_text, disclosure, query_kwargs, False)
