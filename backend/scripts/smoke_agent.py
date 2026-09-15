# -*- coding: utf-8 -*-
"""v2 Agent Loop 端到端冒烟：真实 LLM（deepseek-flash）+ 真实 AMiner。budget=2。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.orchestrator import run_agent
from app.llm.client import LLMClient
from app.providers.aminer_client import AminerClient
from app.seed import DEMO_CASE

import copy


def main():
    case = copy.deepcopy(DEMO_CASE)
    llm = LLMClient()
    client = AminerClient()
    print(f"LLM: {llm.base_url} model={llm.model}")

    def on_event(ev):
        print(f"  [{ev['type'] or 'log':6s}] {ev['msg']}")

    print("\n=== Agent Loop（max_iterations=2, budget=2）===")
    result = run_agent(case, budget=2, min_score=2, size=100,
                       max_iterations=2, llm=llm, client=client,
                       progress_cb=on_event)

    print("\n=== summary ===")
    import json
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))

    print("\n=== priorArt（LLM 精读）===")
    for a in result["prior_art"]:
        print(f"\n{a['pubNo']} grade={a['grade']} score={a['score']}")
        print(f"  结论: {a['conclusion']}")
        for f in a["features"]:
            ev = f["evidence"]
            cite = f"{ev['section']} ¶{ev['paragraphIndex']}" if ev else "—"
            print(f"  - [{f['disclosed']}] {f['mine'][:22]} | {f['theirs'][:38]} | {cite}")

    print("\nLLM usage:", llm.usage)


if __name__ == "__main__":
    main()
