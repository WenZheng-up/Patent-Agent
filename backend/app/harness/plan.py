# -*- coding: utf-8 -*-
"""
M1 Dynamic Plan DAG。
- 初始 DAG 由静态模板生成（零 LLM）；
- ready 节点由调度器直接执行（常规推进不调 LLM）；
- blocked/需要判断时 Planner(flash) 输出结构化 revisions（insert/retry/skip/update_args）；
- 修订有三闸门：环检测、悬挂依赖、同角度同词重复、revision ≤3、reason 必填。
节点状态：pending → ready → running → done；异常 failed → retry/skipped。
"""
import copy

INTENTS = {"anchor_search", "gap_search", "deep_read", "targeted_read",
           "targeted_search", "critique", "recritique", "submit"}
STATUSES = {"pending", "ready", "running", "done", "skipped", "failed"}
MAX_REVISIONS = 3


class PlanError(ValueError):
    """动作/修订校验失败（三要素错误回喂 Planner）。"""


def initial_plan(case: dict, *, with_critic: bool = True) -> dict:
    """
    静态初始 DAG（零 LLM）：anchor_search → deep_read → [critique] → submit。
    with_critic=False 用于消融 E2（Supervisor+Workers，无 critic 闭环）。
    """
    nodes = [
        {"node_id": "n1", "intent": "anchor_search", "status": "ready",
         "depends_on": [], "owner_team": "search",
         "args": {"kind": "anchor"}, "result_ref": None,
         "origin": "initial", "origin_ref": None, "attempts": 0},
        {"node_id": "n2", "intent": "deep_read", "status": "pending",
         "depends_on": ["n1"], "owner_team": "evidence",
         "args": {"patent_ids": []}, "result_ref": None,
         "origin": "initial", "origin_ref": None, "attempts": 0},
    ]
    if with_critic:
        nodes.append({"node_id": "n3", "intent": "critique", "status": "pending",
                      "depends_on": ["n2"], "owner_team": "review",
                      "args": {}, "result_ref": None,
                      "origin": "initial", "origin_ref": None, "attempts": 0})
        submit_dep = ["n3"]
    else:
        submit_dep = ["n2"]
    nodes.append({"node_id": "n4", "intent": "submit", "status": "pending",
                  "depends_on": submit_dep, "owner_team": "supervisor",
                  "args": {}, "result_ref": None,
                  "origin": "initial", "origin_ref": None, "attempts": 0})
    return {
        "plan_id": "plan_0001",
        "revision": 1,
        "goal": "判定权利要求要素相对在先专利的新颖性/创造性",
        "assumptions": ["检索式已由代理师 HITL 审批"],
        "open_gaps": [],
        "nodes": nodes,
    }


# ---------- 查询/校验 ----------

def _node(plan: dict, node_id: str) -> dict | None:
    return next((n for n in plan["nodes"] if n["node_id"] == node_id), None)


def _has_cycle(plan: dict) -> bool:
    color = {n["node_id"]: 0 for n in plan["nodes"]}  # 0白 1灰 2黑
    adj = {n["node_id"]: list(n["depends_on"]) for n in plan["nodes"]}

    def dfs(u):
        color[u] = 1
        for v in adj.get(u, []):
            if v not in color:
                return True
            if color[v] == 1 or (color[v] == 0 and dfs(v)):
                return True
        color[u] = 2
        return False

    return any(color[u] == 0 and dfs(u) for u in color)


def ready_nodes(plan: dict) -> list[dict]:
    done = {n["node_id"] for n in plan["nodes"] if n["status"] == "done"}
    out = []
    for n in plan["nodes"]:
        if n["status"] in ("ready",) or (n["status"] == "pending"
                                         and all(d in done for d in n["depends_on"])):
            out.append(n)
    return out


def is_finished(plan: dict) -> bool:
    return all(n["status"] in ("done", "skipped") for n in plan["nodes"])


def failed_or_blocked(plan: dict) -> bool:
    """没有 ready，且还有未完成节点——需要 Planner 决策。"""
    return not ready_nodes(plan) and not is_finished(plan)


# ---------- 状态转移（确定性） ----------

def mark_running(plan: dict, node_id: str) -> None:
    _node(plan, node_id)["status"] = "running"


def mark_done(plan: dict, node_id: str, result_ref: str | None = None) -> None:
    n = _node(plan, node_id)
    n["status"] = "done"
    n["result_ref"] = result_ref


def mark_failed(plan: dict, node_id: str) -> None:
    _node(plan, node_id)["status"] = "failed"


def _new_node_id(plan: dict) -> str:
    nums = [int(n["node_id"][1:]) for n in plan["nodes"] if n["node_id"][1:].isdigit()]
    return f"n{max(nums, default=0) + 1}"


# ---------- 修订（LLM 输出 apply） ----------

def apply_revisions(plan: dict, revisions: list[dict], *, used_angle_keys: set,
                    origin: str = "inserted_by_recovery") -> list[dict]:
    """
    在 plan 上应用 LLM 修订。返回实际生效的变更摘要。
    校验失败抛 PlanError（三要素：规则 + 被拒修订 + 可行替代）。
    预算语义：revision 计数只对 insert（计划结构变更）生效；retry/skip/update_args 不消耗。
    """
    inserts_now = sum(1 for r in revisions if r.get("op") == "insert")
    if inserts_now and plan["revision"] - 1 + inserts_now > MAX_REVISIONS:
        raise PlanError(f"PLAN_REVISION_LIMIT|计划结构修订已达上限 {MAX_REVISIONS} 次|应收尾提交或跳过缺口")
    if not isinstance(revisions, list) or not revisions:
        raise PlanError("PLAN_INVALID|revisions 为空数组|无需修改时应输出 decision=advance")

    applied = []
    work = copy.deepcopy(plan)
    for rev in revisions:
        op = rev.get("op")
        reason = str(rev.get("reason", "")).strip()
        if op in ("insert", "retry") and not reason:
            raise PlanError(f"PLAN_INVALID|{op} 修订缺少 reason|每个修订必须写明触发原因")

        if op == "insert":
            intent = rev.get("intent")
            if intent not in INTENTS:
                raise PlanError(f"PLAN_INVALID|未知 intent={intent}|"
                                f"可选 {sorted(INTENTS)}")
            args = rev.get("args") or {}
            depends_on = rev.get("depends_on") or []
            if any(d not in {n["node_id"] for n in work["nodes"]} for d in depends_on):
                raise PlanError(f"PLAN_INVALID|depends_on 引用不存在节点 {depends_on}|"
                                "应依赖已存在节点")
            # 同角度同词重复拦截（ZERO_RESULT 后禁止原样重发）
            angle_key = (intent, tuple(sorted(
                str(t) for s in (args.get("searches") or []) for t in s.get("terms", []))))
            if angle_key in used_angle_keys:
                raise PlanError("PLAN_DUPLICATE_ANGLE|该角度相同词表已执行过且无新增|"
                                "应更换同义词/IPC/申请人角度，或 skip 判定为区别特征")
            nid = _new_node_id(work)
            work["nodes"].append({
                "node_id": nid, "intent": intent, "status": "ready",
                "depends_on": depends_on, "owner_team": rev.get("owner_team", "search"),
                "args": args, "result_ref": None,
                "origin": origin, "origin_ref": rev.get("origin_ref"),
                "attempts": 0})
            used_angle_keys.add(angle_key)
            applied.append({"op": "insert", "node_id": nid, "intent": intent, "reason": reason})

        elif op == "retry":
            target = rev.get("node")
            n = _node(work, target)
            if not n:
                raise PlanError(f"PLAN_INVALID|retry 目标 {target} 不存在|给存在的 node_id")
            if n["attempts"] >= 2:
                # 重试用尽 → 建议 skip
                n["status"] = "skipped"
                applied.append({"op": "skip", "node_id": target, "reason": "重试用尽自动跳过"})
            else:
                n["attempts"] += 1
                n["status"] = "ready"
                if rev.get("args"):
                    n["args"].update(rev["args"])
                applied.append({"op": "retry", "node_id": target, "reason": reason})

        elif op == "skip":
            n = _node(work, rev.get("node"))
            if not n:
                raise PlanError("PLAN_INVALID|skip 目标不存在|给存在的 node_id")
            n["status"] = "skipped"
            applied.append({"op": "skip", "node_id": n["node_id"], "reason": reason or "Planner 跳过"})

        elif op == "update_args":
            n = _node(work, rev.get("node"))
            if not n:
                raise PlanError("PLAN_INVALID|update_args 目标不存在|给存在的 node_id")
            n["args"].update(rev.get("args") or {})
            applied.append({"op": "update_args", "node_id": n["node_id"], "reason": reason})
        else:
            raise PlanError(f"PLAN_INVALID|未知 op={op}|insert/retry/skip/update_args")

    if _has_cycle(work):
        raise PlanError("PLAN_CYCLE|修订后依赖出现环|新节点不应反向依赖后继")

    # 提交（仅结构插入抬升 revision）
    plan["nodes"] = work["nodes"]
    if any(a["op"] == "insert" for a in applied):
        plan["revision"] += 1
    return applied


def set_gaps(plan: dict, gaps: list[str], assumptions: list[str] | None = None) -> None:
    plan["open_gaps"] = [str(g)[:80] for g in (gaps or [])][:5]
    if assumptions:
        plan["assumptions"] = [str(a)[:120] for a in assumptions][:5]
