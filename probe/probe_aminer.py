# -*- coding: utf-8 -*-
"""
AMiner 专利检索 API 可行性探针（零第三方依赖，标准库）
用途：在正式开发后端前，验证 Patent-Agent 设计文档中的关键假设。
用法：
    python probe_aminer.py                 # 仅免费探针（search/info），不碰付费 detail
    python probe_aminer.py --detail 1      # 额外做 1 次付费 detail 调用（探余额/字段）
    python probe_aminer.py --detail 2      # 最多 2 次 detail
凭证：环境变量 AMINER_TOKEN（不要把 token 写进本文件或同步目录）。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = "https://datacenter.aminer.cn/gateway/open_platform/api"
SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

# 探针全部围绕原型演示案件：基于复合相变材料的动力电池热管理系统
SYNTAX_MATRIX = [
    ("single",       "相变材料"),
    ("and_space",    "相变材料 液冷板"),
    ("and_word",     "相变材料 AND 液冷板"),
    ("or_word",      "相变材料 OR PCM"),
    ("paren",        "(相变材料 OR PCM) AND 液冷板"),
    ("phrase",       '"复合相变材料"'),
    ("full_mock",    "(相变材料 OR PCM OR 相变储热) AND (液冷板 OR 液冷) "
                     "AND (动力电池 OR 电池包 OR 储能电池) AND 热管理"),
]


def log(msg=""):
    print(msg, flush=True)


class RateLimiter:
    def __init__(self, min_interval=0.55):
        self.min_interval = min_interval
        self.last = 0.0

    def wait(self):
        now = time.monotonic()
        delta = now - self.last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self.last = time.monotonic()


class Probe:
    def __init__(self, token):
        self.token = token
        self.limiter = RateLimiter()
        self.calls = []

    def _raw_request(self, method, path, body=None, params=None):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self.token}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def call(self, tag, method, path, body=None, params=None, retry=3):
        """带限速/退避的调用，返回 (ok, status, body, err)。"""
        last_err = None
        for attempt in range(retry + 1):
            self.limiter.wait()
            t0 = time.monotonic()
            try:
                status, payload = self._raw_request(method, path, body, params)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {"_raw": text[:500]}
                status = e.code
            except Exception as e:  # 网络层错误
                self.calls.append(dict(tag=tag, attempt=attempt, error=repr(e),
                                       elapsed=round(time.monotonic() - t0, 2)))
                last_err = repr(e)
                time.sleep(2 ** attempt)
                continue

            code = payload.get("code")
            msg = str(payload.get("msg", ""))
            elapsed = round(time.monotonic() - t0, 2)
            self.calls.append(dict(tag=tag, attempt=attempt, http=status,
                                   code=code, msg=msg[:120], elapsed=elapsed))

            if status == 200 and code in (None, 200):
                return True, status, payload, None
            if "余额" in msg:
                return False, status, payload, "BALANCE_INSUFFICIENT"
            if code in (40301, 40302, 40307, 40308):
                return False, status, payload, f"AUTH_ERROR({code})"
            if code == 40306 or status >= 500:
                last_err = f"retryable({code or status}) {msg}"
                time.sleep(2 ** attempt)
                continue
            return False, status, payload, f"ERROR({code or status}) {msg}"
        return False, None, None, f"RETRY_EXHAUSTED: {last_err}"

    def search(self, query, page=0, size=20):
        return self.call(f"search:{query[:20]}", "POST", "/patent/search",
                         body={"query": query, "page": page, "size": size})

    def info(self, pid):
        return self.call(f"info:{pid[:8]}", "GET", "/patent/info", params={"id": pid})

    def detail(self, pid):
        return self.call(f"detail:{pid[:8]}", "GET", "/patent/detail", params={"id": pid})


def save_sample(name, obj):
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    path = os.path.join(SAMPLES_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def titles(items, n=5):
    out = []
    for it in items[:n]:
        out.append((it.get("title_zh") or it.get("title") or "?").strip()[:48])
    return out


def jaccard(a, b):
    if not a and not b:
        return None
    return round(len(a & b) / len(a | b), 3)


def main():
    detail_budget = 0
    if "--detail" in sys.argv:
        i = sys.argv.index("--detail")
        detail_budget = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 1

    # 凭证优先级：环境变量 AMINER_TOKEN > 同目录 config.local.json
    token = os.environ.get("AMINER_TOKEN", "").strip()
    token_source = "env"
    if not token:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config.local.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                token = (json.load(f).get("aminer_token") or "").strip()
            token_source = "config.local.json"
        except FileNotFoundError:
            pass
    if not token:
        log("FATAL: 未读到凭证（环境变量 AMINER_TOKEN 或 probe/config.local.json）")
        sys.exit(2)

    log(f"凭证来源: {token_source}（仅显示来源，不打印 token）")
    probe = Probe(token)
    summary = {"started_at": datetime.now().isoformat(timespec="seconds"),
               "detail_budget": detail_budget, "phases": {}}

    # ---------- Phase 0: 鉴权 + smoke ----------
    log("=" * 64)
    log("Phase 0  鉴权 / smoke")
    ok, status, body, err = probe.search("相变材料", size=5)
    if not ok:
        log(f"FATAL: smoke 失败 http={status} err={err} msg={body and body.get('msg')}")
        save_sample("phase0_error.json", body or {"error": err})
        sys.exit(1)
    top_keys = sorted(body.keys())
    smoke = body.get("data") or []
    log(f"  顶层字段: {top_keys}")
    log(f"  返回 {len(smoke)} 条；样例标题: {titles(smoke, 3)}")
    save_sample("phase0_smoke.json", body)
    summary["phases"]["phase0"] = {"ok": True, "top_level_keys": top_keys,
                                   "returned": len(smoke)}

    # ---------- Phase 1: 检索语法矩阵 ----------
    log("=" * 64)
    log("Phase 1  检索语法矩阵（各 page=0 size=20）")
    matrix = {}
    for name, q in SYNTAX_MATRIX:
        ok, _, body, err = probe.search(q, size=20)
        if not ok:
            matrix[name] = {"query": q, "error": err}
            log(f"  [{name:9s}] FAIL {err}")
            continue
        items = body.get("data") or []
        ids = {it.get("id") for it in items}
        matrix[name] = {"query": q, "n": len(items), "ids": sorted(ids),
                        "sample_titles": titles(items, 5)}
        log(f"  [{name:9s}] n={len(items):2d}  {q[:40]}")
        for t in titles(items, 3):
            log(f"               - {t}")
        save_sample(f"phase1_{name}.json", body)
    # 语义推断所需的集合关系
    pairs = [("single", "or_word"), ("and_space", "and_word"),
             ("and_space", "paren"), ("or_word", "paren"),
             ("single", "phrase"), ("full_mock", "paren")]
    overlaps = {}
    for a, b in pairs:
        ia, ib = matrix.get(a, {}).get("ids"), matrix.get(b, {}).get("ids")
        if ia is not None and ib is not None:
            overlaps[f"{a}~{b}"] = jaccard(set(ia), set(ib))
    log(f"  ID 集合 Jaccard: {json.dumps(overlaps, ensure_ascii=False)}")
    summary["phases"]["phase1"] = {"matrix": {k: {kk: vv for kk, vv in v.items()
                                                  if kk != "ids"} for k, v in matrix.items()},
                                   "jaccard": overlaps}
    save_sample("phase1_ids.json", {k: v.get("ids") for k, v in matrix.items()})

    # ---------- Phase 2: 分页深度 ----------
    log("=" * 64)
    log("Phase 2  分页深度（query=相变材料, size=20, page 0..6）")
    pages = []
    seen = {}
    for page in range(7):
        ok, _, body, err = probe.search("相变材料", page=page, size=20)
        if not ok:
            pages.append({"page": page, "error": err})
            log(f"  page={page} FAIL {err}")
            break
        items = body.get("data") or []
        ids = [it.get("id") for it in items]
        dup = sum(1 for i in ids if i in seen)
        for i in ids:
            seen[i] = seen.get(i, 0) + 1
        pages.append({"page": page, "n": len(items), "dups_vs_previous": dup,
                      "first_title": titles(items, 1)})
        log(f"  page={page} n={len(items):2d} 与前页重复={dup}")
        if page == 6:
            save_sample("phase2_page6.json", body)
    # size 上限探测
    ok, _, body, err = probe.search("相变材料", size=50)
    size50_n = len((body or {}).get("data") or []) if ok else None
    log(f"  size=50 实测返回 {size50_n} 条（err={err}）")
    summary["phases"]["phase2"] = {"pages": pages, "size50_returned": size50_n}

    # ---------- Phase 3: info（免费号单） ----------
    log("=" * 64)
    log("Phase 3  /patent/info（免费，2 个 ID）")
    pid_pool = matrix.get("single", {}).get("ids") or []
    info_results = []
    for pid in pid_pool[:2]:
        ok, _, body, err = probe.info(pid)
        if not ok:
            log(f"  {pid} FAIL {err}")
            info_results.append({"id": pid, "error": err})
            continue
        rec = (body.get("data") or [{}])[0]
        info_results.append({"id": pid, "keys": sorted(rec.keys()),
                             "pub_num": rec.get("pub_num"), "country": rec.get("country"),
                             "pub_kind": rec.get("pub_kind")})
        log(f"  {pid} -> {rec.get('pub_num')} {rec.get('country')} kind={rec.get('pub_kind')}")
        save_sample(f"phase3_info_{pid[:8]}.json", body)
    summary["phases"]["phase3"] = info_results

    # ---------- Phase 4: detail（付费，严格预算） ----------
    phase4 = {"attempted": detail_budget}
    if detail_budget > 0:
        log("=" * 64)
        log(f"Phase 4  /patent/detail（付费，预算 {detail_budget} 次）")
        # 优先选与演示案件更相关的 ID：paren > single
        cand = matrix.get("paren", {}).get("ids") or matrix.get("single", {}).get("ids") or []
        used = 0
        for pid in cand:
            if used >= detail_budget:
                break
            ok, _, body, err = probe.detail(pid)
            used += 1
            if err == "BALANCE_INSUFFICIENT":
                log(f"  {pid}: 余额不足，停止 detail 探测")
                phase4["balance"] = "INSUFFICIENT"
                save_sample("phase4_balance_error.json", body)
                break
            if not ok:
                log(f"  {pid} FAIL {err}")
                phase4[pid] = {"error": err}
                continue
            rec = (body.get("data") or [{}])[0]
            desc = (rec.get("description") or {}).get("zh") or []
            abst = (rec.get("abstract") or {}).get("zh") or []
            claims = (rec.get("claims") or {}).get("zh") or []
            ipcr = rec.get("ipcr") or []
            head = desc[:6]
            numbered = [p for p in desc[:30] if p.strip().startswith("[")]
            info = {
                "id": pid,
                "top_level_data_keys": sorted(rec.keys()),
                "abstract_paragraphs": len(abst),
                "abstract_chars": sum(len(x) for x in abst),
                "description_paragraphs": len(desc),
                "description_chars": sum(len(x) for x in desc),
                "claims_count": len(claims),
                "ipcr": ipcr,
                "assignee_present": bool(rec.get("assignee")),
                "app_date": rec.get("app_date"),
                "pub_date": rec.get("pub_date"),
                "bracket_numbered_paragraphs_in_first30": len(numbered),
                "first_paragraphs": head,
                "first_claim": (claims[0][:120] if claims else None),
                "extra_top_level_keys": sorted(k for k in body.keys()
                                               if k not in ("code", "msg", "success", "data", "log_id")),
            }
            phase4.setdefault("records", []).append(info)
            log(f"  {pid}: 摘要 {info['abstract_chars']} 字 / 说明书 {len(desc)} 段 "
                f"{info['description_chars']} 字 / 权利要求 {len(claims)} 条 / IPC {len(ipcr)} 个")
            log(f"  段首方括号编号(前30段中): {len(numbered)}；申请人字段非空: {info['assignee_present']}")
            log(f"  疑似计费/余额字段: {info['extra_top_level_keys']}")
            save_sample(f"phase4_detail_{pid[:8]}.json", body)
    else:
        log("Phase 4  跳过（未传 --detail）")

    summary["phases"]["phase4"] = phase4
    summary["calls"] = probe.calls

    save_sample("summary.json", summary)
    log("=" * 64)
    log(f"完成。共记录 {len(probe.calls)} 次调用尝试，样本目录: {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
