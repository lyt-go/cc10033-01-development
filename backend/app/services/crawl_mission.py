"""采集任务领域服务：任务定义、优先级调度与执行编排。

跨模块数据流：
- crawl_mission → scheduler: 任务提交到调度计划
- crawl_mission → page_render: 触发页面渲染
- crawl_mission → robot_verify: 任务执行中触发反封锁检测
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CrawlMissionService(BaseModuleService):
    MODULE = "crawl_mission"
    ENTITY = "采集任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "目标URL", "优先级", "重试上限", "超时秒数", "渲染策略", "所属项目", "创建时间"]
    REQUIRED_FIELDS = ["任务编号", "目标URL", "优先级"]
    STATUS_ORDER = ["待调度", "执行中", "已完成", "已达上限"]
    ACTION_RULES = {"发起调度": "执行中", "标记完成": "已完成", "标记上限": "已达上限"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        priority = int(values.get("优先级", 1))
        entry["_priority"] = priority
        entry["_created_ts"] = time.time()
        # 自动创建关联的调度事件
        store.enqueue_event("mission_created", {
            "mission_id": entry["id"],
            "target_url": values.get("目标URL", ""),
            "priority": priority,
        })
        store.record_metric("crawl_mission", "mission_created", 1,
                            {"priority": str(priority)})
        return entry, []

    def dispatch(self, entry_id: int, scheduler_plan_id: int | None = None) -> dict[str, Any] | None:
        """将采集任务提交到调度计划执行。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if entry.get("status") != "待调度":
            return entry
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["_dispatch_ts"] = time.time()
        if scheduler_plan_id:
            store.pipeline_trace(
                "crawl_mission", "scheduler", entry_id, scheduler_plan_id,
                "dispatch", {"url": entry.get("目标URL", "")}
            )
        store.record_metric("crawl_mission", "mission_dispatched", 1,
                            {"url": entry.get("目标URL", ""), "priority": str(entry.get("_priority", 1))})
        store.enqueue_event("mission_dispatched", {
            "mission_id": entry_id,
            "scheduler_plan_id": scheduler_plan_id,
            "ts": time.time(),
        })
        return entry

    def retry_mission(self, entry_id: int) -> dict[str, Any] | None:
        """重试已达到上限的任务，重置计数器。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        retry_limit = int(entry.get("重试上限", 3))
        retry_count = entry.get("_retry_count", 0)
        if retry_count >= retry_limit:
            entry["status"] = "已达上限"
            entry["abnormal"] = True
            store.record_metric("crawl_mission", "retry_limit_reached", 1,
                                {"mission_id": str(entry_id)})
            return entry
        entry["_retry_count"] = retry_count + 1
        entry["status"] = "执行中"
        entry["pending"] = True
        store.record_metric("crawl_mission", "mission_retried", 1,
                            {"attempt": str(entry["_retry_count"])})
        return entry

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        missions = store.rows(self.MODULE)
        base["avg_retry"] = (
            sum(m.get("_retry_count", 0) for m in missions) / max(len(missions), 1)
        )
        base["total_dispatched"] = sum(
            1 for m in missions if m.get("_dispatch_ts")
        )
        return base