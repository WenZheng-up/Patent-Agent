# -*- coding: utf-8 -*-
"""v6 ChaxinHarness 端到端冒烟（Plan DAG/Handoff/Router/Recovery，budget=2）。"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harness.supervisor import run_supervisor
from app.llm.client import LLMClient
from app.providers.aminer_client import AminerClient
from app.seed import DEMO_CASE


def main():
    case = copy.deepcopy(DEMO_CASE)
    llm = LLMClient()
    client = AminerClient()

    def on_event(ev):
        print(f"  [{ev['type'] or 'log':6s}] {ev['msg']}")

    print("=== v6 ChaxinHarness run（budget=2）===")
    result = run_supervisor(case, budget=2, min_score=2, size=100,
                            llm=llm, client=client, progress_cb=on_event)

    import json
    print("\n=== summary ===")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))
    print("\n=== priorArt ===")
    for a in result["prior_art"]:
        print(f"{a['pubNo']} grade={a['grade']} score={a['score']} :: {str(a.get('conclusion'))[:60]}")
    print("\nstop_reason:", result["stop_reason"], "| run_id:", result["run_id"])


if __name__ == "__main__":
    main()
