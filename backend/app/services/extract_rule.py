"""抽取规则领域服务：数据抽取规则定义、版本管理与格式输出。

跨模块数据流：
- extract_rule ← page_render: 渲染完成后触发抽取
- extract_rule → result_parse: 抽取结果进入解析流程
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExtractRuleService(BaseModuleService):
    MODULE = "extract_rule"
    ENTITY = "抽取规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "目标页面", "抽取方式", "CSS选择器", "正则表达式", "字段映射", "输出格式", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "目标页面", "抽取方式"]
    STATUS_ORDER = ["草稿", "待验证", "已发布", "已废弃"]
    ACTION_RULES = {"提交验证": "待验证", "发布规则": "已发布", "标记废弃": "已废弃"}

    EXTRACTION_METHODS = ["CSS_Selector", "XPath", "Regex", "JSONPath", "Hybrid"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_version_history"] = [{"version": 1, "ts": time.time()}]
        entry["_execution_count"] = 0
        entry["_success_count"] = 0
        entry["_sample_results"] = []
        store.record_metric("extract_rule", "rule_created", 1,
                            {"method": values.get("抽取方式", ""),
                             "page": values.get("目标页面", "")})
        return entry, []

    def execute(self, entry_id: int, page_html: str | None = None) -> dict[str, Any] | None:
        """模拟执行抽取规则，返回结构化数据。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") not in ("已发布", "待验证"):
            return None
        entry["_execution_count"] += 1
        csv_field = entry.get("CSS选择器", "")
        # 模拟抽取结果
        result = {
            "rule_id": entry_id,
            "method": entry.get("抽取方式", ""),
            "execution_ts": time.time(),
            "fields_extracted": len(csv_field.split(",")) if csv_field else random.randint(3, 12),
            "rows_extracted": random.randint(1, 100),
            "samples": self._generate_samples(),
        }
        entry["_success_count"] += 1
        entry["_sample_results"].append(result)
        store.record_metric("extract_rule", "rule_executed", 1,
                            {"fields": str(result["fields_extracted"]),
                             "rows": str(result["rows_extracted"])})
        return result

    def feed_to_parser(self, entry_id: int, result_parse_id: int, extracted_data: dict[str, Any]) -> dict[str, Any] | None:
        """将抽取结果送入解析流程。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        store.pipeline_trace(
            "extract_rule", "result_parse", entry_id, result_parse_id,
            "feed", {"rows": extracted_data.get("rows_extracted", 0)}
        )
        store.record_metric("extract_rule", "data_fed_to_parser", 1,
                            {"parse_id": str(result_parse_id)})
        return entry

    def version_upgrade(self, entry_id: int) -> dict[str, Any] | None:
        """规则版本升级。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        current = len(entry["_version_history"])
        entry["_version_history"].append({"version": current + 1, "ts": time.time()})
        entry["规则版本"] = f"v{current + 1}"
        store.record_metric("extract_rule", "version_upgraded", 1,
                            {"version": str(current + 1)})
        return entry

    def _generate_samples(self) -> list[dict[str, str]]:
        return [
            {"field": f"sample_field_{i}", "value": f"sample_value_{i}_{random.randint(1, 1000)}"}
            for i in range(random.randint(2, 6))
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rules = store.rows(self.MODULE)
        base["published_rules"] = sum(1 for r in rules if r.get("status") == "已发布")
        base["total_executions"] = sum(r.get("_execution_count", 0) for r in rules)
        base["success_rate"] = (
            sum(r.get("_success_count", 0) for r in rules) /
            max(sum(r.get("_execution_count", 0) for r in rules), 1)
        )
        return base