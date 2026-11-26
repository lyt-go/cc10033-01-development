"""调度计划领域服务：采集任务定时调度、并发控制与失败处理。

跨模块数据流：
- scheduler ← crawl_mission: 任务提交到调度计划
- scheduler ← seed_url: URL种子关联调度计划
- scheduler → scalable_pool: 负载预测通知弹性伸缩
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.attempt_receipt import attempt_receipt_service
from app.services.base import BaseModuleService
from app.store import store


class SchedulerService(BaseModuleService):
    MODULE = "scheduler"
    ENTITY = "调度计划"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["计划编号", "Cron表达式", "关联任务组", "并发上限", "优先级策略", "失败策略", "下次执行", "启用状态"]
    REQUIRED_FIELDS = ["计划编号", "Cron表达式", "关联任务组"]
    STATUS_ORDER = ["草稿", "待验证", "运行中", "已暂停"]
    ACTION_RULES = {"提交验证": "待验证", "启动计划": "运行中", "暂停计划": "已暂停"}

    STRATEGIES = ["round_robin", "priority_first", "earliest_deadline", "shortest_job", "resource_aware"]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is None:
            attempt_receipt_service.restore_entities()
            entry = super().get_entry(entry_id)
        if entry is None:
            return None
        attempt_receipt_service.recover_for_plan(entry_id)
        entry["attempts"] = attempt_receipt_service.list_attempts(plan_id=entry_id)
        entry["_attempts"] = entry["attempts"]
        entry["_attempt_history"] = entry["attempts"]
        entry["dispatch_count"] = int(entry.get("dispatch_count", entry.get("_dispatch_count", 0)))
        entry["dispatch_times"] = entry["dispatch_count"]
        entry["total_dispatched"] = int(entry.get("_total_dispatched", 0))
        entry["dispatch_todos"] = attempt_receipt_service.list_todos(entry_id)["items"]
        entry["_dispatch_todos"] = entry["dispatch_todos"]
        entry["scheduler_todos"] = entry["dispatch_todos"]
        entry["_scheduler_todos"] = entry["dispatch_todos"]
        entry["todos"] = entry["dispatch_todos"]
        entry["redispatchable_items"] = attempt_receipt_service.list_redispatchable(plan_id=entry_id)
        entry["redispatchable"] = entry["redispatchable_items"]
        entry["redispatch_history"] = attempt_receipt_service.redispatch_history(plan_id=entry_id)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_task_queue"] = []
        entry["_execution_history"] = []
        entry["_total_dispatched"] = 0
        entry["_failed_dispatch"] = 0
        store.record_metric("scheduler", "plan_created", 1,
                            {"cron": values.get("Cron表达式", "")})
        return entry, []

    def enqueue_mission(self, entry_id: int, mission_id: int, priority: int = 1) -> dict[str, Any] | None:
        """将采集任务加入调度队列。跨模块：crawl_mission → scheduler"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") not in ("运行中", "待验证"):
            return None
        entry["_task_queue"].append({
            "mission_id": mission_id,
            "priority": priority,
            "enqueued_at": time.time(),
        })
        entry["_task_queue"].sort(key=lambda t: t["priority"], reverse=True)
        store.pipeline_trace(
            "crawl_mission", "scheduler", mission_id, entry_id,
            "enqueue", {"priority": priority}
        )
        store.record_metric("scheduler", "mission_enqueued", 1,
                            {"priority": str(priority)})
        return entry

    def dispatch_next(self, entry_id: int) -> dict[str, Any] | None:
        """按优先调度下一个任务，并生成可用于回报的尝试编号。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or not entry["_task_queue"]:
            return None
        next_task = entry["_task_queue"].pop(0)
        entry["_total_dispatched"] += 1
        entry["下次执行"] = time.strftime("%Y-%m-%d %H:%M:%S")
        max_concurrent = self._parse_int(entry.get("并发上限", "10"))
        active = entry.get("_active_tasks", 0)
        if active >= max_concurrent:
            store.record_metric("scheduler", "dispatch_blocked_concurrency", 1,
                                {"active": str(active), "limit": str(max_concurrent)})
            return None

        mission_id = next_task["mission_id"]
        attempt = attempt_receipt_service.claim_attempt(
            mission_id,
            entry_id,
            priority=next_task.get("priority"),
            reason="redispatch" if next_task.get("redispatch_of_attempt_id") else "dispatch",
            redispatch_of_attempt_id=next_task.get("redispatch_of_attempt_id"),
            increment_active=True,
            _total_already_counted=True,
        )
        # claim_attempt 已追加历史；这里保留旧版调度历史的指标语义。
        store.record_metric("scheduler", "mission_dispatched", 1,
                            {"mission_id": str(mission_id), "attempt_id": str(attempt["id"])})
        result = dict(next_task)
        result.update(attempt)
        result["mission_id"] = mission_id
        return result

    def claim_next(self, entry_id: int) -> dict[str, Any] | None:
        """dispatch_next 的语义别名：领取下一个可执行尝试。"""
        return self.dispatch_next(entry_id)

    def claim_attempt(self, entry_id: int, mission_id: int, **kwargs: Any) -> dict[str, Any]:
        return attempt_receipt_service.claim_attempt(mission_id, entry_id, **kwargs)

    claim_next_attempt = claim_next
    claim_work = claim_next

    def report_attempt(
        self,
        entry_id: int,
        attempt_id: int | str,
        success: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if "plan_id" not in kwargs:
            kwargs["plan_id"] = entry_id
        return attempt_receipt_service.submit_receipt(attempt_id, success=success, **kwargs)

    def report_completion(
        self,
        entry_id: int,
        mission_id: int,
        success: bool,
        attempt_id: int | str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """任务完成回执；同一尝试的重复回报由尝试服务幂等处理。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if attempt_id is None:
            attempt_id = entry.get("_current_attempt_id") or \
                attempt_receipt_service.latest_attempt_id(mission_id)
        if attempt_id is None:
            # 兼容没有显式领取尝试的旧调用：补建第一次尝试。
            attempt = attempt_receipt_service.claim_attempt(mission_id, entry_id)
            attempt_id = attempt["id"]

        result = attempt_receipt_service.submit_receipt(
            attempt_id,
            mission_id=mission_id,
            success=success,
            **kwargs,
        )
        # 旧契约：失败策略为 retry 时入队。新服务已在回执中幂等写入重派决定，
        # 这里仅做兼容补偿；重复回报不会再次入队。
        if success is False and not result.get("duplicate"):
            fail_strategy = entry.get("失败策略", "retry")
            if fail_strategy == "retry":
                attempt_receipt_service._create_redispatch_decision_locked(  # noqa: SLF001
                    attempt_receipt_service._state["attempts"][  # noqa: SLF001
                        attempt_receipt_service._find_index_locked(attempt_id, mission_id)  # noqa: SLF001
                    ],
                    enqueue=True,
                )
                attempt_receipt_service._persist_locked()  # noqa: SLF001
                store.record_metric("scheduler", "mission_requeued_for_retry", 1,
                                    {"mission_id": str(mission_id)})
        return result

    def list_attempts(self, entry_id: int) -> list[dict[str, Any]]:
        return attempt_receipt_service.list_attempts(plan_id=entry_id)

    def list_todos(self, entry_id: int) -> dict[str, Any]:
        return attempt_receipt_service.list_todos(entry_id)

    def scheduler_todos(self, entry_id: int) -> dict[str, Any]:
        return self.list_todos(entry_id)

    def list_redispatchable(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return attempt_receipt_service.list_redispatchable(plan_id=entry_id)

    def get_redispatch_items(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return self.list_redispatchable(entry_id)

    def poll_redispatchable(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return self.list_redispatchable(entry_id)

    def redispatch_queue(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return self.list_redispatchable(entry_id)

    def redispatch_next(self, entry_id: int) -> dict[str, Any] | None:
        return attempt_receipt_service.redispatch_next(entry_id)

    def recover_attempts(self, entry_id: int | None = None) -> dict[str, Any]:
        if entry_id is None:
            return attempt_receipt_service.recover_all()
        return {"items": attempt_receipt_service.recover_for_plan(entry_id)}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action in {"派发下一项", "领取下一项", "派发并领取尝试编号"}:
            task = self.dispatch_next(entry_id)
            if task is None:
                return entry, "调度队列为空或并发上限已满"
            return entry, f"已派发，尝试编号 {task.get('attempt_id')}"
        if action in {"重派下一项", "查询可重派项并重派", "重派"}:
            result = self.redispatch_next(entry_id)
            if result is None:
                return entry, "当前没有可重派项"
            return entry, result.get("message", "已重派")
        if action in {"登记成功", "登记失败"}:
            attempt_id = entry.get("_current_attempt_id")
            if attempt_id is None:
                return entry, "当前没有可回报的尝试"
            result = self.report_attempt(entry_id, attempt_id, action == "登记成功")
            return entry, result["message"]
        return super().run_action(entry_id, action)

    def predict_load(self, entry_id: int) -> dict[str, Any]:
        """预测未来负载，供弹性伸缩决策。跨模块：scheduler → scalable_pool"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"predicted_load": 0}
        queue_size = len(entry["_task_queue"])
        active = entry.get("_active_tasks", 0)
        hourly_rate = active * 2 + queue_size * 0.5
        prediction = {
            "current_load": active,
            "queue_depth": queue_size,
            "predicted_load_next_hour": hourly_rate,
            "recommend_scale": "up" if hourly_rate > 20 else "stable" if hourly_rate > 5 else "down",
        }
        store.record_metric("scheduler", "load_prediction", hourly_rate,
                            {"recommendation": prediction["recommend_scale"]})
        return prediction

    def _parse_int(self, val: Any) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            return 10

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        plans = store.rows(self.MODULE)
        base["active_plans"] = sum(1 for p in plans if p.get("status") == "运行中")
        base["total_dispatched"] = sum(p.get("_total_dispatched", 0) for p in plans)
        base["queue_depth"] = sum(len(p.get("_task_queue", [])) for p in plans)
        base["active_tasks"] = sum(p.get("_active_tasks", 0) for p in plans)
        return base