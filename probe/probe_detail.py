# -*- coding: utf-8 -*-
"""付费 detail 探测（严格 2 次）：字段结构 + 段落编号 + 同标题重复项鉴别。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_aminer import Probe, save_sample, log  # noqa: E402

TARGETS = [
    ("top_microchannel", "6a07460e38b6d5c2a27ec408"),
    ("dup_title_a", "6335e013667297566c1a99df"),
    ("dup_title_b", "63ed22af04c6eefad4edb488"),
]


def load_token():
    token = os.environ.get("AMINER_TOKEN", "").strip()
    if not token:
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
        with open(cfg, "r", encoding="utf-8") as f:
            token = json.load(f)["aminer_token"]
    return token


def brief(rec):
    desc = (rec.get("description") or {}).get("zh") or []
    head = [p[:60] for p in desc[:8]]
    numbered = [(i, p[:30]) for i, p in enumerate(desc) if p.strip().startswith("[")][:5]
    return {
        "keys": sorted(rec.keys()),
        "pub_num": rec.get("pub_num"), "pub_kind": rec.get("pub_kind"),
        "country": rec.get("country"),
        "title": (rec.get("title") or {}).get("zh"),
        "app_num": rec.get("app_num"),
        "app_date": rec.get("app_date"), "pub_date": rec.get("pub_date"),
        "assignee": rec.get("assignee"),
        "abstract_n": len((rec.get("abstract") or {}).get("zh") or []),
        "desc_n": len(desc),
        "desc_chars": sum(len(p) for p in desc),
        "claims_n": len((rec.get("claims") or {}).get("zh") or []),
        "ipcr": rec.get("ipcr"),
        "bracket_numbered": numbered,
        "head": head,
    }


def main():
    probe = Probe(load_token())
    results = {}
    for tag, pid in TARGETS:
        log("=" * 60)
        log(f"detail {tag} {pid}")
        ok, _, body, err = probe.detail(pid)
        if err == "BALANCE_INSUFFICIENT":
            log("  >>> 余额不足，停止")
            results[tag] = {"id": pid, "balance": "INSUFFICIENT", "msg": body.get("msg")}
            save_sample("phase4_balance_error.json", body)
            break
        if not ok:
            log(f"  FAIL {err}")
            results[tag] = {"id": pid, "error": err}
            continue
        rec = (body.get("data") or [{}])[0]
        b = brief(rec)
        results[tag] = {"id": pid, **b}
        log(f"  {b['pub_num']} {b['country']} kind={b['pub_kind']} 申请号={b['app_num']}")
        log(f"  标题={b['title']}")
        log(f"  申请人={json.dumps(b['assignee'], ensure_ascii=False)[:200]}")
        log(f"  摘要段={b['abstract_n']} 说明书段={b['desc_n']} ({b['desc_chars']}字) "
            f"权项={b['claims_n']} IPC={[x.get('l4') for x in (b['ipcr'] or [])]}")
        log(f"  带方括号编号的段(前5个): {b['bracket_numbered']}")
        for p in b["head"]:
            log(f"    | {p}")
        save_sample(f"phase4_detail_{tag}.json", body)

    # 同标题对差异
    a, b = results.get("dup_title_a"), results.get("dup_title_b")
    if a and b and "error" not in a and "error" not in b:
        log("=" * 60)
        log("同标题对差异：")
        for k in ("pub_num", "app_num", "country", "pub_kind", "app_date",
                  "pub_date", "desc_n", "desc_chars", "claims_n"):
            log(f"  {k}: {a.get(k)}  vs  {b.get(k)}  {'SAME' if a.get(k)==b.get(k) else 'DIFF'}")
        log(f"  assignee A={json.dumps(a.get('assignee'), ensure_ascii=False)[:150]}")
        log(f"  assignee B={json.dumps(b.get('assignee'), ensure_ascii=False)[:150]}")
    save_sample("phase4_summary.json", results)


if __name__ == "__main__":
    main()
