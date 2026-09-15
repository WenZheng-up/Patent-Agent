# -*- coding: utf-8 -*-
"""演示案件 seed：CN2026-0881（与原型 mock 同一案件，状态置为「待审批」，
审批后即跑真实 AMiner 检索）。"""
from datetime import datetime

from . import db

DEMO_CASE = {
    "id": "CN2026-0881",
    "title": "一种基于复合相变材料的动力电池热管理系统",
    "client": "星驰新能源科技",
    "clientContact": "周工 · 研发部",
    "field": "H01M 10/6566",
    "filed": "2026-08-30",
    "updated": "2026-09-10 09:41",
    "status": "待审批",
    "progress": 30,

    "disclosure": {
        "problem": "现有动力电池多采用单一液冷板方案，快充工况下电芯间温差超过 8℃，"
                   "局部热点诱发热失控；增大冷却流量又带来约 30% 的附加能耗与续航损失。",
        "solution": "以石墨烯复合相变材料（PCM）包覆电芯构成被动均温层，并与微通道液冷板"
                    "主动冷却耦合：PCM 削平瞬态热峰，微通道带走稳态热负荷；内置分布式温度"
                    "传感器，热失控预警与冷却策略联动。",
        "effect": "模组最大温差由 8.4℃ 降至 2.6℃；1.5C 快充循环 1500 次后容量保持率"
                  "提升 23%；冷却能耗降低 31%。",
        "terms": ["相变材料", "石墨烯复合", "微通道液冷板", "热失控预警",
                  "动力电池", "均温层", "热管理"],
        "wordCount": 4860,
    },

    "query": {
        "topic": "动力电池 / 储能电池热管理",
        "keywords": ["相变材料", "液冷板", "热管理"],
        "synonyms": ["PCM", "相变储热", "液冷", "电池包", "热失控"],
        "ipc": ["H01M 10/6566", "H01M 10/613", "H05K 7/20"],
        "dateFrom": "2018-01-01",
        "dateTo": "2026-09-10",
        "expr": "(相变材料 OR PCM OR 相变储热) AND (液冷板 OR 液冷) "
                "AND (动力电池 OR 电池包 OR 储能电池) AND 热管理",
        "lint": "CNF 编译通过 · 4 组 18 个子查询 · 词袋 fan-out（粗检免费）",
        "groups": [["相变材料", "PCM", "相变储热"],
                   ["液冷板", "液冷"],
                   ["动力电池", "电池包", "储能电池"],
                   ["热管理"]],
    },

    "hits": [],
    "priorArt": [],
    "events": [
        {"ts": "09:41:02", "tag": "loop.gather", "type": "",
         "msg": "读取交底书 CN2026-0881 → 提取 3 要素 / 7 术语"},
        {"ts": "09:41:05", "tag": "tool.build_query", "type": "tool",
         "msg": "检索式 v1 生成：术语规范化 + 同义词 5 项 + IPC 3 项"},
        {"ts": "09:41:05", "tag": "hitl.interrupt", "type": "hitl",
         "msg": "检索式送审 · interrupt 触发，checkpoint 已持久化（等待代理师）"},
    ],
    "eventsRun": [],
    "reportMeta": None,
    "budget": 20,
}


def seed_if_empty() -> None:
    if db.list_cases():
        return
    case = json_ready(DEMO_CASE)
    case["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.upsert_case(case)


def json_ready(obj):
    import copy
    return copy.deepcopy(obj)
