"""频率控制领域服务：策略定义、自动触发与冷却管理。

跨模块数据流：
- rate_limit ← proxy_node: 代理分配时检查限速策略
- rate_limit → crawl_mission: 限速触发时暂停关联任务
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RateLimitService(BaseModuleService):
    MODULE = "rate_limit"
    ENTITY = "限速策略"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["策略编号", "目标域名", "请求间隔", "并发连接数", "日请求上限", "冷却时间", "限速方式", "最近触发"]
    REQUIRED_FIELDS = ["策略编号", "目标域名", "请求间隔"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已暂停"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "暂停策略": "已暂停"}

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_request_count"] = 0
        entry["_window_start"] = time.time()
        entry["_last_request_ts"] = 0
        entry["_cooldown_until"] = 0
        store.record_metric("rate_limit", "policy_created", 1,
                            {"domain": values.get("目标域名", "")})
        return entry, []

    def check_allow(self, entry_id: int, domain: str) -> tuple[bool, str]:
        """检查是否可以发送请求。跨模块调用：proxy_node 分配代理前检查。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return True, "策略不存在，放行"
        if entry.get("status") != "已生效":
            return True, "策略未生效，放行"
        now = time.time()
        if 0 < entry["_cooldown_until"] and now < entry["_cooldown_until"]:
            remain = entry["_cooldown_until"] - now
            return False, f"冷却中，剩余{remain:.0f}秒"
        interval = self._parse_seconds(entry.get("请求间隔", "1s"))
        if now - entry["_last_request_ts"] < interval:
            return False, f"请求间隔不足({interval}s)"
        daily_limit = entry.get("日请求上限")
        if daily_limit:
            limit_val = int(daily_limit) if str(daily_limit).isdigit() else 10000
            window = now - entry["_window_start"]
            if window > 86400:
                entry["_window_start"] = now
                entry["_request_count"] = 0
            if entry["_request_count"] >= limit_val:
                return False, "已达日请求上限"
        return True, "放行"

    def record_request(self, entry_id: int) -> None:
        """记录一次请求，更新计数器。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return
        entry["_request_count"] += 1
        entry["_last_request_ts"] = time.time()
        entry["最近触发"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def trigger_cooldown(self, entry_id: int, duration_seconds: float, mission_id: int | None = None) -> dict[str, Any] | None:
        """触发冷却，暂停关联的采集任务。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_cooldown_until"] = time.time() + duration_seconds
        entry["最近触发"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if mission_id:
            store.pipeline_trace(
                "rate_limit", "crawl_mission", entry_id, mission_id,
                "cooldown_triggered", {"duration": duration_seconds}
            )
        store.record_metric("rate_limit", "cooldown_triggered", 1,
                            {"duration": str(duration_seconds)})
        return entry

    def _parse_seconds(self, s: str) -> float:
        s = str(s).strip().lower()
        multipliers = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
        for unit, mul in multipliers.items():
            if s.endswith(unit):
                try:
                    return float(s[:-len(unit)]) * mul
                except ValueError:
                    pass
        try:
            return float(s)
        except ValueError:
            return 1.0

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        policies = store.rows(self.MODULE)
        base["active_policies"] = sum(1 for p in policies if p.get("status") == "已生效")
        base["total_requests"] = sum(p.get("_request_count", 0) for p in policies)
        return base