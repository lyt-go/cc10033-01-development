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
        """按优先调度下一个任务。"""
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
        entry["_active_tasks"] = active + 1
        entry["_execution_history"].append({
            "mission_id": next_task["mission_id"],
            "dispatched_at": time.time(),
            "priority": next_task["priority"],
        })
        store.record_metric("scheduler", "mission_dispatched", 1,
                            {"mission_id": str(next_task["mission_id"])})
        return next_task

    def report_completion(self, entry_id: int, mission_id: int, success: bool) -> None:
        """任务完成回执。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return
        entry["_active_tasks"] = max(0, entry.get("_active_tasks", 0) - 1)
        if not success:
            entry["_failed_dispatch"] += 1
            fail_strategy = entry.get("失败策略", "retry")
            if fail_strategy == "retry":
                self.enqueue_mission(entry_id, mission_id, priority=0)
                store.record_metric("scheduler", "mission_requeued_for_retry", 1,
                                    {"mission_id": str(mission_id)})

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