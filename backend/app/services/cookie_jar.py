"""Cookie管理领域服务：Cookie容器生命周期、登录态保持与过期管理。

跨模块数据流：
- cookie_jar ← fingerprint: 绑定设备指纹建立身份关联
- cookie_jar → crawl_mission: Cookie过期时通知任务暂停
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CookieJarService(BaseModuleService):
    MODULE = "cookie_jar"
    ENTITY = "Cookie容器"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["容器编号", "所属账号", "域名范围", "过期时间", "Cookie数量", "登录状态", "最后刷新", "关联指纹"]
    REQUIRED_FIELDS = ["容器编号", "所属账号", "域名范围"]
    STATUS_ORDER = ["有效", "临近过期", "已过期", "已失效"]
    ACTION_RULES = {"刷新Cookie": "有效", "标记过期": "已过期", "标记失效": "已失效"}

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_cookies"] = {}
        entry["_expire_ts"] = time.time() + 3600  # 默认1小时
        entry["_refresh_count"] = 0
        store.record_metric("cookie_jar", "jar_created", 1,
                            {"account": values.get("所属账号", "")})
        return entry, []

    def bind_fingerprint(self, entry_id: int, fingerprint_id: int) -> dict[str, Any] | None:
        """绑定设备指纹，建立身份关联。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["关联指纹"] = f"FING-{fingerprint_id:04d}"
        entry["_fingerprint_id"] = fingerprint_id
        store.pipeline_trace(
            "fingerprint", "cookie_jar", fingerprint_id, entry_id,
            "bind", {"account": entry.get("所属账号", "")}
        )
        return entry

    def set_cookie(self, entry_id: int, name: str, value: str, expires_in: int = 3600) -> dict[str, Any] | None:
        """设置单个Cookie。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_cookies"][name] = {
            "value": value,
            "expires_at": time.time() + expires_in,
            "set_at": time.time(),
        }
        entry["Cookie数量"] = len(entry["_cookies"])
        entry["最后刷新"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return entry

    def refresh_cookies(self, entry_id: int, new_cookies: dict[str, str]) -> dict[str, Any] | None:
        """刷新全部Cookie，重置过期时间。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        now = time.time()
        for name, value in new_cookies.items():
            entry["_cookies"][name] = {"value": value, "expires_at": now + 3600, "set_at": now}
        entry["Cookie数量"] = len(entry["_cookies"])
        entry["status"] = "有效"
        entry["pending"] = True
        entry["异常状态"] = False
        entry["_refresh_count"] += 1
        entry["最后刷新"] = time.strftime("%Y-%m-%d %H:%M:%S")
        store.record_metric("cookie_jar", "jar_refreshed", 1,
                            {"count": str(len(new_cookies))})
        return entry

    def check_expiry(self, entry_id: int) -> tuple[str, bool]:
        """检查Cookie是否过期，返回状态和是否需要通知任务。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "未知", False
        now = time.time()
        expired_count = 0
        for info in entry["_cookies"].values():
            if now > info.get("expires_at", 0):
                expired_count += 1
        total = len(entry["_cookies"])
        if total == 0:
            return "已失效", True
        expire_ratio = expired_count / total
        if expire_ratio > 0.8:
            entry["status"] = "已过期"
            entry["abnormal"] = True
            entry["过期时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return "已过期", True
        if expire_ratio > 0.3:
            entry["status"] = "临近过期"
            return "临近过期", False
        return "有效", False

    def notify_mission_pause(self, entry_id: int, mission_id: int) -> None:
        """Cookie过期时通知关联采集任务暂停。"""
        store.pipeline_trace(
            "cookie_jar", "crawl_mission", entry_id, mission_id,
            "expired_notify", {"reason": "cookie_expired"}
        )
        store.record_metric("cookie_jar", "mission_paused_by_expiry", 1,
                            {"mission_id": str(mission_id)})

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        jars = store.rows(self.MODULE)
        base["active_jars"] = sum(1 for j in jars if j.get("status") == "有效")
        base["expired_jars"] = sum(1 for j in jars if j.get("status") in ("已过期", "已失效"))
        base["total_refreshes"] = sum(j.get("_refresh_count", 0) for j in jars)
        return base