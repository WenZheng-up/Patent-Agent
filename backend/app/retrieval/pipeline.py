# -*- coding: utf-8 -*-
"""
两阶段检索管道（垂直切片）：

阶段 1 粗检（免费）：CNF 子查询 fan-out -> id 并集 -> 命中数粗排
阶段 2 精检（付费）：预算闸门取 top-N detail -> 族合并/字段清洗 -> 归一化专利

粗排分解释义：score = 命中的子查询数。每个子查询横跨一个「同义词组合」，
命中越多说明该专利在越多术语表述下都相关（探针实测：最高分 8/18 的
结果精准命中交底书区别特征），且该分完全免费、可向代理师解释。
"""
from dataclasses import dataclass, field

from .. import config
from ..providers import normalize
from .query_compiler import compile_groups


@dataclass
class CoarseCandidate:
    aminer_id: str
    title_zh: str | None
    pub_year: str | None
    inventor_name: str | None
    hits: list[tuple[int, int]] = field(default_factory=list)  # (子查询序号, 排名)

    @property
    def score(self) -> int:
        return len(self.hits)

    @property
    def best_rank(self) -> int:
        return min(r for _, r in self.hits)


@dataclass
class CoarseResult:
    subqueries: list[str]
    meta: dict
    candidates: list[CoarseCandidate]
    active_subqueries: int = 0  # 返回非空的子查询数（score 归一化的分母）

    @property
    def total(self) -> int:
        return len(self.candidates)


@dataclass
class FineResult:
    patents: list[dict]                       # 族合并后的归一化专利（全文档）
    infos_by_id: dict[str, dict]              # 免费号单（摘要档候选拼 pubNo 用）
    paid_detail_calls: int
    families_built: int                       # lookahead 候选建成的族总数
    families_processed: int                   # 预算闸门内实际精检的族数
    score_by_id: dict[str, CoarseCandidate]   # 进入精检视野的全部候选（粗排分回填用）


def coarse_search(client, groups: list[list[str]], *,
                  size: int = config.DEFAULT_SEARCH_SIZE,
                  max_subqueries: int = config.MAX_SUBQUERIES,
                  progress_cb=None) -> CoarseResult:
    """阶段 1：编译 CNF 并 fan-out 粗检。progress_cb(event_dict) 用于事件流回放。"""
    subqueries, meta = compile_groups(groups, max_subqueries=max_subqueries)

    def emit(event, **kw):
        if progress_cb:
            progress_cb({"event": event, **kw})

    pool: dict[str, CoarseCandidate] = {}
    active = 0
    for idx, sq in enumerate(subqueries):
        emit("subquery_start", index=idx, query=sq, total=len(subqueries))
        items = client.search(sq, page=0, size=size)
        if items:
            active += 1
        for rank, it in enumerate(items):
            pid = it.get("id")
            if not pid:
                continue
            cand = pool.get(pid)
            if cand is None:
                cand = CoarseCandidate(
                    aminer_id=pid,
                    title_zh=it.get("title_zh") or it.get("title"),
                    pub_year=it.get("pub_year"),
                    inventor_name=it.get("inventor_name"),
                )
                pool[pid] = cand
            cand.hits.append((idx, rank))
        emit("subquery_done", index=idx, total=len(subqueries), query=sq,
             returned=len(items), pool_size=len(pool))

    candidates = sorted(pool.values(),
                        key=lambda c: (-c.score, c.best_rank, c.title_zh or ""))
    result = CoarseResult(subqueries=subqueries, meta=meta, candidates=candidates,
                          active_subqueries=active)
    emit("coarse_done", total=result.total,
         multi_hit=sum(1 for c in candidates if c.score >= 2))
    return result


def fine_retrieve(client, candidates: list[CoarseCandidate], *,
                  budget: int = config.DETAIL_BUDGET_DEFAULT,
                  min_score: int = 1,
                  lookahead_factor: float = 2.0,
                  progress_cb=None) -> FineResult:
    """
    阶段 2：预算闸门 + 付费精检 + A/B 族合并（族感知，防止为空壳 B 版白付费）。

    流程（探针实测：同申请号 A/B 拆两条 id，B 授权版常全文为空）：
    1. 取粗排前 budget*lookahead 个候选，用【免费】info 端点按申请号建族；
    2. 族按族内最高粗排分排序，最多处理 budget 个族；
    3. 每族按粗排序尝试成员 detail（付费），首个含全文的版本即采用
       （空壳版本不计入；全族皆空时保底取第一条）；
    4. merge_family 合并同批取到的多版本，回填粗排分/命中子查询。
    """
    ranked = [c for c in candidates if c.score >= min_score]
    lookahead = ranked[:max(budget, int(budget * lookahead_factor))]

    def emit(event, **kw):
        if progress_cb:
            progress_cb({"event": event, **kw})

    # --- 免费建族（info 含 pub_kind：不付费也能知道 A/B 双版本的存在）---
    emit("fine_start", candidates=len(lookahead), budget=budget)
    families: dict[tuple, dict] = {}
    infos_by_id: dict[str, dict] = {}
    for cand in lookahead:
        try:
            info = client.info(cand.aminer_id)
        except Exception as e:
            emit("info_error", aminer_id=cand.aminer_id, error=str(e))
            info = None
        key = normalize.family_key(info) if info else ("unknown", cand.aminer_id)
        fam = families.setdefault(key, {"members": [], "infos": []})
        fam["members"].append(cand)
        if info:
            fam["infos"].append(info)
            infos_by_id[cand.aminer_id] = info
    emit("families_built", families=len(families))

    ordered_families = sorted(
        families.values(),
        key=lambda f: -max(m.score for m in f["members"]))[:budget]

    # --- 族内付费精检：只对能拿到全文的成员付费，空壳跳过 ---
    raws: list[dict] = []
    used_details = 0
    for fi, fam in enumerate(ordered_families):
        members = fam["members"]
        chosen_raw = None
        for cand in members:
            try:
                raw = client.detail(cand.aminer_id)
            except Exception as e:
                emit("detail_error", aminer_id=cand.aminer_id, error=str(e))
                continue
            used_details += 1
            if raw and ((raw.get("description") or {}).get("zh")
                        or (raw.get("claims") or {}).get("zh")
                        or (raw.get("abstract") or {}).get("zh")):
                chosen_raw = raw
                break
            if raw and chosen_raw is None:
                chosen_raw = raw  # 全空保底
        # info（免费号单，含另一 kind）+ 一篇富 detail 共同参与族合并
        raws.extend(fam["infos"])
        if chosen_raw:
            raws.append(chosen_raw)
        emit("family_done", index=fi + 1, total=len(ordered_families),
             members=len(members), paid_calls=used_details)

    score_by_id = {c.aminer_id: c for c in lookahead}
    patents = normalize.merge_family(raws)
    for patent in patents:
        member_scores = [score_by_id[mid].score for mid in patent["aminer_ids"]
                         if mid in score_by_id]
        patent["coarse_score"] = max(member_scores or [0])
        hit_sq = {sq_idx for mid in patent["aminer_ids"] if mid in score_by_id
                  for sq_idx, _ in score_by_id[mid].hits}
        patent["matched_subqueries"] = sorted(hit_sq)
    patents.sort(key=lambda p: -p["coarse_score"])
    emit("fine_done", patents=len(patents), paid_calls=used_details)
    return FineResult(patents=patents, infos_by_id=infos_by_id,
                      paid_detail_calls=used_details,
                      families_built=len(families),
                      families_processed=len(ordered_families),
                      score_by_id=score_by_id)
