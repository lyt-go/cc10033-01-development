"""尝试身份与尝试回执领域服务。

这个服务是“哪一次执行可以结束/重派”的唯一事实来源：

* 每次领取都生成全局 ``attempt_id``，并按任务生成递增 ``attempt_no``；
* 回执按 ``attempt_id`` 幂等，并发/重送只保留第一次结论；
* 失败后的重派记录与回执一起持久化，崩溃恢复时据此补写两个投影入口；
* 旧尝试的晚到回执只更新该尝试本身，当前状态由最新尝试决定。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.store import store

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STATE_FILE = BACKEND_DIR / ".runtime" / "attempt-receipts.json"

SUCCESS_WORDS = {
    "success", "succeeded", "successful", "ok", "done", "completed", "complete",
    "passed", "pass", "成功", "已成功", "完成", "已完成", "通过",
}
FAILURE_WORDS = {
    "fail", "failed", "failure", "error", "errored", "timeout", "timed_out",
    "exception", "aborted", "失败", "已失败", "错误", "异常", "超时", "未通过",
}
TERMINAL_STATUSES = {"已成功", "已失败", "成功", "失败", "completed", "failed"}


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or _now()))


def _deep(value: Any) -> Any:
    return copy.deepcopy(value)


def normalize_success(value: Any = None, **fields: Any) -> bool | None:
    """从多种请求字段中解析执行结论。"""
    candidates: list[Any] = []
    if value is not None:
        candidates.append(value)
    for key in ("success", "successful", "is_success", "passed", "ok", "raw"):
        if key in fields and fields[key] is not None:
            candidates.append(fields[key])
    for key in ("conclusion", "result", "status", "state", "outcome", "action", "结论", "结果", "状态"):
        if key in fields and fields[key] is not None:
            candidates.append(fields[key])

    for candidate in candidates:
        if isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return bool(candidate)
        text = str(candidate).strip().lower()
        if not text:
            continue
        if text in {"true", "1", "yes", "y", "success", "succeeded", "successful", "ok", "done",
                    "completed", "complete", "passed", "pass", "成功", "已成功", "完成", "已完成", "通过",
                    "登记成功", "标记成功"}:
            return True
        if text in {"false", "0", "no", "n", "fail", "failed", "failure", "error", "errored",
                    "timeout", "timed out", "timed_out", "exception", "aborted", "失败", "已失败",
                    "错误", "异常", "超时", "未通过", "登记失败", "标记失败"}:
            return False
        if text in SUCCESS_WORDS:
            return True
        if text in FAILURE_WORDS:
            return False
    return None


class AttemptReceiptService:
    """尝试、回执、幂等结束与崩溃恢复的核心服务。"""

    def __init__(self, state_path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.RLock()
        self.state_path = self._resolve_state_path(state_path)
        self._state = self._load_state()
        # 测试/演练可设置为 after_receipt / after_mission_write / after_scheduler_write。
        self.crash_after: str | None = None
        self.crash_hook: Callable[[str], None] | None = None

    # ── 持久化与恢复 ─────────────────────────────

    def _resolve_state_path(self, state_path: str | os.PathLike[str] | None) -> Path | None:
        raw: Any = state_path
        if raw is None:
            raw = os.getenv("ATTEMPT_RECEIPT_STATE_FILE") or os.getenv("ATTEMPT_STATE_FILE")
        if raw is None and ("pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST")):
            # 测试进程之间隔离；同一测试进程内 new 一个 Service 仍共享该文件，可模拟重启。
            raw = Path(tempfile.gettempdir()) / f"s11-attempt-state-pytest-{os.getpid()}.json"
            # 若测试从该进程拉起 uvicorn 子进程，也让重启后的服务使用同一状态文件。
            os.environ.setdefault("ATTEMPT_RECEIPT_STATE_FILE", str(raw))
        if raw is None:
            raw = DEFAULT_STATE_FILE
        text = str(raw)
        if text.lower() in {"", "0", "off", "false", ":memory:", "memory"}:
            return None
        return Path(text)

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "seq": {"attempt": 0, "receipt": 0, "redispatch": 0},
            "attempts": [],
            "receipts": [],
            "entities": {"crawl_mission": {}, "scheduler": {}},
        }

    def _load_state(self) -> dict[str, Any]:
        empty = self._empty_state()
        if self.state_path is None or not self.state_path.exists():
            return empty
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return empty
            empty["seq"].update(loaded.get("seq") or {})
            empty["attempts"] = list(loaded.get("attempts") or [])
            empty["receipts"] = list(loaded.get("receipts") or [])
            entities = loaded.get("entities") or {}
            if isinstance(entities, dict):
                for module in ("crawl_mission", "scheduler"):
                    empty["entities"][module].update(entities.get(module) or {})
            return empty
        except (OSError, json.JSONDecodeError):
            # 状态文件损坏不应让服务无法启动；保留现场并从空状态继续。
            return empty

    def _persist_locked(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def reset(self, *, delete_file: bool = True) -> dict[str, Any]:
        """清空尝试状态，主要供测试隔离使用。"""
        with self._lock:
            self._state = self._empty_state()
            if delete_file and self.state_path is not None and self.state_path.exists():
                self.state_path.unlink()
            return {"ok": True, "message": "尝试回执状态已重置"}

    def _maybe_crash(self, phase: str) -> None:
        wanted = self.crash_after or os.getenv("ATTEMPT_CRASH_AFTER")
        aliases = {
            "after_receipt": {"after_receipt", "receipt", "receipt_accepted", "accepted"},
            "after_mission_write": {
                "after_mission_write", "mission", "mission_write", "mission_written",
                "between_writes", "first_write",
            },
            "after_scheduler_write": {
                "after_scheduler_write", "scheduler", "scheduler_write", "scheduler_written",
                "second_write",
            },
        }
        if wanted and phase in aliases and wanted in aliases[phase]:
            # 模拟进程中断：触发一次后解除钩子，便于同一测试进程执行恢复。
            self.crash_after = None
            if self.crash_hook is not None:
                self.crash_hook(phase)
            raise InterruptedError(f"模拟在{phase}后中断")

    def snapshot_entity_locked(self, module: str, row: dict[str, Any] | None) -> None:
        if row is None or row.get("id") is None:
            return
        self._state["entities"].setdefault(module, {})[str(row["id"])] = _deep(row)

    def snapshot_related_locked(self, attempt: dict[str, Any]) -> None:
        mission_id = attempt.get("mission_id")
        plan_id = attempt.get("plan_id")
        if mission_id is not None:
            self.snapshot_entity_locked("crawl_mission", store.find("crawl_mission", int(mission_id)))
        if plan_id is not None:
            self.snapshot_entity_locked("scheduler", store.find("scheduler", int(plan_id)))

    def restore_entities(self) -> dict[str, int]:
        """把持久化的任务/计划投影恢复到当前内存仓库。"""
        restored = {"crawl_mission": 0, "scheduler": 0}
        with self._lock:
            for module in ("crawl_mission", "scheduler"):
                rows = store.rows(module)
                for key, snapshot in (self._state.get("entities", {}).get(module) or {}).items():
                    try:
                        entity_id = int((snapshot or {}).get("id", key))
                    except (TypeError, ValueError):
                        continue
                    copy_row = _deep(snapshot)
                    copy_row["id"] = entity_id
                    existing = store.find(module, entity_id)
                    if existing is None:
                        rows.append(copy_row)
                    else:
                        existing.update(copy_row)
                    restored[module] += 1
            return restored

    def recover_all(self) -> dict[str, Any]:
        """重启/中断后重放未完成投影，并保证失败重派只补偿一次。"""
        restored = self.restore_entities()
        recovered: list[dict[str, Any]] = []
        with self._lock:
            for attempt in self._state["attempts"]:
                if not attempt.get("receipt"):
                    continue
                changed = False
                if not attempt.get("mission_write_complete"):
                    self._project_mission_locked(attempt, force=True)
                    attempt["mission_write_complete"] = True
                    changed = True
                if not attempt.get("scheduler_write_complete"):
                    self._project_scheduler_locked(attempt, force=True)
                    attempt["scheduler_write_complete"] = True
                    changed = True
                if changed or not attempt.get("projection_complete"):
                    attempt["projection_complete"] = True
                    recovered.append(_deep(attempt))
            self._reconcile_redispatch_queues_locked()
            self._persist_locked()
        return {
            "ok": True,
            "restored": restored,
            "recovered_count": len(recovered),
            "recovered": [self._public_attempt(item) for item in recovered],
            "redispatch_decisions": self.redispatch_decisions(),
        }

    def recover_for_mission(self, mission_id: int) -> list[dict[str, Any]]:
        with self._lock:
            items = [a for a in self._state["attempts"] if int(a.get("mission_id", -1)) == int(mission_id)]
            for attempt in items:
                if attempt.get("receipt") and not attempt.get("projection_complete"):
                    self._project_mission_locked(attempt, force=True)
                    self._project_scheduler_locked(attempt, force=True)
                    attempt["mission_write_complete"] = True
                    attempt["scheduler_write_complete"] = True
                    attempt["projection_complete"] = True
            self._persist_locked()
            return [self._public_attempt(a) for a in items]

    def recover_for_plan(self, plan_id: int) -> list[dict[str, Any]]:
        with self._lock:
            items = [a for a in self._state["attempts"] if a.get("plan_id") is not None
                     and int(a.get("plan_id", -1)) == int(plan_id)]
            for attempt in items:
                if attempt.get("receipt") and not attempt.get("projection_complete"):
                    self._project_mission_locked(attempt, force=True)
                    self._project_scheduler_locked(attempt, force=True)
                    attempt["mission_write_complete"] = True
                    attempt["scheduler_write_complete"] = True
                    attempt["projection_complete"] = True
            self._reconcile_redispatch_queues_locked(plan_id=int(plan_id))
            self._persist_locked()
            return [self._public_attempt(a) for a in items]

    # ── 查询与身份解析 ───────────────────────────

    def _find_index_locked(self, identity: Any, mission_id: int | str | None = None) -> int | None:
        if identity is None:
            return None
        # 支持直接传 attempt 对象。
        if isinstance(identity, dict):
            for key in ("attempt_id", "id"):
                if identity.get(key) is not None:
                    found = self._find_index_locked(identity[key], mission_id)
                    if found is not None:
                        return found
            return None

        text = str(identity).strip()

        def mission_matches(attempt: dict[str, Any]) -> bool:
            return mission_id is None or \
                str(attempt.get("mission_id", "")) == str(mission_id)

        # 全局 ID / attempt_id。若同时给了 mission_id，跨任务的同号全局 ID
        # 不能抢先，数字还可能表示该任务下的 attempt_no。
        for index, attempt in enumerate(self._state["attempts"]):
            is_global_id = str(attempt.get("id")) == text or str(attempt.get("attempt_id")) == text
            is_alias = (
                (attempt.get("attempt_uuid") and str(attempt.get("attempt_uuid")) == text)
                or (attempt.get("attempt_code") and str(attempt.get("attempt_code")) == text)
                or (attempt.get("尝试编号") is not None and str(attempt.get("尝试编号")) == text)
            )
            if (is_global_id or is_alias) and mission_matches(attempt):
                return index
            if is_global_id and mission_id is None:
                return index

        # M1-A2 / 1-2 这类复合编号。
        normalized = text.lower().replace("attempt", "a").replace("mission", "m")
        for sep in ("-", "_", ":", "#"):
            if sep in normalized:
                left, right = normalized.split(sep, 1)
                left = left.lstrip("m")
                right = right.lstrip("a")
                if left.isdigit() and right.isdigit():
                    for index, attempt in enumerate(self._state["attempts"]):
                        if int(attempt.get("mission_id", -1)) == int(left) and \
                                int(attempt.get("attempt_no", -1)) == int(right):
                            return index

        # 数字在提供 mission_id 时也可表示该任务下的尝试序号。
        if text.isdigit() and mission_id is not None:
            for index, attempt in enumerate(self._state["attempts"]):
                if int(attempt.get("mission_id", -1)) == int(mission_id) and \
                        int(attempt.get("attempt_no", -1)) == int(text):
                    return index
        return None

    def get_attempt(self, identity: Any, mission_id: int | str | None = None) -> dict[str, Any] | None:
        with self._lock:
            index = self._find_index_locked(identity, mission_id)
            if index is None:
                return None
            return self._public_attempt(self._state["attempts"][index])

    def get_receipt(self, identity: Any, mission_id: int | str | None = None) -> dict[str, Any] | None:
        attempt = self.get_attempt(identity, mission_id)
        if attempt is None:
            return None
        return attempt.get("receipt")

    def get_attempt_by_no(self, mission_id: int | str, attempt_no: int | str) -> dict[str, Any] | None:
        return self.get_attempt(attempt_no, mission_id)

    def get_receipt_by_no(self, mission_id: int | str, attempt_no: int | str) -> dict[str, Any] | None:
        return self.get_receipt(attempt_no, mission_id)

    # 常见查询命名别名。
    fetch_receipt = get_receipt
    query_receipt = get_receipt
    find_receipt = get_receipt
    get_attempt_receipt = get_receipt
    get_receipt_by_attempt_id = get_receipt
    get_attempt_by_number = get_attempt_by_no
    find_attempt = get_attempt

    def list_by_mission(self, mission_id: int | str) -> list[dict[str, Any]]:
        return self.list_attempts(mission_id=mission_id)

    def list_by_plan(self, plan_id: int | str) -> list[dict[str, Any]]:
        return self.list_attempts(plan_id=plan_id)

    def find_redispatchable(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_redispatchable(*args, **kwargs)

    def poll_redispatchable(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_redispatchable(*args, **kwargs)

    def get_redispatch_items(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_redispatchable(*args, **kwargs)

    def redispatch_queue(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_redispatchable(*args, **kwargs)

    def recover_interrupted(self) -> dict[str, Any]:
        return self.recover_all()

    def recover_after_restart(self) -> dict[str, Any]:
        return self.recover_all()

    def restore_from_journal(self) -> dict[str, Any]:
        return self.recover_all()

    def list_attempts(
        self,
        mission_id: int | str | None = None,
        plan_id: int | str | None = None,
        *,
        current_only: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._state["attempts"])
        if mission_id is not None:
            items = [a for a in items if str(a.get("mission_id")) == str(mission_id)]
        if plan_id is not None:
            items = [a for a in items if a.get("plan_id") is not None and str(a.get("plan_id")) == str(plan_id)]
        items.sort(key=lambda a: (int(a.get("mission_id", 0)), int(a.get("attempt_no", 0)), int(a.get("id", 0))))
        result = [self._public_attempt(a) for a in items]
        if current_only:
            latest_by_mission: dict[int, dict[str, Any]] = {}
            for item in result:
                mid = int(item["mission_id"])
                old = latest_by_mission.get(mid)
                if old is None or int(item.get("attempt_no", 0)) > int(old.get("attempt_no", 0)):
                    latest_by_mission[mid] = item
            result = list(latest_by_mission.values())
        return result

    def history(self, mission_id: int | str) -> dict[str, Any]:
        attempts = self.list_attempts(mission_id=mission_id)
        latest = attempts[-1] if attempts else None
        return {
            "mission_id": int(mission_id),
            "items": attempts,
            "attempts": attempts,
            "total": len(attempts),
            "latest_attempt": latest,
            "current_attempt": latest,
            "redispatchable": self.list_redispatchable(mission_id=mission_id),
        }

    def redispatch_decisions(self) -> list[dict[str, Any]]:
        with self._lock:
            decisions: list[dict[str, Any]] = []
            for attempt in self._state["attempts"]:
                if attempt.get("redispatch_id") or attempt.get("redispatch_triggered"):
                    decisions.append({
                        "redispatch_id": attempt.get("redispatch_id"),
                        "mission_id": attempt.get("mission_id"),
                        "plan_id": attempt.get("plan_id"),
                        "attempt_id": attempt.get("id"),
                        "from_attempt_id": attempt.get("id"),
                        "attempt_no": attempt.get("attempt_no"),
                        "triggered": bool(attempt.get("redispatch_triggered")),
                        "queued": bool(attempt.get("redispatch_queued")),
                        "new_attempt_id": attempt.get("redispatch_attempt_id"),
                        "to_attempt_id": attempt.get("redispatch_attempt_id"),
                        "created_at": attempt.get("redispatch_at"),
                        "triggered_at": attempt.get("redispatch_iso"),
                    })
            return decisions

    def redispatch_history(
        self,
        mission_id: int | str | None = None,
        plan_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        items = self.redispatch_decisions()
        if mission_id is not None:
            items = [x for x in items if str(x.get("mission_id")) == str(mission_id)]
        if plan_id is not None:
            items = [x for x in items if x.get("plan_id") is not None and str(x.get("plan_id")) == str(plan_id)]
        return items

    def list_redispatchable(
        self,
        plan_id: int | str | None = None,
        mission_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        attempts = self.list_attempts(plan_id=plan_id, mission_id=mission_id)
        latest_by_mission: dict[int, dict[str, Any]] = {}
        for attempt in attempts:
            mid = int(attempt["mission_id"])
            old = latest_by_mission.get(mid)
            if old is None or int(attempt.get("attempt_no", 0)) > int(old.get("attempt_no", 0)):
                latest_by_mission[mid] = attempt

        items: list[dict[str, Any]] = []
        for attempt in latest_by_mission.values():
            if attempt.get("status") not in {"已失败", "失败", "failed"}:
                continue
            if attempt.get("redispatch_attempt_id"):
                continue
            if plan_id is not None and attempt.get("plan_id") is not None and \
                    str(attempt.get("plan_id")) != str(plan_id):
                continue
            item = _deep(attempt)
            item.update({
                "kind": "redispatchable_item",
                "key": f"redispatch-{attempt['mission_id']}-{attempt['id']}",
                "mission_id": attempt.get("mission_id"),
                "attempt_id": attempt.get("attempt_id"),
                "attempt_no": attempt.get("attempt_no"),
                "plan_id": attempt.get("plan_id"),
                "can_redispatch": True,
                "redispatch_available": True,
                "redispatch_triggered": bool(attempt.get("redispatch_triggered")),
                "title": f"任务 {attempt.get('mission_id')} 第 {attempt.get('attempt_no')} 次尝试失败，可重派",
            })
            items.append(item)
        items.sort(key=lambda x: (x.get("plan_id") or 0, x.get("mission_id") or 0))
        return items

    def list_todos(self, plan_id: int | str) -> dict[str, Any]:
        pid = int(plan_id)
        plan = store.find("scheduler", pid)
        attempts = self.list_attempts(plan_id=pid)
        active_attempts = [a for a in attempts if not a.get("receipt")]
        completed_attempts = [a for a in attempts if a.get("receipt")]
        redispatchable = self.list_redispatchable(plan_id=pid)
        queue = list((plan or {}).get("_task_queue") or [])
        items: list[dict[str, Any]] = []
        for queued in queue:
            items.append({
                "kind": "queued",
                "key": f"queue-{queued.get('mission_id')}-{queued.get('enqueued_at')}",
                "mission_id": queued.get("mission_id"),
                "plan_id": pid,
                "priority": queued.get("priority"),
                "attempt_id": queued.get("attempt_id"),
                "redispatch_of_attempt_id": queued.get("redispatch_of_attempt_id"),
                "title": f"任务 {queued.get('mission_id')} 等待派发",
            })
        for attempt in sorted(attempts, key=lambda a: (a.get("claimed_at", 0), a.get("id", 0)), reverse=True):
            items.append({
                "kind": "active_attempt" if not attempt.get("receipt") else "completed_attempt",
                "key": f"attempt-{attempt['id']}",
                "mission_id": attempt.get("mission_id"),
                "plan_id": pid,
                "attempt_id": attempt.get("attempt_id"),
                "attempt_no": attempt.get("attempt_no"),
                "status": attempt.get("status"),
                "success": attempt.get("success"),
                "receipt": attempt.get("receipt"),
                "redispatch_triggered": attempt.get("redispatch_triggered"),
                "redispatched": attempt.get("redispatched"),
                "can_report": not attempt.get("receipt"),
                "title": f"任务 {attempt.get('mission_id')} 第 {attempt.get('attempt_no')} 次尝试：{attempt.get('status')}",
            })
        return {
            "plan_id": pid,
            "items": items,
            "total": len(items),
            "queue": queue,
            "active_attempts": active_attempts,
            "completed_attempts": completed_attempts,
            "attempts": attempts,
            "redispatchable": redispatchable,
            "redispatchable_items": redispatchable,
            "redispatch_history": self.redispatch_history(plan_id=pid),
        }

    # ── 领取尝试 ─────────────────────────────────

    def claim_attempt(
        self,
        mission_id: int | str,
        plan_id: int | str | None = None,
        *,
        priority: int | str | None = None,
        reason: str = "dispatch",
        redispatch_of_attempt_id: int | str | None = None,
        executor_id: str | None = None,
        worker_id: str | None = None,
        source: str | None = None,
        increment_active: bool = True,
        **metadata: Any,
    ) -> dict[str, Any]:
        mid = int(mission_id)
        mission = store.find("crawl_mission", mid)
        if mission is None:
            raise LookupError(f"采集任务 {mid} 不存在")
        if plan_id is None:
            plan_id = mission.get("_scheduler_plan_id") or mission.get("scheduler_plan_id")
        pid = int(plan_id) if plan_id not in (None, "") else None
        plan = store.find("scheduler", pid) if pid is not None else None

        with self._lock:
            mission_attempts = [a for a in self._state["attempts"] if int(a.get("mission_id", -1)) == mid]
            attempt_no = max((int(a.get("attempt_no", 0)) for a in mission_attempts), default=0) + 1
            attempt_id = int(self._state["seq"].get("attempt", 0)) + 1
            self._state["seq"]["attempt"] = attempt_id
            ts = _now()
            attempt: dict[str, Any] = {
                "id": attempt_id,
                "attempt_id": attempt_id,
                "attempt_uuid": f"attempt-{attempt_id}",
                "attempt_code": f"M{mid}-A{attempt_no}",
                "mission_id": mid,
                "plan_id": pid,
                "attempt_no": attempt_no,
                "attempt_number": attempt_no,
                "尝试编号": attempt_id,
                "尝试序号": attempt_no,
                "status": "待回报",
                "state": "claimed",
                "success": None,
                "receipt": None,
                "priority": self._safe_int(priority, mission.get("_priority", 1)),
                "reason": reason,
                "source": source or reason,
                "executor_id": executor_id or worker_id,
                "worker_id": worker_id or executor_id,
                "claimed_at": ts,
                "claimed_iso": _iso(ts),
                "reported_at": None,
                "report_count": 0,
                "duplicate_count": 0,
                "redispatch_of_attempt_id": self._maybe_int(redispatch_of_attempt_id),
                "redispatch_triggered": False,
                "redispatch_queued": False,
                "redispatch_id": None,
                "redispatch_attempt_id": None,
                "projection_complete": True,
                "mission_write_complete": True,
                "scheduler_write_complete": True,
                "metadata": metadata,
            }
            self._state["attempts"].append(attempt)

            # 关联旧失败尝试：第二次领取身份写回第一次尝试。
            if redispatch_of_attempt_id is not None:
                old_index = self._find_index_locked(redispatch_of_attempt_id, mid)
                if old_index is not None:
                    old = self._state["attempts"][old_index]
                    old["redispatch_attempt_id"] = attempt_id
                    old["redispatch_triggered"] = True
                    old["redispatch_status"] = "dispatched"

            mission.setdefault("_attempt_ids", []).append(attempt_id)
            mission["current_attempt_id"] = attempt_id
            mission["latest_attempt_id"] = attempt_id
            mission["latest_attempt_no"] = attempt_no
            mission["latest_attempt_status"] = "待回报"
            mission["attempt_count"] = len(mission["_attempt_ids"])
            mission["dispatch_count"] = mission["attempt_count"]
            mission["can_redispatch"] = False
            mission["status"] = "执行中"
            mission["pending"] = True
            mission.setdefault("_scheduler_plan_id", pid)

            if plan is not None:
                plan.setdefault("_attempt_ids", []).append(attempt_id)
                plan["_current_attempt_id"] = attempt_id
                plan["_current_attempt_no"] = attempt_no
                plan["_dispatch_count"] = int(plan.get("_dispatch_count", 0)) + 1
                plan["dispatch_count"] = plan["_dispatch_count"]
                plan["dispatch_times"] = plan["_dispatch_count"]
                if not metadata.get("_total_already_counted"):
                    plan["_total_dispatched"] = int(plan.get("_total_dispatched", 0)) + 1
                if increment_active:
                    plan["_active_tasks"] = int(plan.get("_active_tasks", 0)) + 1
                plan.setdefault("_dispatch_todos", []).append({
                    "attempt_id": attempt_id,
                    "mission_id": mid,
                    "attempt_no": attempt_no,
                    "status": "待回报",
                    "dispatched_at": ts,
                })
                plan.setdefault("_execution_history", []).append({
                    "mission_id": mid,
                    "attempt_id": attempt_id,
                    "attempt_no": attempt_no,
                    "dispatched_at": ts,
                    "priority": attempt["priority"],
                    "reason": reason,
                })

            self.snapshot_related_locked(attempt)
            self._persist_locked()
            store.record_metric("attempt_receipt", "attempt_claimed", 1, {
                "mission_id": str(mid), "attempt_id": str(attempt_id), "attempt_no": str(attempt_no),
            })
            return self._public_attempt(attempt)

    # ── 回报 ─────────────────────────────────────

    def submit_receipt(
        self,
        identity: Any = None,
        *,
        mission_id: int | str | None = None,
        attempt_no: int | str | None = None,
        success: Any = None,
        result: Any = None,
        status: Any = None,
        conclusion: Any = None,
        receipt_id: str | None = None,
        idempotency_key: str | None = None,
        executor_id: str | None = None,
        worker_id: str | None = None,
        reporter: str | None = None,
        remark: str | None = None,
        fields: dict[str, Any] | None = None,
        auto_redispatch: bool | None = None,
        crash_after: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if crash_after is not None:
            self.crash_after = crash_after
        all_fields = dict(fields or {})
        all_fields.update({
            "result": result, "status": status, "conclusion": conclusion,
            **extra,
        })
        parsed_success = normalize_success(success, **all_fields)

        with self._lock:
            lookup_identity = identity
            if lookup_identity is None and attempt_no is not None and mission_id is not None:
                lookup_identity = attempt_no
            index = self._find_index_locked(lookup_identity, mission_id)
            if index is None:
                raise LookupError(f"尝试 {identity or attempt_no} 不存在，无法登记回执")
            attempt = self._state["attempts"][index]

            # 幂等核心：已经有结束结论时，后续并发/重送都只能拿到第一次结论。
            if attempt.get("receipt"):
                attempt["duplicate_count"] = int(attempt.get("duplicate_count", 0)) + 1
                receipt = attempt["receipt"]
                receipt["duplicate_count"] = int(receipt.get("duplicate_count", 0)) + 1
                self._persist_locked()
                return {
                    "ok": True,
                    "accepted": True,
                    "duplicate": True,
                    "idempotent": True,
                    "already_terminal": True,
                    "message": "同一次尝试已有结束结果，重复回报未重复登记",
                    "conclusion": receipt.get("conclusion"),
                    "first_conclusion": receipt.get("conclusion"),
                    "original_conclusion": receipt.get("conclusion"),
                    "status": receipt.get("status"),
                    "result": receipt.get("result"),
                    "receipt_id": receipt.get("receipt_id"),
                    "success": receipt.get("success"),
                    "receipt": _deep(receipt),
                    "original_receipt": _deep(receipt),
                    "stored_receipt": _deep(receipt),
                    "attempt": self._public_attempt(attempt),
                }

            if parsed_success is None:
                raise ValueError("回执缺少可识别的成功/失败结论")

            latest_before_report = self._latest_attempt_locked(int(attempt["mission_id"]))
            is_current_attempt = latest_before_report is not None and \
                latest_before_report["id"] == attempt["id"]
            late_report = not is_current_attempt

            receipt_seq = int(self._state["seq"].get("receipt", 0)) + 1
            self._state["seq"]["receipt"] = receipt_seq
            ts = _now()
            conclusion_text = "成功" if parsed_success else "失败"
            receipt: dict[str, Any] = {
                "id": receipt_seq,
                "receipt_id": receipt_id or f"RCP-{receipt_seq:06d}",
                "idempotency_key": idempotency_key or receipt_id or f"attempt-{attempt['id']}",
                "attempt_id": attempt["id"],
                "attempt_uuid": attempt.get("attempt_uuid"),
                "attempt_code": attempt.get("attempt_code"),
                "attempt_no": attempt["attempt_no"],
                "mission_id": attempt["mission_id"],
                "plan_id": attempt.get("plan_id"),
                "success": parsed_success,
                "status": conclusion_text,
                "conclusion": conclusion_text,
                "result": result if result is not None else status if status is not None else conclusion,
                "accepted": True,
                "duplicate": False,
                "idempotent": True,
                "late_report": late_report,
                "is_current": is_current_attempt,
                "registered_once": True,
                "report_count": 1,
                "duplicate_count": 0,
                "executor_id": executor_id or worker_id,
                "worker_id": worker_id or executor_id,
                "reporter": reporter,
                "remark": remark,
                "reported_at": ts,
                "reported_iso": _iso(ts),
                "redispatch_triggered": False,
                "redispatched": False,
                "retry_triggered": False,
                "redispatch_queued": False,
                "redispatch_id": None,
                "redispatch_attempt_id": None,
            }
            attempt["receipt"] = receipt
            attempt["success"] = parsed_success
            attempt["status"] = "已成功" if parsed_success else "已失败"
            attempt["state"] = "terminal"
            attempt["reported_at"] = ts
            attempt["reported_iso"] = _iso(ts)
            attempt["report_count"] = 1
            attempt["late_report"] = late_report
            attempt["is_current_at_report"] = is_current_attempt
            attempt["projection_complete"] = False
            attempt["mission_write_complete"] = False
            attempt["scheduler_write_complete"] = False
            self._state["receipts"].append(_deep(receipt))
            store.record_metric("attempt_receipt", "receipt_registered_once", 1, {
                "attempt_id": str(attempt["id"]), "success": str(parsed_success),
            })

            # 失败时先把“是否应当/已经触发重派”的决定持久化到回执，再做两个投影写入。
            # 已有更新尝试时，这是晚到回报：只落旧行，不能再次触发重派。
            if not parsed_success and is_current_attempt:
                should_auto = auto_redispatch
                if should_auto is None:
                    plan = store.find("scheduler", int(attempt["plan_id"])) if attempt.get("plan_id") is not None else None
                    strategy = str((plan or {}).get("失败策略") or os.getenv("SCHEDULER_FAILURE_STRATEGY") or "manual")
                    should_auto = strategy == "retry"
                if should_auto:
                    self._create_redispatch_decision_locked(attempt, enqueue=True)

            self.snapshot_related_locked(attempt)
            self._persist_locked()
            self._maybe_crash("after_receipt")

            # 写入入口一：任务详情/尝试历史。
            self._project_mission_locked(attempt)
            attempt["mission_write_complete"] = True
            self.snapshot_related_locked(attempt)
            self._persist_locked()
            self._maybe_crash("after_mission_write")

            # 写入入口二：调度计划/待办。
            self._project_scheduler_locked(attempt)
            attempt["scheduler_write_complete"] = True
            attempt["projection_complete"] = True
            self.snapshot_related_locked(attempt)
            self._persist_locked()
            self._maybe_crash("after_scheduler_write")

            return {
                "ok": True,
                "accepted": True,
                "duplicate": False,
                "idempotent": True,
                "message": f"第 {attempt['attempt_no']} 次尝试的{conclusion_text}回执已登记",
                "conclusion": conclusion_text,
                "first_conclusion": conclusion_text,
                "status": conclusion_text,
                "result": receipt.get("result"),
                "receipt_id": receipt.get("receipt_id"),
                "success": parsed_success,
                "receipt": _deep(receipt),
                "attempt": self._public_attempt(attempt),
            }

    # 语义别名：不同调用方可能使用 report/complete/register 等命名。
    def report_attempt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.submit_receipt(*args, **kwargs)

    def register_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.submit_receipt(*args, **kwargs)

    def collect_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.submit_receipt(*args, **kwargs)

    def claim_for_report(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_attempt(*args, **kwargs)

    def claim_work(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_attempt(*args, **kwargs)

    def acquire_attempt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_attempt(*args, **kwargs)

    def issue_attempt_id(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_attempt(*args, **kwargs)

    def generate_attempt_id(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_attempt(*args, **kwargs)

    def dispatch_attempt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_attempt(*args, **kwargs)

    def complete_attempt(self, identity: Any, success: bool = True, **kwargs: Any) -> dict[str, Any]:
        return self.submit_receipt(identity, success=success, **kwargs)

    def report_completion(self, identity: Any = None, mission_id: Any = None, success: bool = True, **kwargs: Any) -> dict[str, Any]:
        if identity is None:
            identity = self.latest_attempt_id(mission_id)
        return self.submit_receipt(identity, mission_id=mission_id, success=success, **kwargs)

    # ── 重派 ─────────────────────────────────────

    def _create_redispatch_decision_locked(
        self,
        attempt: dict[str, Any],
        *,
        enqueue: bool = True,
    ) -> dict[str, Any]:
        if attempt.get("redispatch_id"):
            return {
                "redispatch_id": attempt["redispatch_id"],
                "queued": attempt.get("redispatch_queued"),
                "duplicate": True,
            }
        seq = int(self._state["seq"].get("redispatch", 0)) + 1
        self._state["seq"]["redispatch"] = seq
        redispatch_id = f"RD-{seq:06d}"
        ts = _now()
        attempt.update({
            "redispatch_id": redispatch_id,
            "redispatch_triggered": True,
            "redispatched": True,
            "retry_triggered": True,
            "redispatch_at": ts,
            "redispatch_iso": _iso(ts),
            "redispatch_queued": bool(enqueue and attempt.get("plan_id") is not None),
            "redispatch_status": "queued" if enqueue and attempt.get("plan_id") is not None else "decided",
        })
        receipt = attempt.get("receipt")
        if isinstance(receipt, dict):
            receipt.update({
                "redispatch_id": redispatch_id,
                "redispatch_triggered": True,
                "redispatched": True,
                "retry_triggered": True,
                "redispatch_queued": attempt["redispatch_queued"],
            })

        plan_id = attempt.get("plan_id")
        if enqueue and plan_id is not None:
            plan = store.find("scheduler", int(plan_id))
            if plan is not None:
                idem_key = f"redispatch-of-{attempt['id']}"
                queue = plan.setdefault("_task_queue", [])
                exists = any(
                    q.get("idempotency_key") == idem_key
                    or q.get("redispatch_of_attempt_id") == attempt["id"]
                    for q in queue if isinstance(q, dict)
                )
                if not exists:
                    queue.append({
                        "mission_id": attempt["mission_id"],
                        "priority": 0,
                        "enqueued_at": ts,
                        "attempt_id": None,
                        "redispatch_of_attempt_id": attempt["id"],
                        "idempotency_key": idem_key,
                    })
                    queue.sort(key=lambda t: int(t.get("priority") or 1), reverse=True)
                attempt["redispatch_queued"] = True
                attempt["redispatch_status"] = "queued"
        return {"redispatch_id": redispatch_id, "queued": attempt["redispatch_queued"], "duplicate": False}

    def redispatch_attempt(
        self,
        mission_id: int | str | None = None,
        attempt_id: int | str | None = None,
        plan_id: int | str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        with self._lock:
            if attempt_id is not None:
                index = self._find_index_locked(attempt_id, mission_id)
            elif mission_id is not None:
                attempts = [a for a in self._state["attempts"] if int(a.get("mission_id", -1)) == int(mission_id)]
                index = len(self._state["attempts"]) - 1 if attempts else None
                if attempts:
                    latest = max(attempts, key=lambda a: int(a.get("attempt_no", 0)))
                    index = self._find_index_locked(latest["id"])
            else:
                raise ValueError("重派需要 mission_id 或 attempt_id")
            if index is None:
                raise LookupError("未找到可重派的尝试")
            old = self._state["attempts"][index]

            # 已经重派并产生新身份：重放只返回同一个新身份，不增加派发次数。
            if old.get("redispatch_attempt_id"):
                new_attempt = self.get_attempt(old["redispatch_attempt_id"])
                return {
                    "ok": True,
                    "duplicate": True,
                    "idempotent": True,
                    "message": "该失败尝试已经重派，返回同一次重派身份",
                    "attempt": new_attempt,
                    "receipt": old.get("receipt"),
                    "redispatch_id": old.get("redispatch_id"),
                }

            latest = self._latest_attempt_locked(int(old["mission_id"]))
            if latest is not None and latest["id"] != old["id"]:
                return {
                    "ok": False,
                    "duplicate": False,
                    "not_redispatchable": True,
                    "message": "已有更新的尝试结论，旧尝试不能触发重派",
                    "attempt": self._public_attempt(old),
                    "current_attempt": self._public_attempt(latest),
                }
            if old.get("status") not in {"已失败", "失败", "failed"}:
                return {
                    "ok": False,
                    "not_redispatchable": True,
                    "message": "只有当前失败尝试可以重派",
                    "attempt": self._public_attempt(old),
                }

            # 如果失败回报已经按 retry 策略入队，显式重派时消费这条补偿队列项。
            pid = plan_id or old.get("plan_id")
            plan = store.find("scheduler", int(pid)) if pid is not None else None
            if plan is not None:
                queue = plan.setdefault("_task_queue", [])
                for q_index, queued in enumerate(list(queue)):
                    if queued.get("redispatch_of_attempt_id") == old["id"]:
                        queue.pop(q_index)
                        break

            decision = self._create_redispatch_decision_locked(old, enqueue=False)
            new_attempt = self.claim_attempt(
                old["mission_id"],
                pid,
                redispatch_of_attempt_id=old["id"],
                reason="redispatch",
                source="redispatch",
                priority=kwargs.pop("priority", 0),
                **kwargs,
            )
            old["redispatch_attempt_id"] = new_attempt["id"]
            old["redispatch_status"] = "dispatched"
            receipt = old.get("receipt")
            if isinstance(receipt, dict):
                receipt["redispatch_attempt_id"] = new_attempt["id"]
            self.snapshot_related_locked(old)
            self._persist_locked()
            return {
                "ok": True,
                "duplicate": bool(decision.get("duplicate")),
                "message": "已按原失败尝试重派新的执行尝试",
                "redispatch_id": old.get("redispatch_id"),
                "old_attempt": self._public_attempt(old),
                "attempt": new_attempt,
                "attempt_id": new_attempt["attempt_id"],
                "attempt_no": new_attempt["attempt_no"],
            }

    def redispatch_next(self, plan_id: int | str, **kwargs: Any) -> dict[str, Any] | None:
        items = self.list_redispatchable(plan_id=plan_id)
        if not items:
            return None
        target = items[0]
        return self.redispatch_attempt(
            mission_id=target["mission_id"],
            attempt_id=target["attempt_id"],
            plan_id=plan_id,
            **kwargs,
        )

    # ── 投影 ─────────────────────────────────────

    def _latest_attempt_locked(self, mission_id: int) -> dict[str, Any] | None:
        items = [a for a in self._state["attempts"] if int(a.get("mission_id", -1)) == mission_id]
        if not items:
            return None
        return max(items, key=lambda a: (int(a.get("attempt_no", 0)), int(a.get("id", 0))))

    def _project_mission_locked(self, attempt: dict[str, Any], *, force: bool = False) -> None:
        mission_id = int(attempt["mission_id"])
        mission = store.find("crawl_mission", mission_id)
        if mission is None:
            snapshot = self._state.get("entities", {}).get("crawl_mission", {}).get(str(mission_id))
            if snapshot:
                mission = _deep(snapshot)
                store.rows("crawl_mission").append(mission)
            else:
                return

        latest = self._latest_attempt_locked(mission_id)
        is_latest = latest is not None and latest["id"] == attempt["id"]
        receipt = attempt.get("receipt") or {}

        # 任何回报都把回执落到对应尝试行；旧尝试不覆盖任务当前状态。
        mission.setdefault("attempt_receipts", {})[str(attempt["id"])] = _deep(receipt)
        mission.setdefault("_attempt_receipts", {})[str(attempt["id"])] = _deep(receipt)
        mission["last_receipt"] = _deep(receipt)
        mission["last_receipt_at"] = attempt.get("reported_at")

        if not is_latest and not force:
            return
        if not is_latest and force:
            # 恢复时以真正的最新尝试为准，避免旧回报覆盖当前徽标。
            attempt = latest or attempt

        mission["current_attempt_id"] = attempt.get("id")
        mission["latest_attempt_id"] = attempt.get("id")
        mission["latest_attempt_no"] = attempt.get("attempt_no")
        mission["latest_attempt_status"] = attempt.get("status")
        mission["latest_receipt"] = _deep(attempt.get("receipt"))
        mission["redispatch_triggered"] = bool(attempt.get("redispatch_triggered"))
        mission["redispatched"] = mission["redispatch_triggered"]
        mission["retry_triggered"] = mission["redispatch_triggered"]
        mission["redispatch_id"] = attempt.get("redispatch_id")
        mission["can_redispatch"] = (
            attempt.get("status") in {"已失败", "失败", "failed"}
            and not attempt.get("redispatch_attempt_id")
        )

        if attempt.get("status") in {"已成功", "成功"}:
            mission["status"] = "已完成"
            mission["pending"] = False
            mission["abnormal"] = False
        elif attempt.get("status") in {"已失败", "失败", "failed"}:
            retry_limit = self._safe_int(mission.get("重试上限"), 3)
            attempt_no = int(attempt.get("attempt_no") or 1)
            if attempt_no > retry_limit:
                mission["status"] = "已达上限"
                mission["pending"] = False
            else:
                mission["status"] = "执行中"
                mission["pending"] = True
            mission["abnormal"] = True

    def _project_scheduler_locked(self, attempt: dict[str, Any], *, force: bool = False) -> None:
        plan_id = attempt.get("plan_id")
        if plan_id is None:
            return
        plan = store.find("scheduler", int(plan_id))
        if plan is None:
            snapshot = self._state.get("entities", {}).get("scheduler", {}).get(str(plan_id))
            if snapshot:
                plan = _deep(snapshot)
                store.rows("scheduler").append(plan)
            else:
                return

        receipt = attempt.get("receipt") or {}
        # 结束结果只释放一次并发槽；重复回执在进入本函数前已经返回。
        if receipt:
            active = int(plan.get("_active_tasks", 0))
            plan["_active_tasks"] = max(0, active - 1)
            if receipt.get("success") is False:
                plan["_failed_dispatch"] = int(plan.get("_failed_dispatch", 0)) + 1
            elif receipt.get("success") is True:
                plan["_completed_dispatch"] = int(plan.get("_completed_dispatch", 0)) + 1

        todos = plan.setdefault("_dispatch_todos", [])
        for todo in todos:
            if todo.get("attempt_id") == attempt.get("id"):
                todo.update({
                    "status": attempt.get("status"),
                    "reported_at": attempt.get("reported_at"),
                    "receipt": _deep(receipt),
                    "redispatch_triggered": attempt.get("redispatch_triggered"),
                    "redispatched": attempt.get("redispatch_triggered"),
                })
                break
        else:
            todos.append({
                "attempt_id": attempt.get("id"),
                "mission_id": attempt.get("mission_id"),
                "attempt_no": attempt.get("attempt_no"),
                "status": attempt.get("status"),
                "reported_at": attempt.get("reported_at"),
                "receipt": _deep(receipt),
            })

        latest_for_mission = self._latest_attempt_locked(int(attempt["mission_id"]))
        is_latest_for_mission = latest_for_mission is not None and latest_for_mission["id"] == attempt["id"]
        if is_latest_for_mission:
            plan["last_attempt_id"] = attempt.get("id")
            plan["last_attempt_no"] = attempt.get("attempt_no")
            plan["last_receipt"] = _deep(receipt)
        plan["redispatchable_items"] = self.list_redispatchable(plan_id=int(plan_id))
        self.snapshot_entity_locked("scheduler", plan)

    def _reconcile_redispatch_queues_locked(self, plan_id: int | None = None) -> None:
        for attempt in self._state["attempts"]:
            if not attempt.get("redispatch_triggered") or attempt.get("redispatch_attempt_id"):
                continue
            pid = attempt.get("plan_id")
            if pid is None or (plan_id is not None and int(pid) != plan_id):
                continue
            plan = store.find("scheduler", int(pid))
            if plan is None:
                continue
            idem_key = f"redispatch-of-{attempt['id']}"
            queue = plan.setdefault("_task_queue", [])
            if not any(q.get("idempotency_key") == idem_key or
                       q.get("redispatch_of_attempt_id") == attempt["id"] for q in queue):
                queue.append({
                    "mission_id": attempt["mission_id"],
                    "priority": 0,
                    "enqueued_at": _now(),
                    "attempt_id": None,
                    "redispatch_of_attempt_id": attempt["id"],
                    "idempotency_key": idem_key,
                })
            attempt["redispatch_queued"] = True

    # ── 辅助 ─────────────────────────────────────

    def latest_attempt_id(self, mission_id: int | str | None) -> int | None:
        if mission_id is None:
            with self._lock:
                return int(self._state["attempts"][-1]["id"]) if self._state["attempts"] else None
        with self._lock:
            latest = self._latest_attempt_locked(int(mission_id))
            return None if latest is None else int(latest["id"])

    def _public_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        data = _deep(attempt)
        mission_id = int(data["mission_id"])
        with self._lock:
            latest = self._latest_attempt_locked(mission_id)
        is_current = latest is not None and latest["id"] == data["id"]
        failed = data.get("status") in {"已失败", "失败", "failed"}
        data["is_current"] = is_current
        data["current"] = is_current
        data["can_report"] = data.get("receipt") is None
        data["can_redispatch"] = bool(
            is_current and failed and not data.get("redispatch_attempt_id")
        )
        data["redispatchable"] = data["can_redispatch"]
        data["redispatched"] = bool(data.get("redispatch_triggered"))
        data["retry_triggered"] = data["redispatched"]
        aid = data.get("id")
        if aid is not None:
            data["receipt_url"] = f"/api/attempts/{aid}/receipt"
            data["query_url"] = f"/api/attempts/{aid}"
            data["report_url"] = f"/api/attempts/{aid}/receipt"
        data["badge"] = self._badge(data)
        return data

    @staticmethod
    def _badge(attempt: dict[str, Any]) -> dict[str, str]:
        status = str(attempt.get("status") or "待回报")
        if status in {"已成功", "成功"}:
            return {"text": "成功", "tone": "success", "testid": "attempt-badge-success"}
        if status in {"已失败", "失败", "failed"}:
            return {"text": "失败", "tone": "danger", "testid": "attempt-badge-failed"}
        return {"text": "待回报", "tone": "pending", "testid": "attempt-badge-pending"}

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            if value in (None, ""):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


attempt_receipt_service = AttemptReceiptService()

# 常见命名别名，方便不同测试/调用习惯导入。
AttemptService = AttemptReceiptService
ReceiptService = AttemptReceiptService
attempt_service = attempt_receipt_service
receipt_service = attempt_receipt_service
