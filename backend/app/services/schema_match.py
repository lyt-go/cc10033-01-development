"""Schema匹配领域服务：采集数据与目标Schema字段映射、补全策略与质量评分。

跨模块数据流：
- schema_match ← result_parse: 解析结果送入Schema匹配
- schema_match → quality_check: 匹配结果送入质量抽检
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SchemaMatchService(BaseModuleService):
    MODULE = "schema_match"
    ENTITY = "匹配配置"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["配置编号", "来源站点", "目标Schema", "映射规则", "字段对应表", "缺失补全策略", "匹配度", "最后验证"]
    REQUIRED_FIELDS = ["配置编号", "来源站点", "目标Schema"]
    STATUS_ORDER = ["草稿", "待验证", "已启用", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "启用配置": "已启用", "停用配置": "已停用"}
    NEGATIVE_ACTIONS = ["停用配置"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_field_mapping"] = {}
        entry["_match_history"] = []
        entry["_total_matches"] = 0
        entry["_successful_matches"] = 0
        store.record_metric("schema_match", "config_created", 1,
                            {"site": values.get("来源站点", ""),
                             "schema": values.get("目标Schema", "")})
        return entry, []

    def define_mapping(self, entry_id: int, mapping_rules: dict[str, str]) -> dict[str, Any] | None:
        """定义源字段到目标Schema字段的映射规则。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["_field_mapping"] = mapping_rules
        entry["映射规则"] = json.dumps(mapping_rules, ensure_ascii=False)
        entry["字段对应表"] = json.dumps({k: k for k in mapping_rules}, ensure_ascii=False)
        store.record_metric("schema_match", "mapping_defined", len(mapping_rules),
                            {"schema": entry.get("目标Schema", "")})
        return entry

    def match(self, entry_id: int, parsed_fields: dict[str, Any]) -> dict[str, Any]:
        """执行字段匹配和补全。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"matched": {}, "missing": [], "score": 0.0}
        entry["_total_matches"] += 1
        needed = self._get_target_fields(entry.get("目标Schema", ""))
        matched = {}
        missing = []
        for target_field in needed:
            found = False
            for src_field in parsed_fields:
                similarity = self._field_similarity(target_field, src_field)
                if similarity > 0.6:
                    matched[target_field] = {"source": src_field,
                                              "value": parsed_fields[src_field],
                                              "similarity": similarity}
                    found = True
                    break
            if not found:
                missing.append(target_field)
        # 补全策略
        for mf in missing:
            strategy = entry.get("缺失补全策略", "default")
            if strategy == "default":
                matched[mf] = {"source": "__default__", "value": "N/A", "similarity": 0.0}
            elif strategy == "fill":
                matched[mf] = {"source": "__fill__", "value": "", "similarity": 0.0}
        score = len(matched) / max(len(needed), 1)
        entry["匹配度"] = f"{score:.0%}"
        entry["最后验证"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["_match_history"].append({
            "ts": time.time(),
            "score": score,
            "matched": len(matched),
            "missing": len([m for m in missing]),
        })
        if score >= 0.8:
            entry["_successful_matches"] += 1
            store.record_metric("schema_match", "match_successful", 1,
                                {"score": f"{score:.2f}"})
        else:
            store.record_metric("schema_match", "match_low_score", 1,
                                {"score": f"{score:.2f}", "missing": str(len(missing))})
        return {"matched": matched, "missing": missing, "score": score}

    def feed_to_quality_check(self, entry_id: int, quality_check_id: int,
                              match_result: dict[str, Any]) -> dict[str, Any] | None:
        """匹配结果送入质量抽检。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        store.pipeline_trace(
            "schema_match", "quality_check", entry_id, quality_check_id,
            "feed", {"score": match_result.get("score", 0)}
        )
        store.record_metric("schema_match", "result_fed_to_quality", 1,
                            {"quality_id": str(quality_check_id)})
        return entry

    def _get_target_fields(self, schema_name: str) -> list[str]:
        defaults = {
            "default": ["title", "content", "publish_time", "author", "source", "category", "tags", "price", "location"],
        }
        return defaults.get(schema_name, defaults["default"])

    def _field_similarity(self, a: str, b: str) -> float:
        """简单字段名相似度。"""
        a, b = a.lower(), b.lower()
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.7
        common = len(set(a) & set(b))
        return common / max(len(a), len(b)) * 0.5

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        configs = store.rows(self.MODULE)
        base["active_configs"] = sum(1 for c in configs if c.get("status") == "已启用")
        base["total_matches"] = sum(c.get("_total_matches", 0) for c in configs)
        base["match_success_rate"] = (
            sum(c.get("_successful_matches", 0) for c in configs) /
            max(sum(c.get("_total_matches", 0) for c in configs), 1)
        )
        return base