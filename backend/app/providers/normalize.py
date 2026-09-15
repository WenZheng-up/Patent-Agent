# -*- coding: utf-8 -*-
"""
AMiner 原始响应 -> 统一业务模型的清洗层。

针对探针实测的 5 类数据质量问题：
1. IPC 分层前缀重复（l4='HH01H01M10/613' -> 'H01M 10/613'）
2. description 无 [0001] 编号，且可能整篇挤成 1 段 -> 章节/编号兜底切分
3. 同一专利 A/B 两个 kind 拆成两条 id，B 版常无全文 -> 按申请号族合并取富文本
4. claims 可能为空 -> 数据完整度标记，业务层降级处理
5. assignee 中英文名称不一致 / 为 null；日期为 {seconds} -> 保留原名 + 统一日期
"""
import re
from datetime import datetime, timezone

# 专利说明书标准章节（出现即作为切分锚点）
SECTION_HEADERS = ("技术领域", "背景技术", "发明内容", "实用新型内容",
                   "附图说明", "具体实施方式", "实施例")
_SECTION_RE = re.compile(r"(?=(?:%s))" % "|".join(SECTION_HEADERS))
_BRACKET_NUM_RE = re.compile(r"(?=\[\d{3,4}\])")          # [0001]
_CJK_BRACKET_RE = re.compile(r"(?=【[^】]{1,12}】)")      # 【0001】


def clean_ipc(raw: dict) -> str | None:
    """
    将 AMiner IPC 层级还原为标准分类号。实测数据集混存两种编码：
    - 双前缀脏格式：{l1:'H', l2:'HH01', l3:'HH01H01M', l4:'HH01H01M10/613'}
      -> 'H01M 10/613'
    - 单前缀格式：{l1:'B', l2:'B60', l3:'B60L', l4:'B60L58/26'}
      -> 'B60L 58/26'
    判据：双前缀时 l2 以 l1 重复开头（如 'HH'）；标准 IPC 大类为「字母+2 数字」，
    不可能出现字母重复。层级关系不满足时回退 l4 原值。
    """
    l1, l2, l3, l4 = (raw.get(k) for k in ("l1", "l2", "l3", "l4"))
    if not l4:
        return None
    if not (l1 and l2 and l3 and l3.startswith(l2) and l4.startswith(l3)):
        return l4
    doubled = l2.startswith(l1 * 2)
    if doubled:
        subclass = l3[len(l2):]          # 'HH01H01M' -> 'H01M'
    else:
        subclass = l3                    # 'B60L'
    group = l4[len(l3):].strip()         # '10/613' / '58/26'
    return f"{subclass} {group}" if group else subclass


def _epoch_seconds(value) -> str | None:
    """{seconds: 1758499200} -> 'YYYY-MM-DD'。"""
    if isinstance(value, dict) and value.get("seconds"):
        try:
            return datetime.fromtimestamp(int(value["seconds"]), tz=timezone.utc).date().isoformat()
        except (ValueError, OSError):
            return None
    return None


def split_description(paragraphs: list[str]) -> list[dict]:
    """
    把说明书切成带章节标注的段落。
    返回 [{index, section, text}]；evidence locator 引用 (section, index)。

    数据源正常时（如 49 段）仅做 strip；整篇挤压成 1 段时用多级正则兜底。
    """
    raws = [p.strip() for p in (paragraphs or []) if p and p.strip()]
    if not raws:
        return []

    if len(raws) >= 4:
        units = raws
    else:
        blob = "\n".join(raws)
        # 优先级 1：[0001] / 【0001】 段落编号
        if _BRACKET_NUM_RE.search(blob) or _CJK_BRACKET_RE.search(blob):
            for pattern in (_BRACKET_NUM_RE, _CJK_BRACKET_RE):
                blob = "\n".join(s.strip() for s in pattern.split(blob) if s.strip())
            units = [s for s in (x.strip() for x in blob.split("\n")) if s]
        else:
            # 优先级 2：句号/换行切句（实测挤压文本的章节多以句号分隔）
            sentences = [s.strip() for s in re.split(r"[。\n]", blob) if s.strip()]
            # 优先级 3：章节锚点切分（章节间无句号、空格粘连时）
            sectioned = [s.strip() for s in _SECTION_RE.split(blob) if s.strip()]
            units = max((sentences, sectioned), key=len)
            if len(units) < 4:
                units = [blob]

    result, current_section = [], None
    for text in units:
        if len(text) <= 8 and text in SECTION_HEADERS:
            current_section = text
            continue
        head = next((h for h in SECTION_HEADERS if text.startswith(h)), None)
        if head:
            current_section = head
        result.append({"index": len(result), "section": current_section, "text": text})
    return result


def family_key(raw: dict) -> tuple[str, str]:
    """族合并键：优先 (国家, 申请号数字)，申请号缺失时退公开号。"""
    country = (raw.get("country") or "").lower() or "unknown"
    num = re.sub(r"\D", "", raw.get("app_num") or "") or re.sub(r"\D", "", raw.get("pub_num") or "")
    return country, num or raw.get("id") or ""


def _richness(rec: dict) -> tuple[int, int, int]:
    """全文富度：(说明书字数, 权项数, IPC 数)，用于族内选主版本。"""
    desc = "".join((rec.get("description") or {}).get("zh") or [])
    claims = (rec.get("claims") or {}).get("zh") or []
    return len(desc), len(claims), len(rec.get("ipcr") or [])


def normalize_detail(raw: dict) -> dict:
    """单条 detail 记录 -> 统一模型（未做族合并）。"""
    title = (raw.get("title") or {}).get("zh") or []
    abstract = (raw.get("abstract") or {}).get("zh") or []
    desc_raw = (raw.get("description") or {}).get("zh") or []
    claims = [c.strip() for c in ((raw.get("claims") or {}).get("zh") or []) if c.strip()]
    paragraphs = split_description(desc_raw)
    inventors = [i.get("name") for i in (raw.get("inventor") or []) if i.get("name")]
    assignees = [a.get("name") for a in (raw.get("assignee") or []) if a.get("name")]

    return {
        "aminer_id": raw.get("id"),
        "country": (raw.get("country") or "").lower() or None,
        "app_num": raw.get("app_num"),
        "pub_num": raw.get("pub_num"),
        "pub_kind": raw.get("pub_kind"),
        "title_zh": title[0] if title else None,
        "app_date": _epoch_seconds(raw.get("app_date")),
        "pub_date": _epoch_seconds(raw.get("pub_date")),
        "inventors": inventors,
        "assignees": assignees,
        "abstract": abstract[0] if abstract else None,
        "paragraphs": paragraphs,
        "claims": claims,
        "ipcs": sorted({clean_ipc(x) for x in (raw.get("ipcr") or []) if clean_ipc(x)}),
        "data_completeness": {
            "has_abstract": bool(abstract),
            "description_paragraphs": len(paragraphs),
            "has_claims": bool(claims),
            "ipc_count": 0,  # 由 merge_family 用最终 ipcs 回填
        },
    }


def merge_family(records: list[dict]) -> list[dict]:
    """
    把 A/B 双版本（同国家+申请号）合并为一条：
    - 全文取族内富度最高的版本（实测 B 授权版常为空壳）
    - 保留全部 kind/aminer_id；申请人名称并集（实测中英译名不一致）
    """
    groups: dict[tuple, list[dict]] = {}
    for raw in records:
        groups.setdefault(family_key(raw), []).append(raw)

    merged = []
    for raws in groups.values():
        primary = normalize_detail(max(raws, key=_richness))
        primary["kinds"] = sorted({r.get("pub_kind") for r in raws if r.get("pub_kind")})
        primary["aminer_ids"] = [r.get("id") for r in raws]
        names = {n for r in raws for n in
                 (a.get("name") for a in (r.get("assignee") or []) if a.get("name"))}
        primary["assignees"] = sorted(names)
        primary["data_completeness"]["ipc_count"] = len(primary["ipcs"])
        merged.append(primary)
    return merged


def locate_evidence(patent: dict, snippet: str) -> dict | None:
    """
    在合并后的专利中定位证据段落，返回可用于报告引用的 locator。
    引用展示形如：§背景技术 ¶7（CN...）。
    """
    for p in patent.get("paragraphs", []):
        pos = p["text"].find(snippet)
        if pos >= 0:
            section = f"§{p['section']}" if p.get("section") else "§说明书"
            return {"section": section, "paragraph_index": p["index"],
                    "char_offset": pos, "quote": snippet[:80]}
    return None
