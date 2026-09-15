# -*- coding: utf-8 -*-
"""
M7 fixture 录制入口（当前仅合成宇宙，零外部成本）。

- 把 eval/universes.py 的 golden 定义落盘到 eval/data/golden/；
- 用 RecordingAminer/RecordingLLM 跑完整 E0-E3/E7/E8 矩阵：
  同键文件跨变体复用（reader 结果、detail 全文只"录"一次），
  E7 的 3 票 critic 自然产出 __2/__3 多重集文件；
- 写 manifest（只有可读标签与文件清单，不含任何凭证）。

真实案件（如 CN2026-0881）的 live 录制在本版本按决策暂不开启；
凭证就绪后用同一入口替换 real_aminer/real_llm 即可。

用法（cwd=backend）：
  .\\.venv\\Scripts\\python.exe scripts\\record_fixtures.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.fixtures import Store, DATA_ROOT
from eval.golden import GOLDEN_ROOT
from eval.universes import UNIVERSES, SyntheticAminer, SyntheticLLM
from eval.run_eval import evaluate_case, MODEL_FLASH


def main() -> int:
    GOLDEN_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DATA_ROOT / "runs" / f"record_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    reports = []
    for cid, universe in UNIVERSES.items():
        g = universe["golden"]
        # 1) golden 落盘（单一事实源，禁止手改漂移）
        gpath = GOLDEN_ROOT / f"{cid}.json"
        gpath.write_text(json.dumps(g, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        print(f"[golden] {gpath.name} 写入（synthetic={g['synthetic']}）")

        # 2) 全矩阵录制（跨变体同键复用）
        real_aminer = SyntheticAminer(universe)
        real_llm = SyntheticLLM(universe, model=MODEL_FLASH)
        report = evaluate_case(
            g, out_dir, record=True,
            real_aminer=real_aminer, real_llm=real_llm, model=MODEL_FLASH)
        reports.append(report)
        print(f"[record] {cid}: status={report['status']} "
              f"cards={len(report['cards'])}"
              + (f" blocked={report['blocked_reason']}" if report.get("blocked_reason") else ""))

        # 3) manifest
        store = Store(cid)
        store.write_manifest(
            mode="synthetic-record", model=MODEL_FLASH, reader_model=MODEL_FLASH,
            extra={"synthetic": True,
                   "recorded_at": datetime.now().isoformat(timespec="seconds"),
                   "variants": [c["variant"] for c in report["cards"]],
                   "note": "合成宇宙录制：零外部成本；仅验证评测管线，不产出统计结论。"})

    ok = all(r["status"] == "ok" for r in reports)
    print(f"\n录制{'成功' if ok else '存在阻断'}，产物：{out_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
