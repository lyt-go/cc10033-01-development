"""机器人校验领域服务：反爬虫检测触发记录、封禁类型与恢复策略。

跨模块数据流：
- robot_verify ← proxy_node: 代理封禁时上报检测结果
- robot_verify → delivery_pipe: 检测信息同步到投递管道
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RobotVerifyService(BaseModuleService):
    MODULE = "robot_verify"
    ENTITY = "校验日志"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["日志编号", "检测页面", "触发规则", "封禁类型", "封禁时长", "恢复策略", "检测时间", "代理切换次数"]
    REQUIRED_FIELDS = ["日志编号", "检测页面", "触发规则"]
    STATUS_ORDER = ["检测中", "已封禁", "已恢复", "人工处理"]
    ACTION_RULES = {"标记封禁": "已封禁", "标记恢复": "已恢复", "人工处理": "人工处理"}

    BAN_TYPES = ["IP封禁", "频率限制", "验证码拦截", "UA封禁", "Cookie校验", "JS挑战",
                 "Cloudflare盾", "Akamai防御", "访问地域限制", "行为分析拦截"]
    RECOVERY_STRATEGIES = ["切换代理", "更换UA", "等待冷却", "随机延时", "模拟真人行为",
                           "更换指纹", "更换Cookie", "降速爬取"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_detection_history"] = []
        entry["_ban_history"] = []
        entry["_proxy_rotations"] = 0
        entry["_created_ts"] = time.time()
        store.record_metric("robot_verify", "detection_started", 1,
                            {"page": values.get("检测页面", "")})
        return entry, []

    def detect(self, entry_id: int, page_url: str) -> dict[str, Any]:
        """模拟反爬虫检测，返回检测结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"status": "unknown"}
        entry["_detection_history"].append({"url": page_url, "ts": time.time()})
        is_blocked = random.random() < 0.35
        ban_type = random.choice(self.BAN_TYPES) if is_blocked else None
        result = {
            "entry_id": entry_id,
            "page": page_url,
            "blocked": is_blocked,
            "ban_type": ban_type,
            "risk_level": random.uniform(0.1, 0.9) if not is_blocked else random.uniform(0.7, 1.0),
            "detection_ts": time.time(),
        }
        if is_blocked:
            entry["status"] = "已封禁"
            entry["封禁类型"] = ban_type
            entry["封禁时长"] = f"{random.randint(60, 7200)}秒"
            entry["恢复策略"] = random.choice(self.RECOVERY_STRATEGIES)
            entry["abnormal"] = True
            store.record_metric("robot_verify", "ban_detected", 1,
                                {"type": ban_type, "page": page_url})
        else:
            entry["status"] = "检测中"
            store.record_metric("robot_verify", "detection_passed", 1,
                                {"risk": f"{result['risk_level']:.2f}"})
        entry["检测时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    def rotate_proxy(self, entry_id: int, old_proxy_id: int, new_proxy_id: int) -> dict[str, Any] | None:
        """封禁时切换代理。跨模块：robot_verify ← proxy_node"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_proxy_rotations"] += 1
        entry["代理切换次数"] = str(entry["_proxy_rotations"])
        store.pipeline_trace(
            "proxy_node", "robot_verify", old_proxy_id, entry_id,
            "proxy_rotated", {"new_proxy_id": new_proxy_id}
        )
        store.record_metric("robot_verify", "proxy_rotated", 1,
                            {"from": str(old_proxy_id), "to": str(new_proxy_id)})
        return entry

    def sync_to_delivery_pipe(self, entry_id: int, delivery_pipe_id: int,
                              detection_result: dict[str, Any]) -> dict[str, Any] | None:
        """检测信息同步到投递管道。跨模块：robot_verify → delivery_pipe"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        store.pipeline_trace(
            "robot_verify", "delivery_pipe", entry_id, delivery_pipe_id,
            "sync_detection", {"blocked": detection_result.get("blocked", False)}
        )
        store.record_metric("robot_verify", "detection_synced", 1,
                            {"pipe_id": str(delivery_pipe_id)})
        return entry

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        logs = store.rows(self.MODULE)
        base["blocked_count"] = sum(1 for l in logs if l.get("status") == "已封禁")
        base["recovered_count"] = sum(1 for l in logs if l.get("status") == "已恢复")
        base["total_proxy_rotations"] = sum(l.get("_proxy_rotations", 0) for l in logs)
        return base