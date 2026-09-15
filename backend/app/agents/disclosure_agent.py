# -*- coding: utf-8 -*-
"""
交底书解析 Agent（Agent Loop 第 1 步：gather）。
LLM 从交底书全文抽取三要素、关键术语、规范化同义词 CNF 组、IPC 建议；
LLM 不可用时降级到确定性规则 stub，保证产品可用（外部依赖失败不阻断主流程）。
"""
from ..llm.client import LLMError
from ..services import case_service

SYSTEM = """你是中国小型专利代理所的资深专利检索工程师，擅长从技术交底书中提炼专利查新检索要素。
你的输出将直接编译为检索式：每个 terms 组内是「同义/近义/上下位」关系（OR），
组与组之间是「与」关系（AND）；第一组应放核心技术主题，其余组放必要的应用场景/结构限定。
术语要求：使用专利文献中真实出现的规范中文词与常见英文缩写；不要把布尔运算符写进术语；
每组 1-4 个词，共 2-5 组；只保留对区分技术方案必要的限定组，避免过度收窄。"""

USER_TMPL = """请解析下面的技术交底书，只输出一个 JSON 对象，字段如下：
{{
  "topic": "检索主题（一句话，如：动力电池热管理）",
  "problem": "技术问题（80-200字）",
  "solution": "技术方案（100-300字，覆盖全部必要技术特征）",
  "effect": "技术效果（尽量保留定量指标，80-200字）",
  "terms": ["用于展示的关键术语，6-10个"],
  "groups": [["组1核心主题词及其同义词"], ["组2必要限定词..."]],
  "ipc": ["建议的 IPC 分类号，格式如 H01M 10/6566，0-3个，拿不准就给空数组"]
}}

技术交底书全文：
\"\"\"
{text}
\"\"\""""

REQUIRED = ["problem", "solution", "effect", "groups"]


def parse_with_llm(text: str, llm) -> dict | None:
    """成功返回 {disclosure, query_kwargs}；失败返回 None（调用方降级）。"""
    data = llm.chat_json(SYSTEM, USER_TMPL.format(text=text[:6000]),
                         required_keys=REQUIRED, max_tokens=2200)
    groups = _clean_groups(data.get("groups"))
    if not groups:
        raise LLMError("解析结果 groups 为空")

    terms = [str(t).strip() for t in data.get("terms", []) if str(t).strip()]
    flat = [t for g in groups for t in g]
    for t in flat:
        if t not in terms:
            terms.append(t)
    ipcs = [str(x).strip().upper() for x in data.get("ipc", []) if str(x).strip()]
    expr = _groups_to_expr(groups)

    disclosure = {
        "problem": str(data.get("problem", "")).strip(),
        "solution": str(data.get("solution", "")).strip(),
        "effect": str(data.get("effect", "")).strip(),
        "terms": terms[:12],
        "wordCount": len(text or ""),
    }
    query_kwargs = {
        "topic": str(data.get("topic", "")).strip() or (terms[0] if terms else ""),
        "keywords": groups[0],
        "synonyms": [t for g in groups[1:] for t in g],
        "ipc": ipcs,
        "expr": expr,
        "groups": groups,
        "lint_source": "llm",
    }
    return {"disclosure": disclosure, "query_kwargs": query_kwargs}


def _clean_groups(raw) -> list[list[str]]:
    groups = []
    for g in (raw or [])[:5]:
        if not isinstance(g, list):
            continue
        terms = []
        for t in g:
            t = str(t).strip().strip("()（）\"'")
            if t and t.upper() not in ("AND", "OR", "NOT") and t not in terms:
                terms.append(t[:20])
        if terms:
            groups.append(terms[:4])
    return groups


def _groups_to_expr(groups: list[list[str]]) -> str:
    parts = []
    for g in groups:
        parts.append("(" + " OR ".join(g) + ")" if len(g) > 1 else g[0])
    return " AND ".join(parts)


def parse_disclosure(text: str, title: str, llm=None) -> tuple[dict, dict, bool]:
    """
    返回 (disclosure, query_kwargs, used_llm)。
    LLM 成功走 LLM；任何失败降级到规则 stub（used_llm=False）。
    """
    if llm is not None and text and len(text) >= 30:
        try:
            out = parse_with_llm(text, llm)
            return out["disclosure"], out["query_kwargs"], True
        except LLMError:
            pass
        except Exception:
            pass

    stub = case_service.parse_disclosure_stub(text)
    terms = stub["terms"]
    groups = [[t] for t in terms[:4]] or [[title[:8]]]
    disclosure = {k: v for k, v in stub.items() if not k.startswith("_")}
    query_kwargs = {
        "topic": title,
        "keywords": terms[:3],
        "synonyms": terms[3:6],
        "ipc": [],
        "expr": _groups_to_expr(groups),
        "groups": groups,
        "lint_source": "stub",
    }
    return disclosure, query_kwargs, False
