# -*- coding: utf-8 -*-
"""
M4 Evidence Graph（内存有向图，dict/JSON，无图数据库）。

投影关系：
  ClaimElement（我方技术特征）
      └─[disclosed{level, theirs, evidence_ref}]──▶ Patent
  Evidence（{patent, section, ¶index, quote, quote_match}）
      └─[from]──▶ Patent；被 DisclosedEdge 引用
  Patent ─[grade{reader, critic}]──▶ Grade

构建方式：charts（reader 产出 prior_art）+ patents 全文确定性投影——
LLM 不自由发挥图结构，只在图上做 critique 判断。

图查询服务于护栏：
  gaps()                 缺证据的公开特征（disclosed=是/部分 但无 evidence）
  weak_quotes()          quote 与所引段落不匹配（证据薄弱）
  x_incomplete(pubno)    X 类是否存在未覆盖（否）要素 → G2
  y_needs_combo(pubno)   Y 类是否给出组合理由（conclusion）→ G3
"""
from dataclasses import dataclass, field


@dataclass
class FeatureNode:
    fid: str                 # F0/F1...
    text: str


@dataclass
class PatentNode:
    pub_no: str
    title: str = ""
    pub_date: str = ""
    reader_grade: str | None = None
    critic_verdict: str | None = None
    conclusion: str | None = None


@dataclass
class EvidenceNode:
    eid: str                 # E<pid>.<fid>
    pub_no: str
    section: str
    paragraph_index: int
    quote: str
    quote_match: float = 1.0   # 与所引段落的相似度/包含度，由构建时计算


@dataclass
class DisclosedEdge:
    fid: str
    pub_no: str
    level: str               # 是 | 部分 | 否
    theirs: str
    evidence: EvidenceNode | None = None


@dataclass
class EvidenceGraph:
    features: list[FeatureNode] = field(default_factory=list)
    patents: dict[str, PatentNode] = field(default_factory=dict)
    edges: list[DisclosedEdge] = field(default_factory=list)

    # ---------- 构建 ----------

    @classmethod
    def from_charts(cls, features: list[str], prior_art: list[dict],
                    patent_docs: dict | None = None) -> "EvidenceGraph":
        """
        features: 我方特征（claim 要素）文本列表
        prior_art: reader 产出（features[].mine/disclosed/evidence, grade, pubNo, conclusion）
        patent_docs: {pub_no: normalize 后的 patent dict}，用于 quote 真实性校验
        """
        g = cls()
        g.features = [FeatureNode(fid=f"F{i}", text=t) for i, t in enumerate(features)]
        text_to_fid = {t: f"F{i}" for i, t in enumerate(features)}
        docs = patent_docs or {}

        for art in prior_art:
            pub = art.get("pubNo", "?")
            g.patents[pub] = PatentNode(
                pub_no=pub, title=art.get("title", ""), pub_date=art.get("date", ""),
                reader_grade=art.get("grade"), conclusion=art.get("conclusion"))
            for feat in art.get("features", []):
                mine = feat.get("mine", "")
                fid = _match_fid(mine, g.features) or _append_feature(g, mine)
                level = feat.get("disclosed", "否")
                ev_raw = feat.get("evidence")
                ev = None
                if isinstance(ev_raw, dict) and ev_raw.get("paragraphIndex") is not None:
                    match = _quote_match(ev_raw, docs.get(pub))
                    ev = EvidenceNode(
                        eid=f"E{pub}.{fid}", pub_no=pub,
                        section=ev_raw.get("section", ""),
                        paragraph_index=int(ev_raw["paragraphIndex"]),
                        quote=str(ev_raw.get("quote", ""))[:80],
                        quote_match=match)
                g.edges.append(DisclosedEdge(
                    fid=fid, pub_no=pub, level=level,
                    theirs=str(feat.get("theirs", ""))[:140], evidence=ev))
        return g

    # ---------- 图查询（护栏/Critic 用） ----------

    def edges_of(self, pub_no: str) -> list[DisclosedEdge]:
        return [e for e in self.edges if e.pub_no == pub_no]

    def gaps(self) -> list[dict]:
        """公开（是/部分）但缺有效证据的特征节点。"""
        out = []
        for e in self.edges:
            if e.level in ("是", "部分") and (e.evidence is None or e.evidence.quote_match < 0.5):
                out.append({"pub_no": e.pub_no, "fid": e.fid,
                            "feature": _ftext(self, e.fid), "level": e.level,
                            "problem": "missing_evidence" if e.evidence is None else "weak_quote"})
        return out

    def uncovered(self, pub_no: str) -> list[str]:
        """该专利中未覆盖（否）的特征文本。"""
        return [_ftext(self, e.fid) for e in self.edges_of(pub_no) if e.level == "否"]

    def x_invalid(self, pub_no: str) -> tuple[bool, str | None]:
        """G2：X 类必须单篇覆盖全部必要要素（无「否」）。返回 (是否非法, 原因)。"""
        p = self.patents.get(pub_no)
        if not p or p.reader_grade != "X":
            return False, None
        no = self.uncovered(pub_no)
        if no:
            return True, f"X 类存在未覆盖要素：{'、'.join(no[:3])}，应降为 Y（需跨篇组合）或 A"
        return False, None

    def y_invalid(self, pub_no: str) -> tuple[bool, str | None]:
        """G3：Y 类应有结论性组合/区别说明。"""
        p = self.patents.get(pub_no)
        if not p or p.reader_grade != "Y":
            return False, None
        if not (p.conclusion or "").strip() or len(p.conclusion or "") < 20:
            return True, "Y 类缺少组合路径/区别特征结论"
        return False, None

    def a_invalid(self, pub_no: str) -> tuple[bool, str | None]:
        """G3：A 类不得有逐特征正面公开（是）。"""
        p = self.patents.get(pub_no)
        if not p or p.reader_grade != "A":
            return False, None
        if any(e.level == "是" for e in self.edges_of(pub_no)):
            return True, "A 类存在逐特征正面公开，应升为 Y/X"
        return False, None

    def grade_conflicts(self) -> list[dict]:
        """供 critic：图结构能直接判定的分级冲突。"""
        out = []
        for pub in self.patents:
            for check in (self.x_invalid, self.y_invalid, self.a_invalid):
                bad, reason = check(pub)
                if bad:
                    out.append({"pub_no": pub, "reader_grade": self.patents[pub].reader_grade,
                                "issue": reason})
        return out

    def summary_for_critic(self) -> str:
        """给 Critic LLM 的紧凑图视图。"""
        lines = [f"Features: {len(self.features)}"]
        for f in self.features:
            rows = []
            for e in self.edges:
                if e.fid != f.fid:
                    continue
                cite = (f"@{e.evidence.section}¶{e.evidence.paragraph_index}"
                        if e.evidence else "无证据")
                rows.append(f"{e.pub_no}={e.level}({cite})")
            lines.append(f"  {f.fid} {f.text[:30]} → " + ("; ".join(rows) or "无任何判定"))
        return "\n".join(lines)


# ---------- helpers ----------

def _ftext(g: EvidenceGraph, fid: str) -> str:
    f = next((x for x in g.features if x.fid == fid), None)
    return f.text if f else fid


def _match_fid(mine: str, features: list[FeatureNode]) -> str | None:
    """reader 的 mine 文本与我方特征做包含匹配。"""
    for f in features:
        if mine and (mine[:12] in f.text or f.text[:12] in mine):
            return f.fid
    return None


def _append_feature(g: EvidenceGraph, text: str) -> str:
    fid = f"F{len(g.features)}"
    g.features.append(FeatureNode(fid=fid, text=text[:80]))
    return fid


def _quote_match(ev_raw: dict, patent_doc: dict | None) -> float:
    """
    quote 与所引段落的一致性：1.0 段落包含 quote 关键片段；0.5 索引存在但弱；0.0 索引非法。
    确定性校验，防 reader 杜撰段落号。
    """
    if not patent_doc:
        return 0.6  # 无源文档可核（resume 场景），给中性分
    idx = ev_raw.get("paragraphIndex")
    paras = patent_doc.get("paragraphs", [])
    para = next((p for p in paras if p.get("index") == idx), None)
    if para is None:
        return 0.0
    quote = str(ev_raw.get("quote", "")).strip()
    text = para.get("text", "")
    if not quote:
        return 0.3
    # 关键片段（去标点后 10 字滑片子串）
    key = quote.replace("，", "").replace("。", "")[:14]
    if key and key in text.replace("，", "").replace("。", ""):
        return 1.0
    # 词级重叠
    qs = set(quote) & set("相变材料液冷板温度传感器电池冷却")
    overlap = sum(1 for ch in qs if ch in text) / max(len(qs), 1)
    return round(0.4 + 0.4 * overlap, 2)
