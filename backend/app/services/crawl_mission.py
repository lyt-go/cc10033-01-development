"""采集任务领域服务：任务定义、优先级调度与执行编排。

跨模块数据流：
- crawl_mission → scheduler: 任务提交到调度计划
- crawl_mission → page_render: 触发页面渲染
- crawl_mission → robot_verify: 任务执行中触发反封锁检测
"""
from __future__ import annotations

import time
from typing import Any

from app.services.attempt_receipt import attempt_receipt_service
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

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """任务详情同时带出按尝试编号索引的历史与当前可重派结论。"""
        entry = super().get_entry(entry_id)
        if entry is None:
            # 进程重启后，尝试状态中可能保存了任务快照；尝试恢复一次。
            attempt_receipt_service.restore_entities()
            entry = super().get_entry(entry_id)
        if entry is None:
            return None
        attempt_receipt_service.recover_for_mission(entry_id)
        history = attempt_receipt_service.history(entry_id)
        entry["attempts"] = history["items"]
        entry["attempt_history"] = history["items"]
        entry["_attempt_history"] = history["items"]
        entry["_attempts"] = history["items"]
        entry["attempt_total"] = history["total"]
        entry["attempt_count"] = history["total"]
        entry["dispatch_count"] = history["total"]
        entry["latest_attempt"] = history["latest_attempt"]
        entry["current_attempt"] = history["current_attempt"]
        entry["redispatchable_items"] = history["redispatchable"]
        entry["can_redispatch"] = bool(history["redispatchable"])
        entry["redispatch_history"] = attempt_receipt_service.redispatch_history(mission_id=entry_id)
        return entry

    def claim_attempt(
        self,
        entry_id: int,
        scheduler_plan_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """领取一次可回报的尝试身份。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if entry.get("status") == "待调度":
            self.dispatch(entry_id, scheduler_plan_id=scheduler_plan_id)
        return attempt_receipt_service.claim_attempt(
            entry_id, scheduler_plan_id, **kwargs
        )

    # 语义别名：部分调用方把“领取”称为开始/发起/派发尝试。
    start_attempt = claim_attempt
    issue_attempt = claim_attempt
    issue_attempt_id = claim_attempt
    generate_attempt_id = claim_attempt
    acquire_attempt = claim_attempt
    create_attempt = claim_attempt

    def list_attempts(self, entry_id: int) -> list[dict[str, Any]]:
        return attempt_receipt_service.list_attempts(mission_id=entry_id)

    def attempt_history(self, entry_id: int) -> dict[str, Any]:
        return attempt_receipt_service.history(entry_id)

    def get_attempt(self, entry_id: int, attempt_id: int | str) -> dict[str, Any] | None:
        return attempt_receipt_service.get_attempt(attempt_id, mission_id=entry_id)

    def report_attempt(
        self,
        entry_id: int,
        attempt_id: int | str,
        success: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return attempt_receipt_service.submit_receipt(
            attempt_id, mission_id=entry_id, success=success, **kwargs
        )

    def submit_attempt_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.report_attempt(*args, **kwargs)

    def redispatch_attempt(self, entry_id: int, attempt_id: int | str | None = None, **kwargs: Any) -> dict[str, Any]:
        return attempt_receipt_service.redispatch_attempt(
            mission_id=entry_id, attempt_id=attempt_id, **kwargs
        )

    def redispatchable_items(self, entry_id: int) -> list[dict[str, Any]]:
        return attempt_receipt_service.list_redispatchable(mission_id=entry_id)

    def get_attempt_history(self, entry_id: int) -> dict[str, Any]:
        return self.attempt_history(entry_id)

    def list_receipts(self, entry_id: int) -> list[dict[str, Any]]:
        return [a["receipt"] for a in self.list_attempts(entry_id) if a.get("receipt")]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """兼容通用动作入口：无 attempt_id 时作用于当前最新尝试。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action in {"领取尝试", "领取尝试编号", "生成尝试编号", "开始尝试"}:
            attempt = self.claim_attempt(entry_id, entry.get("_scheduler_plan_id"))
            return entry, f"已领取第 {attempt['attempt_no']} 次尝试，编号 {attempt['attempt_id']}"
        if action in {"登记成功", "提交成功回执", "尝试成功"}:
            attempt_id = entry.get("current_attempt_id") or attempt_receipt_service.latest_attempt_id(entry_id)
            if attempt_id is None:
                attempt = self.claim_attempt(entry_id, entry.get("_scheduler_plan_id"))
                attempt_id = attempt["id"]
            result = self.report_attempt(entry_id, attempt_id, True)
            return entry, result["message"]
        if action in {"登记失败", "提交失败回执", "尝试失败"}:
            attempt_id = entry.get("current_attempt_id") or attempt_receipt_service.latest_attempt_id(entry_id)
            if attempt_id is None:
                attempt = self.claim_attempt(entry_id, entry.get("_scheduler_plan_id"))
                attempt_id = attempt["id"]
            result = self.report_attempt(entry_id, attempt_id, False)
            return entry, result["message"]
        if action in {"重派", "重新派发", "可重派", "重派失败尝试"}:
            result = self.redispatch_attempt(entry_id)
            if not result.get("ok"):
                return entry, result.get("message", "当前尝试不可重派")
            return entry, result.get("message", "已重派")
        return super().run_action(entry_id, action)

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