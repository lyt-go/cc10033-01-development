"""设备指纹领域服务：浏览器指纹模板管理、随机化策略与特征组合。

跨模块数据流：
- fingerprint → cookie_jar: 指纹分配给Cookie容器绑定设备身份
- fingerprint → page_render: 渲染时注入指纹配置
"""
from __future__ import annotations

import hashlib
import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FingerprintService(BaseModuleService):
    MODULE = "fingerprint"
    ENTITY = "指纹模板"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["模板编号", "浏览器类型", "UserAgent", "屏幕分辨率", "语言偏好", "时区设置", "WebGL指纹", "Canvas指纹"]
    REQUIRED_FIELDS = ["模板编号", "浏览器类型", "UserAgent"]
    STATUS_ORDER = ["待测试", "可用", "已封禁", "已废弃"]
    ACTION_RULES = {"测试指纹": "可用", "标记可用": "已封禁", "标记封禁": "已废弃"}

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_fingerprint_hash"] = self._compute_hash(entry)
        entry["_usage_count"] = 0
        entry["_last_used_ts"] = 0
        entry["_ban_history"] = []
        store.record_metric("fingerprint", "template_created", 1,
                            {"browser": values.get("浏览器类型", "unknown")})
        return entry, []

    def assign_to_cookie_jar(self, entry_id: int, cookie_jar_id: int) -> dict[str, Any] | None:
        """将指纹模板绑定到Cookie容器，建立设备身份关联。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "可用":
            return None
        entry["_cookie_jar_id"] = cookie_jar_id
        entry["_usage_count"] += 1
        entry["_last_used_ts"] = time.time()
        store.pipeline_trace(
            "fingerprint", "cookie_jar", entry_id, cookie_jar_id,
            "bind", {"hash": entry["_fingerprint_hash"]}
        )
        store.record_metric("fingerprint", "fingerprint_assigned", 1,
                            {"cookie_jar_id": str(cookie_jar_id)})
        return entry

    def inject_to_render(self, entry_id: int, render_id: int) -> dict[str, Any] | None:
        """渲染时注入设备指纹配置。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "可用":
            return None
        entry["_render_id"] = render_id
        store.pipeline_trace(
            "fingerprint", "page_render", entry_id, render_id,
            "inject", {"ua": entry.get("UserAgent", "")}
        )
        return entry

    def randomize_fingerprint(self, entry_id: int) -> dict[str, Any] | None:
        """随机化指纹特征，降低封禁风险。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        resolutions = ["1920x1080", "2560x1440", "1440x900", "3840x2160"]
        entry["屏幕分辨率"] = random.choice(resolutions)
        entry["_fingerprint_hash"] = self._compute_hash(entry)
        entry["_randomized_count"] = entry.get("_randomized_count", 0) + 1
        store.record_metric("fingerprint", "fingerprint_randomized", 1,
                            {"template_id": str(entry_id)})
        return entry

    def mark_banned(self, entry_id: int) -> dict[str, Any] | None:
        """标记指纹被封禁，记录封禁历史。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "已封禁"
        entry["abnormal"] = True
        entry["_ban_history"].append({
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 自动随机化生成新指纹
        if entry.get("_cookie_jar_id"):
            store.pipeline_trace(
                "fingerprint", "cookie_jar", entry_id, entry["_cookie_jar_id"],
                "unbind", {"reason": "banned"}
            )
        store.record_metric("fingerprint", "fingerprint_banned", 1,
                            {"template_id": str(entry_id)})
        return entry

    def _compute_hash(self, entry: dict[str, Any]) -> str:
        raw = "|".join([
            str(entry.get("浏览器类型") or ""),
            str(entry.get("UserAgent") or ""),
            str(entry.get("屏幕分辨率") or ""),
            str(entry.get("WebGL指纹") or ""),
            str(entry.get("Canvas指纹") or ""),
        ])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        templates = store.rows(self.MODULE)
        base["active_templates"] = sum(1 for t in templates if t.get("status") == "可用")
        base["banned_count"] = sum(1 for t in templates if t.get("status") == "已封禁")
        base["total_assignments"] = sum(t.get("_usage_count", 0) for t in templates)
        return base