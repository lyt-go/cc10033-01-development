"""结果解析领域服务：采集结果字段解析、结构化转换与异常记录。

跨模块数据流：
- result_parse ← extract_rule: 抽取结果进入解析流程
- result_parse → schema_match: 解析结果送入Schema匹配
"""
from __future__ import annotations

import hashlib
import random
import re
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ResultParseService(BaseModuleService):
    MODULE = "result_parse"
    ENTITY = "解析记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["记录编号", "来源页面", "解析方式", "字段数量", "数据行数", "解析耗时", "异常字段", "校验状态"]
    REQUIRED_FIELDS = ["记录编号", "来源页面", "解析方式"]
    STATUS_ORDER = ["解析中", "解析完成", "字段缺失", "格式异常"]
    ACTION_RULES = {"标记完成": "解析完成", "标记缺失": "字段缺失", "标记异常": "格式异常"}

    PARSE_METHODS = ["HTML_Parse", "JSON_Parse", "XML_Parse", "Custom_Regex", "LLM_Extract"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_parsed_fields"] = {}
        entry["_parse_history"] = []
        entry["_start_ts"] = time.time()
        store.record_metric("result_parse", "parse_started", 1,
                            {"method": values.get("解析方式", ""),
                             "source": values.get("来源页面", "")})
        return entry, []

    def parse(self, entry_id: int, raw_content: str) -> dict[str, Any] | None:
        """解析原始内容，提取结构化字段。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        start = time.time()
        # 模拟解析
        self._simulate_parse()
        elapsed = (time.time() - start) * 1000
        fields = self._extract_fields(raw_content)
        entry["解析耗时"] = f"{elapsed:.0f}ms"
        entry["_parsed_fields"] = fields
        entry["字段数量"] = len(fields)
        entry["数据行数"] = str(random.randint(10, 500))
        entry["校验状态"] = "已校验" if len(fields) >= 5 else "字段不足"
        entry["_parse_history"].append({
            "ts": time.time(),
            "field_count": len(fields),
            "elapsed_ms": elapsed,
        })
        if len(fields) < 3:
            entry["status"] = "字段缺失"
            entry["abnormal"] = True
            entry["异常字段"] = f"缺失{3 - len(fields)}个关键字段"
            store.record_metric("result_parse", "parse_field_missing", 1,
                                {"field_count": str(len(fields))})
        elif "error" in raw_content.lower():
            entry["status"] = "格式异常"
            entry["abnormal"] = True
            entry["异常字段"] = "内容格式异常"
            store.record_metric("result_parse", "parse_format_error", 1,
                                {"source": entry.get("来源页面", "")})
        else:
            entry["status"] = "解析完成"
            entry["pending"] = False
            store.record_metric("result_parse", "parse_completed", 1,
                                {"field_count": str(len(fields)),
                                 "elapsed": str(elapsed)})
        return {"entry_id": entry_id, "fields": fields, "status": entry["status"],
                "elapsed_ms": elapsed}

    def feed_to_schema_match(self, entry_id: int, schema_match_id: int,
                             parsed_fields: dict[str, Any]) -> dict[str, Any] | None:
        """解析结果送入Schema匹配进行字段映射。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "解析完成":
            return None
        store.pipeline_trace(
            "result_parse", "schema_match", entry_id, schema_match_id,
            "feed", {"field_count": len(parsed_fields)}
        )
        store.record_metric("result_parse", "data_fed_to_schema", 1,
                            {"schema_id": str(schema_match_id)})
        return entry

    def _extract_fields(self, content: str) -> dict[str, str]:
        """模拟字段抽取。"""
        field_count = random.randint(4, 15)
        return {
            f"field_{i}": f"value_{i}_{hashlib.md5(content.encode()).hexdigest()[:6]}"
            for i in range(field_count)
        }

    def _simulate_parse(self) -> None:
        time.sleep(random.uniform(0.02, 0.08))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        records = store.rows(self.MODULE)
        base["completed_parses"] = sum(1 for r in records if r.get("status") == "解析完成")
        base["error_parses"] = sum(1 for r in records if r.get("status") in ("字段缺失", "格式异常"))
        base["avg_field_count"] = (
            sum(int(r.get("字段数量", 0)) for r in records) / max(len(records), 1)
        )
        return base