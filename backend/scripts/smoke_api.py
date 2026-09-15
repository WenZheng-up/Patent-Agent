# -*- coding: utf-8 -*-
"""FastAPI 端到端冒烟：审批 -> SSE 检索流 -> 终态 -> 对比 -> 报告导出。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
CID = "CN2026-0881"


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    # 1. 审批
    case = call("GET", f"/api/cases/{CID}")["data"]
    resp = call("POST", f"/api/cases/{CID}/query/confirm", {
        "query": case["query"]["expr"], "edited": False, "budget": 2})
    print("confirm:", resp["code"], resp["data"]["compiled"])

    # 2. SSE 流
    print("\n--- SSE ---")
    req = urllib.request.Request(
        f"{BASE}/api/cases/{CID}/retrieval/stream?budget=2&min_score=2&size=100")
    result = None
    with urllib.request.urlopen(req, timeout=180) as r:
        event = None
        for raw in r:
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
                if event == "progress":
                    print(f"  [{payload['type'] or 'log':6s}] {payload['msg']}")
                elif event == "result":
                    result = payload
                elif event == "error":
                    print("  ERROR:", payload)
                elif event == "done":
                    print("  done:", payload)
                    break

    print("\n--- summary ---")
    print(json.dumps(result["summary"], ensure_ascii=False))
    print(f"\n--- hits ({len(result['hits'])}) ---")
    for h in result["hits"][:8]:
        dc = h.get("dataCompleteness")
        note = f"段{dc['descriptionParagraphs']}/权{dc['hasClaims']}" if dc else "摘要档"
        print(f"  [{h['stage']}] {h['score']:.2f} {h['pubNo']:16s} "
              f"{h['title'][:34]} | {note}")

    # 3. 终态读取
    fin = call("GET", f"/api/cases/{CID}/retrieval")
    print("\nterminal GET code:", fin["code"], "hits:", len(fin["data"]["hits"]))

    # 4. 对比
    cmp = call("GET", f"/api/cases/{CID}/comparison")
    arts = cmp["data"]
    print(f"comparison: {len(arts)} 篇")
    for a in arts:
        states = [f["disclosed"] for f in a["features"]]
        print(f"  {a['pubNo']} grade={a['grade']} 特征{len(a['features'])}条 公开度={states}")
        for f in a["features"][:2]:
            ev = f["evidence"]
            print(f"    - {f['mine'][:24]} | {f['disclosed']} | "
                  f"{(ev['section'] + ' ¶' + str(ev['paragraphIndex'])) if ev else '—'}")

    # 5. 报告导出
    rep = call("POST", f"/api/cases/{CID}/report/export")
    print("\nreport:", rep["data"])

    # 6. 案件状态
    c = call("GET", f"/api/cases/{CID}")["data"]
    print("final status:", c["status"], c["progress"], "reportMeta.no:",
          (c.get("reportMeta") or {}).get("no"))


if __name__ == "__main__":
    main()
