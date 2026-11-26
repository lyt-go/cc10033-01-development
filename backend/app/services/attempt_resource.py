"""把尝试回执暴露为通用资源路由可识别的只读/登记服务。"""
from __future__ import annotations

from typing import Any

from app.services.attempt_receipt import attempt_receipt_service
from app.services.base import BaseModuleService


class AttemptReceiptResourceService(BaseModuleService):
    MODULE = "attempt_receipt"
    ENTITY = "尝试回执"
    LIST_FIELDS = [
        "attempt_id", "attempt_no", "mission_id", "plan_id",
        "success", "conclusion", "status", "receipt_id", "reported_iso",
    ]
    REQUIRED_FIELDS = ["attempt_id"]
    STATUS_ORDER = ["待回报", "已成功", "已失败"]

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        mission_id = (filters or {}).get("mission_id")
        plan_id = (filters or {}).get("plan_id")
        attempt_id = (filters or {}).get("attempt_id")
        if attempt_id:
            receipt = attempt_receipt_service.get_receipt(attempt_id, mission_id)
            items = [receipt] if receipt else []
        else:
            attempts = attempt_receipt_service.list_attempts(mission_id=mission_id, plan_id=plan_id)
            items = [a.get("receipt") for a in attempts if a.get("receipt")]
        if status:
            items = [item for item in items if item and str(item.get("status")) == status]
        total = len(items)
        start = (page - 1) * size
        return items[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        # 数字 id 既可能是回执 id，也可能是尝试 id；优先按尝试编号查询。
        receipt = attempt_receipt_service.get_receipt(entry_id)
        if receipt is not None:
            return receipt
        for item in attempt_receipt_service._state["receipts"]:  # noqa: SLF001
            if int(item.get("id", -1)) == int(entry_id):
                return item
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        attempt_id = values.get("attempt_id") or values.get("尝试编号")
        if attempt_id is None:
            return None, ["attempt_id"]
        result = attempt_receipt_service.submit_receipt(
            attempt_id,
            mission_id=values.get("mission_id"),
            success=values.get("success", values.get("conclusion") or values.get("status")),
            **{k: v for k, v in values.items() if k not in {"attempt_id", "尝试编号", "mission_id"}},
        )
        return result.get("receipt"), []


class AttemptResourceService(AttemptReceiptResourceService):
    MODULE = "attempt"
    ENTITY = "尝试身份"
    LIST_FIELDS = ["attempt_id", "attempt_no", "mission_id", "plan_id", "status"]
    REQUIRED_FIELDS = ["mission_id"]

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        filters = kwargs.get("filters") or {}
        items = attempt_receipt_service.list_attempts(
            mission_id=filters.get("mission_id"), plan_id=filters.get("plan_id")
        )
        return items, len(items)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return attempt_receipt_service.get_attempt(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        mission_id = values.get("mission_id") or values.get("entry_id")
        if mission_id is None:
            return None, ["mission_id"]
        attempt = attempt_receipt_service.claim_attempt(
            mission_id, values.get("plan_id") or values.get("scheduler_plan_id")
        )
        return attempt, []
