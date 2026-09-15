# -*- coding: utf-8 -*-
"""
对比分析 v1 骨架（契约 §8）：
- 特征项从交底书 solution 分句提取；
- 以交底书术语为关键词，在精检专利段落中做命中率匹配；
- 命中即用 normalize.locate_evidence 产出 EvidenceRef（§章节 ¶序号 + 摘录）；
- disclosed 三档：全部关键词命中=是，部分=部分，否则=否；
- grade / conclusion 为 null（v2 LLM sub-agent 精读给出 X/Y/A）。
"""
import re

from ..providers.normalize import locate_evidence


def _features_from_disclosure(disclosure: dict) -> list[str]:
    """从技术方案分句得到「我方特征」列表。"""
    text = disclosure.get("solution", "")
    parts = [p.strip(" ，,。；;：:") for p in re.split(r"[；;。:：]", text) if p.strip()]
    out = []
    for p in parts:
        # 跳过过短的引导语
        if len(p) >= 8:
            out.append(p)
    return out


def _keywords(phrase: str, terms: list[str]) -> list[str]:
    kws = [t for t in terms if t and t in phrase]
    if kws:
        return kws
    # 兜底：CJK bigram（术语表覆盖不到的分句）
    grams = set()
    for run in re.findall(r"[一-鿿]{2,}", phrase):
        for i in range(len(run) - 1):
            grams.add(run[i:i + 2])
    return sorted(grams)


def _best_paragraph(patent: dict, kws: list[str]):
    """返回 (paragraph, hit_count)；无命中返回 (None, 0)。"""
    best, best_hits = None, 0
    for p in patent.get("paragraphs", []):
        hits = sum(1 for k in kws if k in p["text"])
        if hits > best_hits:
            best, best_hits = p, hits
    return best, best_hits


def _quote_around(text: str, kws: list[str], width: int = 80) -> str:
    pos = min((text.find(k) for k in kws if text.find(k) >= 0), default=0)
    start = max(0, pos - width // 3)
    return text[start:start + width]


def build_comparison(case: dict, patents: list[dict]) -> list[dict]:
    """patents: 精检后的归一化专利（含 paragraphs）。"""
    disclosure = case.get("disclosure", {})
    terms = disclosure.get("terms", [])
    feature_phrases = _features_from_disclosure(disclosure)

    results = []
    for patent in patents:
        if not (patent.get("paragraphs") or patent.get("abstract")):
            continue
        features = []
        for phrase in feature_phrases:
            kws = _keywords(phrase, terms)
            para, hits = _best_paragraph(patent, kws)
            ratio = hits / len(kws) if kws else 0
            if ratio >= 0.99:
                disclosed = "是"
            elif hits > 0:
                disclosed = "部分"
            else:
                disclosed = "否"

            evidence = None
            theirs = "未涉及"
            if para and hits > 0:
                loc = locate_evidence(patent, para["text"][:20])
                quote = _quote_around(para["text"], kws)
                evidence = {
                    "section": loc["section"] if loc else f"§{para.get('section') or '说明书'}",
                    "paragraphIndex": para["index"],
                    "quote": quote,
                }
                theirs = para["text"][:60]

            features.append({
                "mine": phrase,
                "theirs": theirs,
                "disclosed": disclosed,
                "evidence": evidence,
            })

        results.append({
            "pubNo": _pub_no(patent),
            "title": patent.get("title_zh") or "",
            "assignee": (patent.get("assignees") or [""])[0],
            "date": patent.get("pub_date") or "",
            "grade": None,
            "score": patent.get("_ui_score", 0),
            "abstract": (patent.get("abstract") or "")[:200],
            "features": features,
            "conclusion": None,
        })

    results.sort(key=lambda x: -x["score"])
    return results


def _pub_no(patent: dict) -> str:
    pub_num = patent.get("pub_num")
    if not pub_num:
        return ""
    return f"{(patent.get('country') or 'cn').upper()}{pub_num}{patent.get('pub_kind') or ''}"
