"""验证码处理领域服务：验证码识别、类型分类与解决策略。

独立领域服务，管理验证码识别全生命周期。
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CaptchaSolveService(BaseModuleService):
    MODULE = "captcha_solve"
    ENTITY = "验证码记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["记录编号", "验证码类型", "图片数据摘要", "识别结果", "置信度", "耗时毫秒", "解决方式", "关联任务"]
    REQUIRED_FIELDS = ["记录编号", "验证码类型", "图片数据摘要"]
    STATUS_ORDER = ["待识别", "识别成功", "识别失败", "人工介入"]
    ACTION_RULES = {"发起识别": "识别成功", "标记成功": "识别失败", "标记失败": "人工介入"}

    CAPTCHA_TYPES = ["reCAPTCHA v2", "reCAPTCHA v3", "hCaptcha", "滑块验证", "短信验证", "图形验证", "云片滑动"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_attempt_count"] = 0
        entry["_solve_history"] = []
        entry["_created_ts"] = time.time()
        store.record_metric("captcha_solve", "captcha_detected", 1,
                            {"type": values.get("验证码类型", "unknown")})
        return entry, []

    def solve(self, entry_id: int, method: str = "auto") -> dict[str, Any] | None:
        """模拟验证码解决过程。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_attempt_count"] += 1
        start = time.time()
        self._simulate_solve()
        elapsed = (time.time() - start) * 1000
        success = random.random() > 0.25
        entry["耗时毫秒"] = f"{elapsed:.0f}ms"
        if success:
            entry["status"] = "识别成功"
            entry["pending"] = False
            entry["识别结果"] = f"OK-{random.randint(1000, 9999)}"
            entry["置信度"] = f"{random.uniform(0.80, 0.99):.2%}"
            store.record_metric("captcha_solve", "solve_success", 1,
                                {"type": entry.get("验证码类型", ""),
                                 "attempt": str(entry["_attempt_count"])})
        else:
            if entry["_attempt_count"] >= 3:
                entry["status"] = "人工介入"
                entry["abnormal"] = True
            else:
                entry["status"] = "识别失败"
            entry["置信度"] = f"{random.uniform(0.20, 0.45):.2%}"
            store.record_metric("captcha_solve", "solve_failed", 1,
                                {"type": entry.get("验证码类型", ""),
                                 "attempt": str(entry["_attempt_count"])})
        return entry

    def identify_type(self, image_hash: str) -> dict[str, Any]:
        """基于图片摘要识别验证码类型。"""
        type_index = hash(image_hash) % len(self.CAPTCHA_TYPES)
        return {"type": self.CAPTCHA_TYPES[type_index], "confidence": random.uniform(0.7, 0.95)}

    def _simulate_solve(self) -> None:
        time.sleep(random.uniform(0.05, 0.15))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        records = store.rows(self.MODULE)
        base["total_attempts"] = sum(r.get("_attempt_count", 0) for r in records)
        base["solve_rate"] = (
            sum(1 for r in records if r.get("status") == "识别成功") / max(len(records), 1)
        )
        return base