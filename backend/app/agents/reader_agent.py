# -*- coding: utf-8 -*-
"""
精读 Sub-Agent（Harness「子 Agent 隔离」）：
- 每个对比文件派生一个独立上下文：只注入该专利文本 + 待比对特征清单，
  专利之间上下文互不可见，防污染、可并行；
- 强约束 JSON：X/Y/A 分级 + 逐条特征公开程度 + 段落级证据（paragraphIndex）+ 结论；
- 证据回填后端权威段落结构（§章节），并校验 quote 确实出自所引段落，防止模型杜撰；
- 单篇失败降级为关键词骨架（comparison_service），不阻断整批。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..llm.client import LLMError
from ..providers.normalize import locate_evidence
from ..services import comparison_service

SYSTEM = """你是中国国家知识产权局风格的资深专利审查员，正在做发明专利申请前的查新检索。
你将拿到一篇对比专利的文本（段落已编号）和本申请的技术特征清单。
请逐条判断每个技术特征在对比文件中是否被公开，并依据专利审查惯例给出文件级别相关性：
- X：单独一篇文件公开了独立权利要求的实质整体构思（新颖性威胁）；
- Y：该文件需与其他文件结合才破坏创造性；
- A：背景技术文件，仅反映现有技术一般状况。
证据要求：每个「是/部分」的特征必须给出所引段落编号 paragraphIndex（整数，对应提供的编号）
和该段落中的原文摘录 quote（20-60字，必须真实出自该段落，禁止编造）；「否」给 null。
"""

USER_TMPL = """【本申请】
标题：{title}

待比对技术特征（逐条，编号即 features 数组顺序）：
{features}

【对比文件】
公开号：{pub_no}　标题：{p_title}
摘要：{abstract}

说明书段落（编号:章节 文本）：
{paragraphs}
{claims}

只输出一个 JSON 对象：
{{
  "grade": "X" 或 "Y" 或 "A",
  "features": [
    {{"disclosed": "是|部分|否",
      "theirs": "对比文件中对应的公开内容概述（未公开写：未涉及），40-120字",
      "evidence": {{"paragraphIndex": 整数, "quote": "原文摘录"}} 或 null}}
  ],
  "conclusion": "审查意见结论，80-200字，说明威胁点与可争取的区别特征"
}}
注意：features 数组顺序和长度必须与待比对特征清单完全一致。"""

MAX_PARAGRAPH_CHARS = 9000
MAX_CLAIMS_CHARS = 1200


def _numbered_paragraphs(patent: dict) -> tuple[str, int]:
    buf, used = [], 0
    for p in patent.get("paragraphs", []):
        line = f"[{p['index']}]{('§' + p['section']) if p.get('section') else ''} {p['text']}"
        if used + len(line) > MAX_PARAGRAPH_CHARS:
            break
        buf.append(line)
        used += len(line)
    return "\n".join(buf), used


def _build_user(patent: dict, features: list[str], my_title: str) -> str:
    paragraphs, _ = _numbered_paragraphs(patent)
    claims = patent.get("claims") or []
    claims_txt = ""
    if claims:
        joined = "\n".join(f"权项{i+1}：{c}" for i, c in enumerate(claims[:3]))
        claims_txt = "\n权利要求（节选）：\n" + joined[:MAX_CLAIMS_CHARS]
    feat_txt = "\n".join(f"{i+1}. {f}" for i, f in enumerate(features))
    return USER_TMPL.format(
        title=my_title, features=feat_txt,
        pub_no=patent.get("pub_num", ""), p_title=patent.get("title_zh", ""),
        abstract=(patent.get("abstract") or "（无摘要）")[:500],
        paragraphs=paragraphs or "（无说明书段落）", claims=claims_txt)


def _pub_no(patent: dict) -> str:
    pn = patent.get("pub_num") or ""
    return f"{(patent.get('country') or 'cn').upper()}{pn}{patent.get('pub_kind') or ''}"


def _validate(data: dict, patent: dict, my_features: list[str]) -> dict:
    grade = data.get("grade")
    grade = grade if grade in ("X", "Y", "A") else "A"
    raw_features = data.get("features") if isinstance(data.get("features"), list) else []
    paras = patent.get("paragraphs", [])
    para_by_idx = {p["index"]: p for p in paras}

    out_features = []
    for i, mine in enumerate(my_features):
        rf = raw_features[i] if i < len(raw_features) and isinstance(raw_features[i], dict) else {}
        disclosed = rf.get("disclosed") if rf.get("disclosed") in ("是", "部分", "否") else "否"
        theirs = str(rf.get("theirs", "")).strip() or ("未涉及" if disclosed == "否" else "")
        evidence = None
        ev = rf.get("evidence")
        if disclosed != "否" and isinstance(ev, dict):
            idx = ev.get("paragraphIndex")
            quote = str(ev.get("quote", "")).strip()
            if isinstance(idx, int) and idx in para_by_idx:
                para = para_by_idx[idx]
                # 防杜撰：quote 应能在所引段落定位；定位不到则用段落原文兜底
                loc = locate_evidence(patent, quote[:20]) if quote else None
                if loc and loc["paragraph_index"] == idx:
                    pass
                else:
                    quote = para["text"][:60]
                evidence = {"section": f"§{para['section']}" if para.get("section") else "§说明书",
                            "paragraphIndex": idx, "quote": quote[:80]}
            elif quote:
                # 索引非法：在全文按摘录重新定位
                loc = locate_evidence(patent, quote[:20])
                if loc:
                    evidence = loc
        out_features.append({"mine": mine, "theirs": theirs[:140],
                             "disclosed": disclosed, "evidence": evidence})

    return {"grade": grade, "features": out_features,
            "conclusion": str(data.get("conclusion", "")).strip()[:400] or None}


def read_one(patent: dict, my_features: list[str], my_title: str, llm,
             my_assignees: list[str] | None = None, max_chars: int = MAX_PARAGRAPH_CHARS) -> dict:
    """单篇精读（供并行调用）。失败时抛 LLMError，由上层降级。"""
    global MAX_PARAGRAPH_CHARS
    saved = MAX_PARAGRAPH_CHARS
    MAX_PARAGRAPH_CHARS = max_chars
    try:
        data = llm.chat_json(SYSTEM, _build_user(patent, my_features, my_title),
                             temperature=0.05, max_tokens=4000,
                             required_keys=["grade", "features"])
    finally:
        MAX_PARAGRAPH_CHARS = saved
    result = _validate(data, patent, my_features)
    result.update({
        "pubNo": _pub_no(patent),
        "title": patent.get("title_zh") or "",
        "assignee": (patent.get("assignees") or [""])[0],
        "date": patent.get("pub_date") or "",
        "score": patent.get("_ui_score", 0),
        "abstract": (patent.get("abstract") or "")[:200],
    })
    return result


def read_all(patents: list[dict], my_features: list[str], my_title: str, llm,
             max_workers: int = 3, progress_cb=None) -> tuple[list[dict], dict]:
    """
    并行精读全部精检专利。
    返回 (prior_art, stats)；stats 含成功/降级数与 token 用量。
    """
    results, llm_ok, degraded = [], 0, 0

    def _run(patent):
        # 全文失败 → 精简上下文（段落 4500 字）重试一次 → 仍失败降级关键词骨架
        try:
            return read_one(patent, my_features, my_title, llm), True
        except Exception:
            try:
                return read_one(patent, my_features, my_title, llm, max_chars=4500), True
            except Exception:
                skeleton = comparison_service.build_comparison(
                    {"disclosure": {"solution": "；".join(my_features),
                                    "terms": my_features}}, [patent])
                return (skeleton[0] if skeleton else None), False

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_run, p): p for p in patents}
        done_n = 0
        for fut in as_completed(future_map):
            art, ok = fut.result()
            done_n += 1
            if art:
                results.append(art)
                llm_ok += 1 if ok else 0
                degraded += 0 if ok else 1
            if progress_cb:
                progress_cb(done_n, len(patents), ok, art["pubNo"] if art else "?")

    # 排序：X > Y > A > None，同级按 score
    order = {"X": 0, "Y": 1, "A": 2}
    results.sort(key=lambda a: (order.get(a.get("grade"), 3), -float(a.get("score") or 0)))
    return results, {"llm_read": llm_ok, "degraded": degraded, "usage": dict(llm.usage)}
