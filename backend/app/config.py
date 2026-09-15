# -*- coding: utf-8 -*-
"""运行配置：凭证读取（环境变量优先，开发期回退 probe/config.local.json）。"""
import json
import os
from pathlib import Path

AMINER_BASE_URL = "https://datacenter.aminer.cn/gateway/open_platform/api"

# fan-out / 预算闸门默认参数（均由探针实测校准，见 probe/ 目录）
DEFAULT_SEARCH_SIZE = 100        # 实测 size=200 可用，100 兼顾相关性与成本
MIN_CALL_INTERVAL = 0.55         # 实测限速安全间隔（秒）
MAX_RETRY = 3
MAX_SUBQUERIES = 36              # CNF 笛卡尔积上限，超出触发同义词裁剪
DETAIL_BUDGET_DEFAULT = 20       # 单次检索默认付费 detail 名额


class CredentialMissing(RuntimeError):
    """凭证未配置（区别于鉴权失败）：给用户明确的配置指引。"""


def _aminer_candidates() -> list[Path]:
    return [
        Path(__file__).resolve().parents[2] / "probe" / "config.local.json",
        Path.cwd() / "probe" / "config.local.json",
    ]


def aminer_configured() -> bool:
    if os.environ.get("AMINER_TOKEN", "").strip():
        return True
    for p in _aminer_candidates():
        try:
            if p.is_file() and json.loads(p.read_text(encoding="utf-8")).get("aminer_token"):
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


def load_aminer_token() -> str:
    token = os.environ.get("AMINER_TOKEN", "").strip()
    if token:
        return token
    # 开发期回退：探针本地配置（config.local.json 已在 .gitignore，不入库）
    for path in _aminer_candidates():
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("aminer_token"):
                return data["aminer_token"].strip()
    raise CredentialMissing(
        "未配置 AMiner 凭证：请设置环境变量 AMINER_TOKEN，"
        "或复制 probe/config.local.example.json 为 probe/config.local.json 并填入 token"
    )
