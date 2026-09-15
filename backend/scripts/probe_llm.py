# -*- coding: utf-8 -*-
"""DeepSeek（OpenAI 兼容）API 探针：模型名、JSON 模式、延迟与计费字段。零第三方依赖。"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.deepseek.com"
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config.local.json")


def load_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    with open(CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)["deepseek_api_key"].strip()


def call(path, body, key, timeout=60):
    req = urllib.request.Request(
        BASE + path, method="POST" if body else "GET",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"_raw": text[:600]}


def main():
    key = load_key()
    print("key 来源:", "env" if os.environ.get("DEEPSEEK_API_KEY") else "config.local.json",
          "| 尾号", key[-4:])

    # 1. 模型列表
    st, body = call("/v1/models", None, key)
    print("\nGET /v1/models ->", st)
    if st == 200:
        ids = [m.get("id") for m in body.get("data", [])]
        print("models:", ids)
    else:
        print("body:", json.dumps(body, ensure_ascii=False)[:400])

    # 2. 候选模型名实测（用户指定 deepseek-flash）
    for model in ("deepseek-flash", "deepseek-chat"):
        t0 = time.monotonic()
        st, body = call("/v1/chat/completions", {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是测试助手，只输出 JSON。"},
                {"role": "user", "content": '返回 {"ok": true, "n": 1}，不要输出其他内容。'}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 64,
        }, key)
        ms = round((time.monotonic() - t0) * 1000)
        if st == 200:
            choice = body["choices"][0]["message"]["content"]
            print(f"\n[{model}] {ms}ms -> {choice.strip()[:120]}")
            print("  usage:", body.get("usage"), "| model 字段:", body.get("model"))
        else:
            print(f"\n[{model}] FAIL {st} ({ms}ms):",
                  json.dumps(body.get("error", body), ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
