"""代理节点领域服务：节点注册、健康检测、负载分配与降级策略。

跨模块数据流：
- proxy_node → rate_limit: 代理分配时关联限速策略，确保IP不超频
- proxy_node → robot_verify: 代理被封禁时自动上报反封锁检测
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ProxyNodeService(BaseModuleService):
    MODULE = "proxy_node"
    ENTITY = "代理节点"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["节点编号", "IP地址", "端口号", "代理类型", "地理位置", "响应延迟", "成功率", "并发上限"]
    REQUIRED_FIELDS = ["节点编号", "IP地址", "端口号"]
    STATUS_ORDER = ["待验证", "可用", "降级", "已禁用"]
    ACTION_RULES = {"验证节点": "可用", "标记降级": "降级", "禁用节点": "已禁用"}

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_latency_history"] = []
        entry["_success_count"] = 0
        entry["_fail_count"] = 0
        entry["_last_check_ts"] = time.time()
        # 自动关联频率控制
        store.enqueue_event("proxy_registered", {
            "proxy_id": entry["id"],
            "ip": values.get("IP地址", ""),
            "port": values.get("端口号", ""),
        })
        store.record_metric("proxy_node", "node_registered", 1,
                            {"type": values.get("代理类型", "unknown")})
        return entry, []

    def health_check(self, entry_id: int, latency_ms: float) -> dict[str, Any] | None:
        """健康检测：记录延迟，动态调整状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_latency_history"].append(latency_ms)
        entry["_last_check_ts"] = time.time()
        entry["响应延迟"] = f"{latency_ms:.0f}ms"

        # 历史延迟分析
        history = entry["_latency_history"]
        if len(history) >= 3:
            avg_latency = sum(history[-3:]) / 3
            if avg_latency > 5000:
                entry["status"] = "降级"
                entry["abnormal"] = True
                store.record_metric("proxy_node", "node_degraded", 1,
                                    {"latency": str(avg_latency)})
                return entry
        entry["status"] = "可用"
        entry["_success_count"] += 1
        self._update_success_rate(entry)
        store.record_metric("proxy_node", "health_check_ok", 1,
                            {"latency": str(latency_ms)})
        return entry

    def assign_to_mission(self, entry_id: int, mission_id: int, rate_limit_id: int | None = None) -> dict[str, Any] | None:
        """为采集任务分配代理节点，关联限速策略。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "可用":
            return None
        entry["_assigned_mission"] = mission_id
        if rate_limit_id:
            store.pipeline_trace(
                "proxy_node", "rate_limit", entry_id, rate_limit_id,
                "assign", {"mission_id": mission_id}
            )
        store.pipeline_trace(
            "crawl_mission", "proxy_node", mission_id, entry_id,
            "proxy_assigned", {"ip": entry.get("IP地址", "")}
        )
        store.record_metric("proxy_node", "node_assigned", 1,
                            {"mission_id": str(mission_id)})
        return entry

    def rotate_on_ban(self, entry_id: int, robot_check_id: int) -> dict[str, Any] | None:
        """代理被封禁时切换节点。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "禁用" if entry.get("_ban_count", 0) >= 2 else "降级"
        entry["_ban_count"] = entry.get("_ban_count", 0) + 1
        entry["_fail_count"] += 1
        self._update_success_rate(entry)
        store.pipeline_trace(
            "proxy_node", "robot_verify", entry_id, robot_check_id,
            "banned", {"ban_count": entry["_ban_count"]}
        )
        store.record_metric("proxy_node", "node_banned", 1,
                            {"ban_count": str(entry["_ban_count"])})
        return entry

    def _update_success_rate(self, entry: dict[str, Any]) -> None:
        total = entry["_success_count"] + entry["_fail_count"]
        entry["成功率"] = f"{(entry['_success_count'] / max(total, 1) * 100):.1f}%"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        nodes = store.rows(self.MODULE)
        base["available_nodes"] = sum(1 for n in nodes if n.get("status") == "可用")
        base["degraded_nodes"] = sum(1 for n in nodes if n.get("status") == "降级")
        base["total_assigned"] = sum(1 for n in nodes if n.get("_assigned_mission"))
        return base