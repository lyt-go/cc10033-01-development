"""弹性伸缩领域服务：采集Worker弹性伸缩策略与资源调度。

跨模块数据流：
- scalable_pool ← scheduler: 负载预测触发弹性伸缩决策
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ScalablePoolService(BaseModuleService):
    MODULE = "scalable_pool"
    ENTITY = "伸缩策略"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["策略编号", "Worker类型", "最小实例数", "最大实例数", "扩容阈值", "缩容阈值", "冷却时间", "当前实例数"]
    REQUIRED_FIELDS = ["策略编号", "Worker类型", "最小实例数"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    WORKER_TYPES = ["crawler", "renderer", "parser", "exporter", "monitor"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_current_instances"] = self._parse_int(values.get("最小实例数", "2"))
        entry["当前实例数"] = str(entry["_current_instances"])
        entry["_scale_history"] = []
        entry["_last_scale_ts"] = 0
        entry["_total_scale_ups"] = 0
        entry["_total_scale_downs"] = 0
        store.record_metric("scalable_pool", "pool_created", 1,
                            {"worker_type": values.get("Worker类型", "unknown"),
                             "min": values.get("最小实例数", "")})
        return entry, []

    def evaluate(self, entry_id: int, load_prediction: dict[str, Any] | None = None) -> dict[str, Any]:
        """评估负载，决定是否需要扩缩容。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"action": "none", "reason": "策略未生效"}
        min_instances = self._parse_int(entry.get("最小实例数", "2"))
        max_instances = self._parse_int(entry.get("最大实例数", "20"))
        scale_up_threshold = float(entry.get("扩容阈值") or 0.7)
        scale_down_threshold = float(entry.get("缩容阈值") or 0.3)
        current = entry["_current_instances"]
        predicted_load = load_prediction.get("predicted_load_next_hour", current) if load_prediction else current
        load_ratio = predicted_load / max(max_instances, 1)
        if load_ratio > scale_up_threshold and current < max_instances:
            return self._scale_up(entry_id, entry, min_instances, max_instances)
        if load_ratio < scale_down_threshold and current > min_instances:
            return self._scale_down(entry_id, entry, min_instances, max_instances)
        return {"action": "stable", "current_instances": current,
                "load_ratio": round(load_ratio, 3)}

    def _scale_up(self, entry_id: int, entry: dict[str, Any],
                  min_inst: int, max_inst: int) -> dict[str, Any]:
        cooldown = self._parse_cooldown(entry.get("冷却时间", "300"))
        now = time.time()
        if now - entry["_last_scale_ts"] < cooldown:
            return {"action": "cooldown", "current_instances": entry["_current_instances"]}
        new_count = min(entry["_current_instances"] + random.randint(1, 3), max_inst)
        old_count = entry["_current_instances"]
        entry["_current_instances"] = new_count
        entry["当前实例数"] = str(new_count)
        entry["_last_scale_ts"] = now
        entry["_total_scale_ups"] += 1
        entry["_scale_history"].append({
            "ts": now, "action": "scale_up",
            "from": old_count, "to": new_count,
        })
        store.pipeline_trace(
            "scheduler", "scalable_pool", -1, entry_id,
            "scale_up", {"from": old_count, "to": new_count}
        )
        store.record_metric("scalable_pool", "scaled_up", new_count - old_count,
                            {"worker_type": entry.get("Worker类型", ""),
                             "from": str(old_count), "to": str(new_count)})
        return {"action": "scale_up", "from": old_count, "to": new_count,
                "current_instances": new_count}

    def _scale_down(self, entry_id: int, entry: dict[str, Any],
                    min_inst: int, max_inst: int) -> dict[str, Any]:
        cooldown = self._parse_cooldown(entry.get("冷却时间", "300"))
        now = time.time()
        if now - entry["_last_scale_ts"] < cooldown:
            return {"action": "cooldown", "current_instances": entry["_current_instances"]}
        new_count = max(entry["_current_instances"] - random.randint(1, 2), min_inst)
        old_count = entry["_current_instances"]
        entry["_current_instances"] = new_count
        entry["当前实例数"] = str(new_count)
        entry["_last_scale_ts"] = now
        entry["_total_scale_downs"] += 1
        entry["_scale_history"].append({
            "ts": now, "action": "scale_down",
            "from": old_count, "to": new_count,
        })
        store.record_metric("scalable_pool", "scaled_down", old_count - new_count,
                            {"worker_type": entry.get("Worker类型", ""),
                             "from": str(old_count), "to": str(new_count)})
        return {"action": "scale_down", "from": old_count, "to": new_count,
                "current_instances": new_count}

    def _parse_int(self, val: Any) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            return 2

    def _parse_cooldown(self, val: Any) -> int:
        try:
            v = str(val).strip().lower()
            if v.endswith("s"):
                return int(v[:-1])
            if v.endswith("m"):
                return int(v[:-1]) * 60
            return int(float(val))
        except (ValueError, TypeError):
            return 300

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        pools = store.rows(self.MODULE)
        base["active_pools"] = sum(1 for p in pools if p.get("status") == "已生效")
        base["total_instances"] = sum(p.get("_current_instances", 0) for p in pools)
        base["total_scale_ups"] = sum(p.get("_total_scale_ups", 0) for p in pools)
        base["total_scale_downs"] = sum(p.get("_total_scale_downs", 0) for p in pools)
        return base