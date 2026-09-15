# -*- coding: utf-8 -*-
"""
M7 Fixture 录制 / 回放（零第三方依赖，标准库）。

录制与回放在【同一接口边界】包装真实依赖：
- AMiner：search / info / detail（detail 付费；录制期同键复用，全矩阵去重，不重复扣费）
- LLM：chat_json / chat_text（记录结构化结果 + 当次 token 用量）

多重集语义：
- 同一 key 在【一次变体运行】内第 N 次出现，落文件 `<key>.json` / `<key>__2.json` / ...；
- 录制期文件已存在则直接复用（跨变体共用，AMiner detail 不重复付费、LLM 不重复花钱）；
- 回放期每个变体给一个新 Store（计数从 0 开始），与录制时各变体的出现次序对齐。

R1 硬闸：回放缺文件即抛 FixtureMissing，禁止拿半截 fixture 出对比结论。
fixture 只存响应体（凭证只在 HTTP 头，天然不落盘）；manifest 只留可读标签。
"""
import hashlib
import json
import shutil
import threading
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent / "data"
FIXTURE_ROOT = DATA_ROOT / "fixtures"


class FixtureMissing(RuntimeError):
    """R1：回放时命中未录制的请求——禁止出对比结论。"""


def _key(method: str, *parts) -> str:
    raw = method + "|" + "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Store:
    """单案件的响应仓库：fixtures/<case_id>/{aminer,llm}/<key>[__n].json。"""

    def __init__(self, case_id: str, root: Path = FIXTURE_ROOT):
        self.case_id = case_id
        self.dir = root / case_id
        self._counters: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self.misses: list[str] = []

    def reset_counters(self) -> None:
        """每个变体运行前重置（录制/回放都要，保证多重集序号对齐）。"""
        self._counters.clear()

    def _path(self, ns: str, key: str, idx: int) -> Path:
        name = key if idx == 0 else f"{key}__{idx + 1}"
        return self.dir / ns / f"{name}.json"

    def _next_index(self, ns: str, key: str) -> int:
        with self._lock:
            idx = self._counters.get((ns, key), 0)
            self._counters[(ns, key)] = idx + 1
            return idx

    def fetch(self, ns: str, key: str, label: str = "") -> dict:
        idx = self._next_index(ns, key)
        path = self._path(ns, key, idx)
        if not path.is_file():
            ref = f"{ns}:{label or key}"
            self.misses.append(ref)
            raise FixtureMissing(
                f"[R1] 案件 {self.case_id} 缺少录制：{ref}（第 {idx + 1} 次调用）。"
                f"请先用 scripts/record_fixtures.py 补录，禁止用半截 fixture 出对比结论。")
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def remember(self, ns: str, key: str, producer, label: str = "") -> dict:
        """
        录制：按多重集序号取/建文件。producer() 为真实调用（仅在文件不存在时执行）。
        返回 {"value": ...} 形态的统一记录。
        """
        idx = self._next_index(ns, key)
        path = self._path(ns, key, idx)
        if path.is_file():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        record = producer()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        shutil.move(str(tmp), str(path))
        return record

    def namespace_keys(self, ns: str) -> set[str]:
        d = self.dir / ns
        if not d.is_dir():
            return set()
        return {p.stem.split("__")[0] for p in d.glob("*.json")}

    def is_complete_shell(self) -> bool:
        return (self.dir / "aminer").is_dir() or (self.dir / "llm").is_dir()

    def write_manifest(self, *, mode: str, model: str, reader_model: str,
                       extra: dict | None = None) -> None:
        aminer = sorted(p.name for p in (self.dir / "aminer").glob("*.json")) \
            if (self.dir / "aminer").is_dir() else []
        llm = sorted(p.name for p in (self.dir / "llm").glob("*.json")) \
            if (self.dir / "llm").is_dir() else []
        manifest = {"case_id": self.case_id, "mode": mode,
                    "llm_model": model, "reader_model": reader_model,
                    "aminer_files": len(aminer), "llm_files": len(llm),
                    "aminer_keys": aminer[:200], "llm_keys": llm[:200]}
        if extra:
            manifest.update(extra)
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "manifest.json").open("w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)


# ---------------- AMiner 包装 ----------------

class _AminerBase:
    def __init__(self, store: Store):
        self.store = store
        # 兼容 pipeline 直接读取限速属性（回放不需要真正 sleep）
        self.min_interval = 0.0
        self.max_retry = 0


class ReplayAminer(_AminerBase):
    """回放 AMiner：search->list；info/detail->dict|None；缺录硬错。"""

    def search(self, query: str, page: int = 0, size: int = 20):
        rec = self.store.fetch("aminer", _key("search", query, page, size),
                               label=f"search:{query[:40]}")
        return json.loads(json.dumps(rec["value"]))  # 深拷贝，防调用方 mutate

    def info(self, patent_id: str):
        rec = self.store.fetch("aminer", _key("info", patent_id),
                               label=f"info:{patent_id}")
        return json.loads(json.dumps(rec["value"]))

    def detail(self, patent_id: str):
        rec = self.store.fetch("aminer", _key("detail", patent_id),
                               label=f"detail:{patent_id}")
        return json.loads(json.dumps(rec["value"]))


class RecordingAminer(_AminerBase):
    """录制 AMiner：真实调用结果落盘；同键文件已存在则复用（跨变体去重、不重复付费）。"""

    def __init__(self, real, store: Store):
        super().__init__(store)
        self.real = real

    def search(self, query: str, page: int = 0, size: int = 20):
        key = _key("search", query, page, size)
        rec = self.store.remember(
            "aminer", key, lambda: {"value": self.real.search(query, page, size)},
            label=f"search:{query[:40]}")
        return json.loads(json.dumps(rec["value"]))

    def info(self, patent_id: str):
        key = _key("info", patent_id)
        rec = self.store.remember(
            "aminer", key, lambda: {"value": self.real.info(patent_id)},
            label=f"info:{patent_id}")
        return json.loads(json.dumps(rec["value"]))

    def detail(self, patent_id: str):
        key = _key("detail", patent_id)
        rec = self.store.remember(
            "aminer", key, lambda: {"value": self.real.detail(patent_id)},
            label=f"detail:{patent_id}")
        return json.loads(json.dumps(rec["value"]))


# ---------------- LLM 包装 ----------------

# 按 system prompt 前缀识别调用角色（用于按角色归集 token；证据隔离观测）
_ROLE_MARKERS = (
    ("reader", "你是中国国家知识产权局风格的资深专利审查员"),
    ("critic", "你是专利查新审查质控员"),
    ("planner", "规划器（Supervisor）"),
    ("coverage", "你是专利检索质量评估器"),
)


def role_of(system_prompt: str) -> str:
    head = system_prompt[:80]
    for role, marker in _ROLE_MARKERS:
        if marker in head or marker in system_prompt[:200]:
            return role
    return "other"


def _llm_key(method: str, model: str, system: str, user: str,
             temperature: float, max_tokens: int) -> str:
    return _key(method, model, round(float(temperature), 2), max_tokens, system, user)


class _LLMBase:
    def __init__(self, store: Store, model: str):
        self.store = store
        self.model = model
        # 与真实 LLMClient 同形：业务代码读 usage / model
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "cached_tokens": 0, "calls": 0}
        self.usage_by_role: dict[str, dict] = {}

    def _add_usage(self, role: str, u: dict) -> None:
        for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            self.usage[k] += int(u.get(k, 0) or 0)
        self.usage["calls"] += 1
        bucket = self.usage_by_role.setdefault(
            role, {"prompt_tokens": 0, "completion_tokens": 0,
                   "cached_tokens": 0, "calls": 0})
        for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "calls"):
            bucket[k] += int(u.get(k, 0) or 0) if k != "calls" else 1


class ReplayLLM(_LLMBase):
    """回放 LLM：返回录制的结构化结果，按记录的 token 用量计费（零成本、可重复）。"""

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1,
                  max_tokens: int = 2000, required_keys=None,
                  repair_hint=None) -> dict:
        key = _llm_key("chat_json", self.model, system, user, temperature, max_tokens)
        rec = self.store.fetch("llm", key, label=f"chat_json:{role_of(system)}")
        self._add_usage(role_of(system), rec.get("usage") or {})
        return json.loads(json.dumps(rec["value"]))

    def chat_text(self, system: str, user: str, *, temperature: float = 0.3,
                  max_tokens: int = 1000) -> str:
        key = _llm_key("chat_text", self.model, system, user, temperature, max_tokens)
        rec = self.store.fetch("llm", key, label=f"chat_text:{role_of(system)}")
        self._add_usage(role_of(system), rec.get("usage") or {})
        return rec["value"]


class RecordingLLM(_LLMBase):
    """录制 LLM：真实调用 + 记录返回值与当次 token 增量；文件已存在则复用。"""

    def __init__(self, real, store: Store, model: str | None = None):
        super().__init__(store, model or getattr(real, "model", "unknown"))
        self.real = real
        self._call_lock = threading.Lock()  # 并行 reader 下保证 token 增量归属正确

    def _snapshot_delta(self) -> dict:
        u = self.real.usage
        return {"prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "cached_tokens": u.get("cached_tokens", 0)}

    def _run(self, method: str, system: str, user: str, temperature, max_tokens, call):
        key = _llm_key(method, self.model, system, user, temperature, max_tokens)

        def producer():
            # 锁覆盖真实调用全程：usage 是共享累计计数器，并行时必须串行快照
            with self._call_lock:
                before = self._snapshot_delta()
                value = call()
                after = self._snapshot_delta()
            return {"value": value,
                    "usage": {"prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
                              "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
                              "cached_tokens": after["cached_tokens"] - before["cached_tokens"]}}

        rec = self.store.remember("llm", key, producer,
                                  label=f"{method}:{role_of(system)}")
        self._add_usage(role_of(system), rec.get("usage") or {})
        return json.loads(json.dumps(rec["value"]))

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1,
                  max_tokens: int = 2000, required_keys=None,
                  repair_hint=None):
        return self._run("chat_json", system, user, temperature, max_tokens,
                         lambda: self.real.chat_json(
                             system, user, temperature=temperature,
                             max_tokens=max_tokens,
                             required_keys=required_keys, repair_hint=repair_hint))

    def chat_text(self, system: str, user: str, *, temperature: float = 0.3,
                  max_tokens: int = 1000) -> str:
        return self._run("chat_text", system, user, temperature, max_tokens,
                         lambda: self.real.chat_text(
                             system, user, temperature=temperature, max_tokens=max_tokens))
