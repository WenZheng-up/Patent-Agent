# -*- coding: utf-8 -*-
"""
M6 Experience Memory（建议态，克制实现）。

链路（离线与 run 中严格分离）：
  HITL 修正（检索式审批 diff / X·Y·A 改判）
    → corrections 表（只存抽象信号：词项增删、分级变化；绝不存交底书原文）
    → 离线 flash 抽取候选经验（LLM 不可用时有确定性模板降级）
    → experiences 表 status=pending，人工审核 approved 后才可用
    → approved 样本 ≥ MIN_APPROVED(10) 才启用；Planner 仅在 blocked 时被注入
      ≤3 条最相似经验（带来源案件 + "可忽略"），不自动改变任何行为
    → 采纳后反馈：效果好 confidence+0.1，效果差 -0.15（保守衰减）

合规：
- corrections/experiences 只含 IPC、检索词项、分级、抽象策略；
- 抽取 prompt 禁止复述案件原文，入口侧做长度/类型消毒；
- measured_gain 仅在有 golden 对照时填写，否则恒为 None。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .. import db

MIN_APPROVED = 10      # 样本门槛：不足则建议态整体禁用
MAX_INJECT = 3         # 单次注入上限
VALID_OUTCOMES = ("good", "bad", "neutral")
CORRECTION_KINDS = ("query_edit", "grade_fix")
VALID_GRADES = ("X", "Y", "A")

# 字段长度硬上限（防 LLM 把长原文塞进经验条目）
LIMIT_SITUATION = 120
LIMIT_STRATEGY = 80
LIMIT_CORRECTION = 120
MAX_KEYS = 12
MAX_KEY_LEN = 24


@dataclass
class Experience:
    """规格 M6 §② 九字段（situation 为特征化情境，不含原文）。"""
    exp_id: str
    situation: str
    attempted_strategy: str
    outcome: str                        # good | bad | neutral
    source_case: str
    confidence: float
    created_at: str
    human_correction: str | None = None
    measured_gain: float | None = None
    # 运行期元数据（非规格字段，支撑匹配/审核/反馈闭环）
    situation_keys: list[str] | None = None
    status: str = "pending"             # pending | approved | rejected
    origin: str = "llm"                 # llm | rule_fallback


# ---------------- 情境特征（确定性，零 LLM）----------------

def _ipc_tokens(ipc: list[str]) -> list[str]:
    """H01M 10/6566 → ['H01M', '10/6566']；大类用于鲁棒匹配，小组留作精确键。"""
    out = []
    for raw in ipc or []:
        for p in str(raw).split():
            p = p.strip().upper()
            if p and len(p) <= 16 and p not in out:
                out.append(p)
    return out


def _bigrams(text: str) -> set[str]:
    text = str(text).strip().lower()
    if len(text) <= 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def situation_of(case: dict) -> dict:
    """
    从案件构造特征化情境（只取 IPC + 审批检索式首组锚点词，不触碰交底书）。
    返回 {text, ipc(list), keys(set)} 供匹配与 prompt 展示。
    """
    query = case.get("query") or {}
    ipc_raw = list(query.get("ipc") or [])
    field = (case.get("field") or "").strip()
    if field and field not in ipc_raw:
        ipc_raw.append(field)
    ipc = _ipc_tokens(ipc_raw)
    groups = query.get("groups") or []
    anchors = [str(t).strip() for t in (groups[0] if groups else []) if str(t).strip()]
    term_keys = {a.lower()[:MAX_KEY_LEN] for a in anchors}
    keys = set(ipc) | term_keys
    for a in anchors:
        keys |= _bigrams(a)
    text = f"IPC {' '.join(ipc[:4]) or '未知'}；主题锚点：{'、'.join(anchors[:5]) or '无'}"
    return {"text": text, "ipc": ipc, "keys": keys, "anchors": anchors}


# ---------------- HITL 修正信号（run 路径外调用）----------------

def record_query_correction(case: dict, before_groups: list[list[str]],
                            after_groups: list[list[str]]) -> int | None:
    """
    检索式审批时人工改动 → query_edit 信号。
    只存词项集合的增删差（抽象信号，非原文）；无差异不记录。
    """
    before = {t.strip() for g in (before_groups or []) for t in g if t.strip()}
    after = {t.strip() for g in (after_groups or []) for t in g if t.strip()}
    added, removed = sorted(after - before), sorted(before - after)
    if not added and not removed:
        return None
    sit = situation_of(case)
    signal = {"kind": "query_edit",
              "added_terms": added[:15], "removed_terms": removed[:15],
              "groups_before": len(before_groups or []),
              "groups_after": len(after_groups or []),
              "ipc": sit["ipc"], "anchors": sit["anchors"][:8],
              "situation_text": sit["text"]}
    return db.insert_correction(case["id"], "query_edit", signal,
                                datetime.now().isoformat(timespec="seconds"))


def record_grade_correction(case: dict, *, pub_no: str, from_grade: str,
                            to_grade: str, reason: str = "") -> int:
    """人工把某篇专利的 X/Y/A 建议改判 → grade_fix 信号（公开信息，非交底书内容）。"""
    sit = situation_of(case)
    signal = {"kind": "grade_fix", "pubNo": pub_no,
              "from_grade": from_grade.upper(), "to_grade": to_grade.upper(),
              "reason": str(reason or "").strip()[:100],
              "ipc": sit["ipc"], "anchors": sit["anchors"][:8],
              "situation_text": sit["text"]}
    return db.insert_correction(case["id"], "grade_fix", signal,
                                datetime.now().isoformat(timespec="seconds"))


# ---------------- 离线抽取（flash + 确定性降级）----------------

_EXTRACT_SYSTEM = """你是专利查新系统的「经验抽取器」。输入是一条人工修正信号，
内容只包含 IPC 分类、检索词项增删或专利对比文件 X/Y/A 分级改判，不包含任何交底书原文。
你的任务：把修正抽象成一条未来检索规划可复用的【策略经验】。

硬约束：
1. 禁止复述/改写案件原文、标题、申请人；situation 只写抽象领域/IPC/技术现象；
2. attempted_strategy 是一句可执行的检索或证据策略（__STRAT__字内）；
3. 原策略被人工修正 → outcome="bad"；human_correction 只描述修正动作本身；
4. situation_keys 给 4-10 个短标签：IPC 大类 + 现象/手段词（每个 ≤__KEYLEN__ 字符）；
5. 信号信息不足以抽象时，experiences 输出空数组，不要编造。

只输出 JSON：
{"experiences": [
  {"situation": "特征化情境",
   "situation_keys": ["H01M", "短标签"],
   "attempted_strategy": "一句可执行策略",
   "outcome": "bad",
   "human_correction": "人工修正动作",
   "confidence": 0.5}
]}""".replace("__STRAT__", str(LIMIT_STRATEGY)).replace("__KEYLEN__", str(MAX_KEY_LEN))


def _sanitize_entry(raw: dict, source_case: str, created_at: str,
                    origin: str, dedup: set[str]) -> dict | None:
    """类型/长度/枚举消毒 + 去重；不合格条目丢弃（绝不带着原文入库）。"""
    if not isinstance(raw, dict):
        return None
    try:
        situation = str(raw["situation"]).strip()[:LIMIT_SITUATION]
        strategy = str(raw["attempted_strategy"]).strip()[:LIMIT_STRATEGY]
        outcome = str(raw.get("outcome", "neutral")).strip().lower()
        correction = str(raw.get("human_correction") or "").strip()[:LIMIT_CORRECTION]
    except (KeyError, TypeError):
        return None
    if not situation or not strategy or outcome not in VALID_OUTCOMES:
        return None
    keys = raw.get("situation_keys") or []
    if not isinstance(keys, list):
        return None
    keys = [str(k).strip()[:MAX_KEY_LEN] for k in keys if str(k).strip()]
    keys = list(dict.fromkeys(keys))[:MAX_KEYS]
    try:
        conf = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = min(0.95, max(0.05, conf))
    dedup_key = (source_case + "|" + strategy.lower()).encode("utf-8")
    exp_id = "exp_" + hashlib.sha1(dedup_key).hexdigest()[:12]
    if exp_id in dedup:
        return None
    dedup.add(exp_id)
    return {"exp_id": exp_id, "situation": situation, "situation_keys": keys,
            "attempted_strategy": strategy, "outcome": outcome,
            "human_correction": correction or None, "measured_gain": None,
            "source_case": source_case, "confidence": conf,
            "status": "pending", "origin": origin, "created_at": created_at}


def _rule_fallback(corr: dict, created_at: str, dedup: set[str]) -> dict | None:
    """LLM 不可用时的确定性模板抽取（信号本身已抽象，模板只做结构化转述）。"""
    sig = corr["signal"]
    if sig.get("kind") == "query_edit":
        added, removed = sig.get("added_terms", []), sig.get("removed_terms", [])
        if not added and not removed:
            return None
        raw = {
            "situation": sig.get("situation_text", "专利新颖性检索"),
            "situation_keys": (sig.get("ipc", []) + sig.get("anchors", []))[:MAX_KEYS],
            "attempted_strategy": "初始 CNF 检索式不经同义词/上位词扩展直接 fanout 粗检",
            "outcome": "bad",
            "human_correction": (f"审批时人工调词：增 {'、'.join(added[:6]) or '无'}"
                                 f"；删 {'、'.join(removed[:6]) or '无'}")[:LIMIT_CORRECTION],
            "confidence": 0.4}
    elif sig.get("kind") == "grade_fix":
        raw = {
            "situation": sig.get("situation_text", "专利证据分级复核"),
            "situation_keys": (sig.get("ipc", []) + sig.get("anchors", []))[:MAX_KEYS],
            "attempted_strategy": f"按证据公开充分度给出 {sig.get('from_grade')} 级对比文件建议",
            "outcome": "bad",
            "human_correction": (f"人工复核将 {sig.get('pubNo')} 分级 "
                                 f"{sig.get('from_grade')}→{sig.get('to_grade')}"
                                 + (f"：{sig.get('reason')}" if sig.get("reason") else ""))
            [:LIMIT_CORRECTION],
            "confidence": 0.4}
    else:
        return None
    return _sanitize_entry(raw, corr["case_id"], created_at, "rule_fallback", dedup)


def extract_experiences(llm=None, *, limit: int = 20) -> dict:
    """
    离线处理未抽取的修正信号（禁止在 run 热路径调用）。
    flash 成功走 LLM 抽取；任一调用 LLM 不可用 → 该批确定性模板降级。
    返回 {processed, inserted, fallback, skipped}。
    """
    pending = db.list_corrections(only_unextracted=True, limit=limit)
    created_at = datetime.now().isoformat(timespec="seconds")
    existing = {e["exp_id"] for e in db.list_experiences(limit=500)}
    processed, inserted, fallback, skipped = [], 0, 0, 0
    use_llm = llm is not None

    for corr in pending:
        entry = None
        if use_llm:
            try:
                data = llm.chat_json(
                    _EXTRACT_SYSTEM,
                    "修正信号：\n" + str(corr["signal"]),
                    required_keys=["experiences"], max_tokens=900, temperature=0.1)
                raws = data.get("experiences") or data.get("experiments") or []
                if isinstance(raws, list) and raws:
                    entry = _sanitize_entry(raws[0], corr["case_id"], created_at,
                                            "llm", existing)
            except Exception:  # noqa: BLE001 - 单条抽取失败必须降级/跳过，批处理不中断
                entry = None
            if entry is None:
                entry = _rule_fallback(corr, created_at, existing)
                if entry is not None:
                    fallback += 1
        else:
            entry = _rule_fallback(corr, created_at, existing)
            if entry is not None:
                fallback += 1

        if entry is None:
            skipped += 1
        else:
            db.insert_experience(entry)
            inserted += 1
        processed.append(corr["id"])

    db.mark_corrections_extracted(processed)
    return {"processed": len(processed), "inserted": inserted,
            "fallback": fallback, "skipped": skipped}


# ---------------- 审核 / 门槛 / 建议注入 ----------------

def approved_count() -> int:
    return db.count_experiences("approved")


def enabled() -> bool:
    return approved_count() >= MIN_APPROVED


def review_experience(exp_id: str, approve: bool) -> bool:
    return db.set_experience_status(
        exp_id, "approved" if approve else "rejected",
        datetime.now().isoformat(timespec="seconds"))


def _match_score(exp: dict, sit: dict) -> float:
    keys = set(exp.get("situation_keys") or [])
    ipc_hit = sum(1 for ipc in sit["ipc"] if any(ipc == k or ipc in k or k in ipc
                                                 for k in keys))
    term_hit = len(keys & sit["keys"])
    raw = 2.0 * ipc_hit + term_hit
    if raw <= 0:
        return 0.0
    return raw * (0.5 + float(exp.get("confidence", 0.5)))


def suggest(situation: dict, *, k: int = MAX_INJECT) -> list[dict]:
    """
    样本门槛未达 → 直接返回 []（调用方零特殊处理）。
    达门槛后：确定性 IPC/主题词重叠 × confidence 排序，取前 k。
    """
    if not enabled():
        return []
    scored = []
    for exp in db.list_experiences(status="approved", limit=500):
        score = _match_score(exp, situation)
        if score > 0:
            scored.append((score, exp))
    scored.sort(key=lambda x: (-x[0], -float(x[1]["confidence"])))
    return [e for _, e in scored[:k]]


def render_planner_block(suggestions: list[dict]) -> str:
    """注入 Planner user prompt 的动态区：带来源、标注建议态可忽略。"""
    if not suggestions:
        return ""
    lines = ["【历史经验建议（建议态：不自动生效，可忽略；不得据此重复已失败的检索）】"]
    for i, e in enumerate(suggestions, 1):
        lines.append(
            f"{i}. [{e['exp_id']}｜来源案件 {e['source_case']}｜置信 {e['confidence']:.2f}]"
            f" 情境：{e['situation']}")
        lines.append(f"   策略：{e['attempted_strategy']}")
        if e.get("human_correction"):
            lines.append(f"   人工曾修正：{e['human_correction']}")
    return "\n".join(lines)


def note_shown(exp_ids: list[str]) -> None:
    db.note_experiences_shown(exp_ids)


def apply_outcome_feedback(exp_id: str, helped: bool) -> None:
    """经验被采纳后的效果反馈（离线/复核时调用；confidence 保守加减）。"""
    db.bump_experience(exp_id, helped)
