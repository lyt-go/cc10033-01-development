"""页面渲染领域服务：渲染引擎执行、性能指标采集与异常监控。

跨模块数据流：
- page_render ← fingerprint: 渲染时注入设备指纹配置
- page_render → extract_rule: 渲染完成后触发数据抽取规则
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class PageRenderService(BaseModuleService):
    MODULE = "page_render"
    ENTITY = "渲染记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["记录编号", "渲染引擎", "页面URL", "渲染耗时", "DOM节点数", "截图大小", "JS错误数", "结束状态"]
    REQUIRED_FIELDS = ["记录编号", "渲染引擎", "页面URL"]
    STATUS_ORDER = ["渲染中", "成功", "超时", "异常中断"]
    ACTION_RULES = {"标记成功": "成功", "标记超时": "超时", "标记异常": "异常中断"}

    ENGINES = ["Chromium Headless", "Playwright", "Puppeteer", "Selenium", "WebKit"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_js_errors"] = []
        entry["_render_metrics"] = {}
        entry["_start_ts"] = time.time()
        store.record_metric("page_render", "render_started", 1,
                            {"engine": values.get("渲染引擎", ""),
                             "url": values.get("页面URL", "")})
        return entry, []

    def inject_fingerprint(self, entry_id: int, fingerprint_id: int) -> dict[str, Any] | None:
        """渲染前注入设备指纹配置。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_fingerprint_id"] = fingerprint_id
        store.pipeline_trace(
            "fingerprint", "page_render", fingerprint_id, entry_id,
            "inject", {"url": entry.get("页面URL", "")}
        )
        return entry

    def render(self, entry_id: int, timeout_ms: int = 30000) -> dict[str, Any] | None:
        """模拟页面渲染执行。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        start = time.time()
        # 模拟渲染过程
        self._simulate_render(timeout_ms / 1000)
        elapsed = (time.time() - start) * 1000
        entry["渲染耗时"] = f"{elapsed:.0f}ms"
        if elapsed > timeout_ms:
            entry["status"] = "超时"
            entry["abnormal"] = True
            store.record_metric("page_render", "render_timeout", 1,
                                {"url": entry.get("页面URL", ""),
                                 "elapsed": str(elapsed)})
            return entry
        err_count = random.randint(0, 5)
        entry["DOM节点数"] = str(random.randint(500, 5000))
        entry["截图大小"] = f"{random.randint(50, 500)}KB"
        entry["JS错误数"] = str(err_count)
        entry["status"] = "成功" if err_count < 3 else "异常中断"
        entry["abnormal"] = err_count >= 3
        entry["结束状态"] = entry["status"]
        entry["_render_metrics"] = {
            "DOMContentLoaded": random.uniform(0.2, 2.0),
            "Load": random.uniform(0.5, 5.0),
            "FirstPaint": random.uniform(0.1, 1.0),
        }
        store.record_metric("page_render", "render_completed", 1,
                            {"status": entry["status"],
                             "js_errors": str(err_count)})
        return entry

    def trigger_extraction(self, entry_id: int, extract_rule_id: int) -> dict[str, Any] | None:
        """渲染完成后触发数据抽取规则。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "成功":
            return None
        store.pipeline_trace(
            "page_render", "extract_rule", entry_id, extract_rule_id,
            "trigger_extraction", {"url": entry.get("页面URL", "")}
        )
        store.record_metric("page_render", "extraction_triggered", 1,
                            {"rule_id": str(extract_rule_id)})
        return entry

    def _simulate_render(self, max_seconds: float) -> None:
        time.sleep(random.uniform(0.1, min(max_seconds * 0.8, 2.0)))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        records = store.rows(self.MODULE)
        base["success_renders"] = sum(1 for r in records if r.get("status") == "成功")
        base["timeout_renders"] = sum(1 for r in records if r.get("status") == "超时")
        base["total_js_errors"] = sum(int(r.get("JS错误数", 0)) for r in records if r.get("JS错误数", "").isdigit())
        return base