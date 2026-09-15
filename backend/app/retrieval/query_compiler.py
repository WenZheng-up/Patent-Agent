# -*- coding: utf-8 -*-
"""
检索式编译器（本项目 Agentic RAG 的关键工程件）。

背景（探针实测 2026-09-10）：AMiner /patent/search 只支持「空格分隔的 AND 词袋」，
OR 会产生噪声、括号直接返回 0 条。代理师在 HITL 界面审批的布尔检索式
    (A1 OR A2) AND (B1 OR B2) AND C
必须在服务端编译为 CNF 笛卡尔积子查询：
    A1 B1 C / A1 B2 C / A2 B1 C / A2 B2 C
再 fan-out 并发（受限速约束）执行，结果按 id 并集，
「命中的子查询数」即一个免费、可解释的粗排相关性分。
"""
import itertools
import re

# 服务端不支持的运算符/关键字，必须从术语中剔除
_FORBIDDEN_TOKENS = {"AND", "OR", "NOT", "AND/OR"}
_UNSAFE_CHARS = re.compile(r"[()（）\"'']")


class QueryCompileError(ValueError):
    pass


def sanitize_term(term: str) -> str:
    """清洗单个检索词：去括号/引号/运算符，折叠空白。"""
    term = _UNSAFE_CHARS.sub(" ", term or "")
    parts = [p for p in re.split(r"\s+", term.strip()) if p and p.upper() not in _FORBIDDEN_TOKENS]
    return " ".join(parts)


def parse_boolean(expr: str) -> list[list[str]]:
    """
    解析一层嵌套的布尔式为 CNF 组（组内 OR，组间 AND）。
    支持原型检索式形态：'(相变材料 OR PCM) AND (液冷板 OR 液冷) AND 热管理'
    不支持任意深度嵌套——HITL 结构化入参应优先使用 compile_groups。
    """
    expr = (expr or "").strip()
    if not expr:
        return []

    groups = []
    for factor in re.split(r"\s+AND\s+", expr):
        factor = factor.strip()
        # 仅允许整组被一层括号包裹
        if factor.startswith("(") and factor.endswith(")"):
            factor = factor[1:-1]
        terms = []
        for raw in re.split(r"\s+OR\s+", factor):
            t = sanitize_term(raw)
            if t and t not in terms:
                terms.append(t)
        if terms:
            groups.append(terms)
    return groups


def compile_groups(groups: list[list[str]],
                   max_subqueries: int = 36) -> tuple[list[str], dict]:
    """
    CNF 组 -> 可执行子查询列表。

    返回 (subqueries, meta)。笛卡尔积超过 max_subqueries 时，
    按「组内顺序即优先级」截断每组（同义词排在前面的通常是规范术语），
    并在 meta 中记录裁剪情况供 Agent 迭代时决策。
    """
    clean_groups = []
    dropped_terms = []
    for group in groups:
        terms = []
        for raw in group:
            t = sanitize_term(raw)
            if t and t not in terms:
                terms.append(t)
            elif raw and not t:
                dropped_terms.append(raw)
        if terms:
            clean_groups.append(terms)

    if not clean_groups:
        raise QueryCompileError("编译后无有效检索词")

    product_size = 1
    for g in clean_groups:
        product_size *= len(g)

    truncated = False
    if product_size > max_subqueries:
        truncated = True
        # 贪心：从同义词最多的组尾部丢弃，直到乘积达标
        while True:
            size = 1
            for g in clean_groups:
                size *= len(g)
            if size <= max_subqueries:
                break
            largest = max((i for i, g in enumerate(clean_groups) if len(g) > 1),
                          key=lambda i: len(clean_groups[i]), default=None)
            if largest is None:
                break
            dropped_terms.append(clean_groups[largest].pop())

    subqueries = [" ".join(combo) for combo in itertools.product(*clean_groups)]
    meta = {
        "group_count": len(clean_groups),
        "groups": clean_groups,
        "subquery_count": len(subqueries),
        "raw_product_size": product_size,
        "truncated": truncated,
        "dropped_terms": dropped_terms,
    }
    return subqueries, meta
