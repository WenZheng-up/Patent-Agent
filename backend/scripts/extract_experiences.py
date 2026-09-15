# -*- coding: utf-8 -*-
"""
M6 Experience Memory 离线抽取脚本（不在 run 热路径调用）。

读取 corrections 表中未抽取的 HITL 修正信号 → flash 抽象为候选经验 →
experiences 表 status=pending（等待人工在 /api/experiences 审阅）。
LLM 未配置/调用失败时自动走确定性模板降级（origin=rule_fallback）。

用法：
  .venv/Scripts/python scripts/extract_experiences.py [--limit 20] [--no-llm]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.harness import memory
from app.llm.client import LLMClient, LLMError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--no-llm", action="store_true", help="强制确定性模板抽取")
    args = ap.parse_args()

    llm = None
    if not args.no_llm:
        try:
            llm = LLMClient()
            print(f"[extract] LLM 就绪：{llm.model}（失败条目自动模板降级）")
        except LLMError as e:
            print(f"[extract] LLM 不可用（{str(e)[:80]}）→ 全程确定性模板降级")

    stats = memory.extract_experiences(llm, limit=args.limit)
    print(f"[extract] 处理信号 {stats['processed']} 条；"
          f"新增候选经验 {stats['inserted']} 条"
          f"（其中模板降级 {stats['fallback']} 条，跳过 {stats['skipped']} 条）")
    print(f"[extract] 当前 approved {memory.approved_count()}/{memory.MIN_APPROVED}"
          f" → 建议态{'已启用' if memory.enabled() else '未启用'}")
    pending = len(db.list_experiences(status="pending"))
    if pending:
        print(f"[extract] 待人工审阅 {pending} 条：GET /api/experiences?status=pending")


if __name__ == "__main__":
    main()
