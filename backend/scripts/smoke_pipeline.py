# -*- coding: utf-8 -*-
"""端到端冒烟：真实 AMiner API 跑通 粗检 fan-out -> 预算闸门 -> 精检族合并。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.providers.aminer_client import AminerClient
from app.retrieval.pipeline import coarse_search, fine_retrieve
from app.retrieval.query_compiler import parse_boolean

MOCK_EXPR = ("(相变材料 OR PCM OR 相变储热) AND (液冷板 OR 液冷) "
             "AND (动力电池 OR 电池包 OR 储能电池) AND 热管理")

events = []


def on_event(ev):
    events.append(ev)
    if ev["event"] == "subquery_done":
        print(f"  子查询 {ev['index'] + 1:2d}/{ev['total']} "
              f"[{ev['returned']:3d}条] {ev['query']}  池={ev['pool_size']}")
    elif ev["event"] == "coarse_done":
        print(f"\n粗检完成：并集 {ev['total']} 条，多子查询命中 {ev['multi_hit']} 条")
    elif ev["event"] == "detail_done":
        print(f"  detail {ev['index']}/{ev['total']} score={ev['score']} {ev['aminer_id'][:10]}")
    elif ev["event"] == "detail_error":
        print(f"  detail 失败 {ev['aminer_id'][:10]}: {ev['error'][:80]}")
    elif ev["event"] == "fine_done":
        print(f"精检完成：族合并后 {ev['patents']} 篇")


def main():
    client = AminerClient()
    groups = parse_boolean(MOCK_EXPR)
    print("=== 阶段1 粗检（免费 fan-out）===")
    result = coarse_search(client, groups, size=100, progress_cb=on_event)

    print("\nTop 10 粗排候选：")
    for c in result.candidates[:10]:
        print(f"  score={c.score:2d} rank={c.best_rank:2d} "
              f"{(c.title_zh or '?')[:46]}")

    print("\n=== 阶段2 精检（付费 budget=2）===")
    fine = fine_retrieve(client, result.candidates, budget=2, progress_cb=on_event)
    print(f"(FineResult: 族数={fine.family_count} 付费={fine.paid_detail_calls} "
          f"有效子查询={result.active_subqueries})")
    patents = fine.patents
    for p in patents:
        print("\n" + "=" * 60)
        print(f"标题: {p['title_zh']}")
        print(f"公开号: {p['pub_num']} kind={p['kinds']}  日期: {p['pub_date']}")
        print(f"申请人: {p['assignees']}")
        print(f"IPC(清洗后): {p['ipcs']}")
        print(f"段落: {len(p['paragraphs'])}  权项: {len(p['claims'])}  "
              f"摘要: {'有' if p['abstract'] else '无'}")
        print(f"粗排分: {p['coarse_score']}  数据完整度: {p['data_completeness']}")
        for para in p["paragraphs"][:4]:
            print(f"  ¶{para['index']} [{para['section']}] {para['text'][:50]}")


if __name__ == "__main__":
    main()
