# -*- coding: utf-8 -*-
"""
SQLite 存储。
表：
  cases         案件全量 JSON
  retrieval     检索终态 JSON
  case_events   append-only 强类型事件日志（v6：actor/etype/causation，事件即状态）
  checkpoints   Plan 检查点（run 游标 + plan 快照 + 计数器）
  read_idem     付费 detail 幂等记录（resume 不重复扣费）
进程内单连接 + 锁。
"""
import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "chaxin.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# v6 强类型事件枚举（18）
ETYPES = {
    "plan.created", "plan.revised", "step.started", "step.done",
    "action.emitted", "observation.recorded",
    "handoff.opened", "handoff.returned",
    "failure.classified", "recovery.selected",
    "guard.verdict", "critic.issued",
    "checkpoint.written", "human.approved", "skill.matched",
    "memory.suggested", "run.finished",
    # 兼容管道类观察（粗检/精检/子agent 的过程消息）
    "runtime.note",
}


def _cols(table: str) -> set:
    return {r[1] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _conn_get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS retrieval (
                case_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL)""")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS case_events (
                case_id TEXT NOT NULL, seq INTEGER NOT NULL, run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                etype TEXT NOT NULL DEFAULT 'runtime.note',
                tag TEXT NOT NULL DEFAULT '', type TEXT NOT NULL DEFAULT '',
                msg TEXT NOT NULL DEFAULT '',
                payload TEXT, causation_id INTEGER,
                PRIMARY KEY (case_id, seq))""")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                case_id TEXT NOT NULL, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                plan_snapshot TEXT NOT NULL, counters TEXT NOT NULL, written_at TEXT NOT NULL,
                PRIMARY KEY (case_id, run_id))""")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS read_idem (
                case_id TEXT NOT NULL, idem_key TEXT NOT NULL,
                patents TEXT NOT NULL, paid_calls INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (case_id, idem_key))""")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL, kind TEXT NOT NULL,
                signal TEXT NOT NULL, extracted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL)""")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS experiences (
                exp_id TEXT PRIMARY KEY,
                situation TEXT NOT NULL, situation_keys TEXT NOT NULL,
                attempted_strategy TEXT NOT NULL, outcome TEXT NOT NULL,
                human_correction TEXT, measured_gain REAL,
                source_case TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'pending',
                origin TEXT NOT NULL DEFAULT 'llm',
                shown_count INTEGER NOT NULL DEFAULT 0,
                adopted_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, reviewed_at TEXT)""")
        # 老库平滑加列
        if "actor" not in _cols("case_events"):
            _conn.execute("ALTER TABLE case_events ADD COLUMN actor TEXT NOT NULL DEFAULT 'system'")
        if "etype" not in _cols("case_events"):
            _conn.execute("ALTER TABLE case_events ADD COLUMN etype TEXT NOT NULL DEFAULT 'runtime.note'")
        if "causation_id" not in _cols("case_events"):
            _conn.execute("ALTER TABLE case_events ADD COLUMN causation_id INTEGER")
    return _conn


# ---------- EventStore ----------

def append_event(case_id: str, run_id: str, event: dict, payload: dict | None = None,
                 actor: str = "system", etype: str = "runtime.note",
                 causation_id: int | None = None) -> int:
    """追加强类型不可变事件，返回案件内 seq。event 可含 ts/tag/type/msg 的 UI 形态。"""
    if etype not in ETYPES:
        raise ValueError(f"非法事件类型: {etype}")
    with _lock:
        conn = _conn_get()
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM case_events WHERE case_id = ?",
            (case_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO case_events "
            "(case_id, seq, run_id, ts, actor, etype, tag, type, msg, payload, causation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, seq, run_id, event.get("ts", ""), actor, etype,
             event.get("tag", ""), event.get("type", ""), event.get("msg", ""),
             json.dumps(payload if payload is not None else event, ensure_ascii=False),
             causation_id))
        conn.commit()
        return seq


def list_events(case_id: str, run_id: str | None = None,
                after_seq: int | None = None) -> list[dict]:
    """回放事件（UI 形态：ts/tag/type/msg），支持游标续传。"""
    with _lock:
        sql = "SELECT ts, tag, type, msg FROM case_events WHERE case_id = ?"
        args: list = [case_id]
        if run_id:
            sql += " AND run_id = ?"; args.append(run_id)
        if after_seq is not None:
            sql += " AND seq > ?"; args.append(after_seq)
        sql += " ORDER BY seq"
        rows = _conn_get().execute(sql, args).fetchall()
    return [{"ts": r[0], "tag": r[1], "type": r[2], "msg": r[3]} for r in rows]


def list_event_records(case_id: str, run_id: str | None = None) -> list[dict]:
    """回放强类型完整记录（fold/replay/eval 用）。"""
    with _lock:
        sql = ("SELECT seq, run_id, ts, actor, etype, tag, type, msg, payload, causation_id "
               "FROM case_events WHERE case_id = ?")
        args: list = [case_id]
        if run_id:
            sql += " AND run_id = ?"; args.append(run_id)
        sql += " ORDER BY seq"
        rows = _conn_get().execute(sql, args).fetchall()
    out = []
    for r in rows:
        out.append({"seq": r[0], "run_id": r[1], "ts": r[2], "actor": r[3], "etype": r[4],
                    "tag": r[5], "type": r[6], "msg": r[7],
                    "payload": json.loads(r[8]) if r[8] else None,
                    "causation_id": r[9]})
    return out


# ---------- Checkpoint ----------

def write_checkpoint(case_id: str, run_id: str, seq: int,
                     plan_snapshot: dict, counters: dict, written_at: str) -> None:
    with _lock:
        _conn_get().execute(
            "INSERT INTO checkpoints (case_id, run_id, seq, plan_snapshot, counters, written_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(case_id, run_id) DO UPDATE SET "
            "seq=excluded.seq, plan_snapshot=excluded.plan_snapshot, "
            "counters=excluded.counters, written_at=excluded.written_at",
            (case_id, run_id, seq, json.dumps(plan_snapshot, ensure_ascii=False),
             json.dumps(counters, ensure_ascii=False), written_at))
        _conn_get().commit()


def latest_checkpoint(case_id: str, run_id: str | None = None) -> dict | None:
    with _lock:
        if run_id:
            row = _conn_get().execute(
                "SELECT run_id, seq, plan_snapshot, counters, written_at FROM checkpoints "
                "WHERE case_id=? AND run_id=? ORDER BY seq DESC LIMIT 1", (case_id, run_id)).fetchone()
        else:
            row = _conn_get().execute(
                "SELECT run_id, seq, plan_snapshot, counters, written_at FROM checkpoints "
                "WHERE case_id=? ORDER BY seq DESC LIMIT 1", (case_id,)).fetchone()
    if not row:
        return None
    return {"run_id": row[0], "seq": row[1],
            "plan_snapshot": json.loads(row[2]), "counters": json.loads(row[3]),
            "written_at": row[4]}


# ---------- 付费 detail 幂等 ----------

def get_read_idem(case_id: str, idem_key: str) -> dict | None:
    with _lock:
        row = _conn_get().execute(
            "SELECT patents, paid_calls FROM read_idem WHERE case_id=? AND idem_key=?",
            (case_id, idem_key)).fetchone()
    if not row:
        return None
    return {"patents": json.loads(row[0]), "paid_calls": row[1]}


def put_read_idem(case_id: str, idem_key: str, patents: list, paid_calls: int,
                  created_at: str) -> None:
    with _lock:
        _conn_get().execute(
            "INSERT OR REPLACE INTO read_idem (case_id, idem_key, patents, paid_calls, created_at) "
            "VALUES (?,?,?,?,?)",
            (case_id, idem_key, json.dumps(patents, ensure_ascii=False), paid_calls, created_at))
        _conn_get().commit()


# ---------- 案件/终态 ----------

def upsert_case(case: dict) -> None:
    with _lock:
        _conn_get().execute(
            "INSERT INTO cases (id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (case["id"], json.dumps(case, ensure_ascii=False), case.get("updated", "")))
        _conn_get().commit()


def get_case(case_id: str) -> dict | None:
    with _lock:
        row = _conn_get().execute("SELECT data FROM cases WHERE id = ?", (case_id,)).fetchone()
    return json.loads(row[0]) if row else None


def list_cases() -> list[dict]:
    with _lock:
        rows = _conn_get().execute(
            "SELECT data FROM cases ORDER BY updated_at DESC").fetchall()
    return [json.loads(r[0]) for r in rows]


def save_retrieval(case_id: str, result: dict) -> None:
    from datetime import datetime
    with _lock:
        _conn_get().execute(
            "INSERT INTO retrieval (case_id, data, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(case_id) DO UPDATE SET data=excluded.data, created_at=excluded.created_at",
            (case_id, json.dumps(result, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        _conn_get().commit()


def get_retrieval(case_id: str) -> dict | None:
    with _lock:
        row = _conn_get().execute(
            "SELECT data FROM retrieval WHERE case_id = ?", (case_id,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------- M6 Experience Memory：修正信号 + 经验条目 ----------

def insert_correction(case_id: str, kind: str, signal: dict, created_at: str) -> int:
    with _lock:
        cur = _conn_get().execute(
            "INSERT INTO corrections (case_id, kind, signal, extracted, created_at) "
            "VALUES (?,?,?,0,?)",
            (case_id, kind, json.dumps(signal, ensure_ascii=False), created_at))
        _conn_get().commit()
        return int(cur.lastrowid)


def list_corrections(only_unextracted: bool = True, limit: int = 50) -> list[dict]:
    sql = "SELECT id, case_id, kind, signal, extracted, created_at FROM corrections"
    if only_unextracted:
        sql += " WHERE extracted=0"
    sql += " ORDER BY id LIMIT ?"
    with _lock:
        rows = _conn_get().execute(sql, (limit,)).fetchall()
    return [{"id": r[0], "case_id": r[1], "kind": r[2],
             "signal": json.loads(r[3]), "extracted": bool(r[4]), "created_at": r[5]}
            for r in rows]


def mark_corrections_extracted(ids: list[int]) -> None:
    if not ids:
        return
    with _lock:
        _conn_get().executemany(
            "UPDATE corrections SET extracted=1 WHERE id=?", [(i,) for i in ids])
        _conn_get().commit()


def insert_experience(exp: dict) -> None:
    with _lock:
        _conn_get().execute(
            "INSERT OR IGNORE INTO experiences "
            "(exp_id, situation, situation_keys, attempted_strategy, outcome, "
            " human_correction, measured_gain, source_case, confidence, status, origin, "
            " shown_count, adopted_count, created_at, reviewed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exp["exp_id"], exp["situation"],
             json.dumps(exp.get("situation_keys", []), ensure_ascii=False),
             exp["attempted_strategy"], exp["outcome"], exp.get("human_correction"),
             exp.get("measured_gain"), exp["source_case"],
             float(exp.get("confidence", 0.5)), exp.get("status", "pending"),
             exp.get("origin", "llm"), int(exp.get("shown_count", 0)),
             int(exp.get("adopted_count", 0)), exp["created_at"],
             exp.get("reviewed_at")))
        _conn_get().commit()


def _row_to_experience(r) -> dict:
    return {"exp_id": r[0], "situation": r[1],
            "situation_keys": json.loads(r[2]) if r[2] else [],
            "attempted_strategy": r[3], "outcome": r[4],
            "human_correction": r[5], "measured_gain": r[6],
            "source_case": r[7], "confidence": r[8], "status": r[9],
            "origin": r[10], "shown_count": r[11], "adopted_count": r[12],
            "created_at": r[13], "reviewed_at": r[14]}


_EXP_COLS = ("exp_id, situation, situation_keys, attempted_strategy, outcome, "
             "human_correction, measured_gain, source_case, confidence, status, origin, "
             "shown_count, adopted_count, created_at, reviewed_at")


def list_experiences(status: str | None = None, limit: int = 200) -> list[dict]:
    sql = f"SELECT {_EXP_COLS} FROM experiences"
    args: list = []
    if status:
        sql += " WHERE status=?"; args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    with _lock:
        rows = _conn_get().execute(sql, args).fetchall()
    return [_row_to_experience(r) for r in rows]


def get_experience(exp_id: str) -> dict | None:
    with _lock:
        row = _conn_get().execute(
            f"SELECT {_EXP_COLS} FROM experiences WHERE exp_id=?", (exp_id,)).fetchone()
    return _row_to_experience(row) if row else None


def set_experience_status(exp_id: str, status: str, reviewed_at: str) -> bool:
    with _lock:
        cur = _conn_get().execute(
            "UPDATE experiences SET status=?, reviewed_at=? WHERE exp_id=?",
            (status, reviewed_at, exp_id))
        _conn_get().commit()
        return cur.rowcount > 0


def count_experiences(status: str = "approved") -> int:
    with _lock:
        return _conn_get().execute(
            "SELECT COUNT(*) FROM experiences WHERE status=?", (status,)).fetchone()[0]


def note_experiences_shown(exp_ids: list[str]) -> None:
    if not exp_ids:
        return
    with _lock:
        _conn_get().executemany(
            "UPDATE experiences SET shown_count=shown_count+1 WHERE exp_id=?",
            [(i,) for i in exp_ids])
        _conn_get().commit()


def bump_experience(exp_id: str, helped: bool) -> None:
    """采纳后效果好 → confidence+0.1；效果差 → -0.15（衰减快于累加，保守）。"""
    exp = get_experience(exp_id)
    if not exp:
        return
    new_conf = min(1.0, max(0.05, exp["confidence"] + (0.1 if helped else -0.15)))
    with _lock:
        _conn_get().execute(
            "UPDATE experiences SET confidence=?, adopted_count=adopted_count+1 "
            "WHERE exp_id=?", (new_conf, exp_id))
        _conn_get().commit()


def reset_all() -> None:
    """开发期重置（重新 seed 用）。"""
    with _lock:
        c = _conn_get()
        c.execute("DELETE FROM cases")
        c.execute("DELETE FROM retrieval")
        c.execute("DELETE FROM case_events")
        c.execute("DELETE FROM checkpoints")
        c.execute("DELETE FROM read_idem")
        c.execute("DELETE FROM corrections")
        c.execute("DELETE FROM experiences")
        c.commit()
