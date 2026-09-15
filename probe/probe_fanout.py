# -*- coding: utf-8 -*-
"""补充探针（全免费）：size 上限、分页真实天花板、英文 token 行为、CNF fan-out 模拟。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_aminer import Probe, save_sample, log  # noqa: E402

GROUPS = {
    "A_material": ["相变材料", "PCM", "相变储热"],
    "B_cooling": ["液冷板", "液冷"],
    "C_battery": ["动力电池", "电池包", "储能电池"],
    "D_topic": ["热管理"],
}


def load_token():
    token = os.environ.get("AMINER_TOKEN", "").strip()
    if not token:
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
        with open(cfg, "r", encoding="utf-8") as f:
            token = json.load(f)["aminer_token"]
    return token


def main():
    probe = Probe(load_token())
    out = {}

    # 1) size 上限
    log("== size 上限 ==")
    for size in (100, 200):
        ok, _, body, err = probe.search("相变材料", size=size)
        n = len((body or {}).get("data") or []) if ok else None
        log(f"  size={size} -> n={n} err={err}")
        out[f"size_{size}"] = n

    # 2) 分页天花板 page 7..11
    log("== 分页天花板 ==")
    seen_ids = set()
    pages = {}
    for page in range(12):
        ok, _, body, err = probe.search("相变材料", page=page, size=20)
        if not ok:
            pages[page] = {"error": err}
            log(f"  page={page} FAIL {err}")
            break
        items = (body or {}).get("data") or []
        ids = [it.get("id") for it in items]
        dup = sum(1 for i in ids if i in seen_ids)
        seen_ids.update(ids)
        pages[page] = {"n": len(items), "dup": dup, "unique_cumulative": len(seen_ids)}
        log(f"  page={page:2d} n={len(items):2d} dup={dup} 累计去重={len(seen_ids)}")
        if len(items) == 0:
            break
    out["pages"] = pages

    # 3) 英文 token 行为
    log("== 英文 token 行为 ==")
    for q in ("PCM", "相变材料 or PCM", "相变材料 pcm"):
        ok, _, body, err = probe.search(q, size=10)
        items = (body or {}).get("data") or [] if ok else []
        titles = [(it.get("title_zh") or "")[:36] for it in items[:5]]
        log(f"  [{q}] n={len(items)} -> {titles}")
        out.setdefault("english_tokens", {})[q] = {"n": len(items), "titles": titles}

    # 4) CNF fan-out：完整 mock 检索式拆成 3*2*3*1=18 个子查询
    log("== CNF fan-out 模拟（18 子查询, size=50）==")
    import itertools
    combos = list(itertools.product(*[GROUPS[k] for k in ("A_material", "B_cooling",
                                                          "C_battery", "D_topic")]))
    union = {}          # id -> 命中的子查询数
    per_combo = {}
    for a, b, c, d in combos:
        q = f"{a} {b} {c} {d}"
        ok, _, body, err = probe.search(q, size=50)
        items = (body or {}).get("data") or [] if ok else []
        per_combo[q] = len(items)
        log(f"  [{len(items):2d}] {q}")
        for it in items:
            pid = it.get("id")
            union.setdefault(pid, {"count": 0, "title": it.get("title_zh")})
            union[pid]["count"] += 1
    log(f"  fan-out 并集去重后: {len(union)} 条；命中>=2 子查询: "
        f"{sum(1 for v in union.values() if v['count'] >= 2)} 条")
    ranked = sorted(union.items(), key=lambda kv: -kv[1]["count"])[:15]
    for pid, v in ranked:
        log(f"    x{v['count']}  {v['title'][:44]}")
    out["fanout"] = {"combo_counts": per_combo,
                     "union_size": len(union),
                     "hit_ge2": sum(1 for v in union.values() if v["count"] >= 2),
                     "top": [{"id": pid, **v} for pid, v in ranked]}
    save_sample("phase5_fanout.json", out)
    log("完成 -> samples/phase5_fanout.json")


if __name__ == "__main__":
    main()
