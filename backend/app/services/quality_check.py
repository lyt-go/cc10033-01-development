"""质量抽检领域服务：采集数据质量抽检、合格判定与异常上报。

跨模块数据流：
- quality_check ← schema_match: Schema匹配结果送入质量抽检
- quality_check → export_dataset: 质量抽检通过后触发导出
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QualityCheckService(BaseModuleService):
    MODULE = "quality_check"
    ENTITY = "抽检任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "数据批次", "抽检比例", "检测字段", "通过标准", "合格数量", "不合格数量", "抽检结论"]
    REQUIRED_FIELDS = ["任务编号", "数据批次", "抽检比例"]
    STATUS_ORDER = ["待执行", "执行中", "通过", "未通过"]
    ACTION_RULES = {"发起抽检": "执行中", "标记通过": "通过", "标记未通过": "未通过"}

    CHECK_ITEMS = ["完整性", "准确性", "一致性", "时效性", "唯一性", "格式规范", "业务规则"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_check_results"] = {}
        entry["_score"] = 0.0
        entry["_executed_at"] = 0.0
        store.record_metric("quality_check", "check_task_created", 1,
                            {"batch": values.get("数据批次", "")})
        return entry, []

    def run_check(self, entry_id: int, sample_data: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """执行质量抽检，逐项评分。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "执行中"
        entry["_executed_at"] = time.time()
        sample_size = sample_data if isinstance(sample_data, int) else len(sample_data) if sample_data else random.randint(10, 200)
        # 模拟检测
        results = {}
        low_score_count = 0
        for item in self.CHECK_ITEMS:
            score = random.uniform(0.6, 1.0)
            results[item] = {
                "score": score,
                "passed": score >= 0.8,
                "sample_count": sample_size if isinstance(sample_size, int) else 0,
                "defect_count": random.randint(0, max(1, sample_size // 10)) if isinstance(sample_size, int) else 0,
            }
            if not results[item]["passed"]:
                low_score_count += 1
        overall = sum(r["score"] for r in results.values()) / len(results)
        entry["_check_results"] = results
        entry["_score"] = overall
        entry["合格数量"] = sample_size if isinstance(sample_size, int) else 0
        entry["不合格数量"] = low_score_count
        pass_threshold = float(entry.get("通过标准") or 0.8)
        if overall >= pass_threshold:
            entry["status"] = "通过"
            entry["pending"] = False
            entry["抽检结论"] = f"合格(综合评分{overall:.0%})"
            store.record_metric("quality_check", "check_passed", 1,
                                {"score": f"{overall:.2f}"})
        else:
            entry["status"] = "未通过"
            entry["abnormal"] = True
            entry["抽检结论"] = f"不合格(综合评分{overall:.0%}，{low_score_count}项不达标)"
            store.record_metric("quality_check", "check_failed", 1,
                                {"score": f"{overall:.2f}",
                                 "failed_items": str(low_score_count)})
        return {"entry_id": entry_id, "score": overall, "results": results,
                "status": entry["status"]}

    def trigger_export_if_passed(self, entry_id: int, export_task_id: int) -> dict[str, Any] | None:
        """质量通过后触发导出。跨模块：quality_check → export_dataset"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "通过":
            return None
        store.pipeline_trace(
            "quality_check", "export_dataset", entry_id, export_task_id,
            "trigger", {"score": entry["_score"]}
        )
        store.record_metric("quality_check", "export_triggered", 1,
                            {"export_id": str(export_task_id)})
        return entry

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        base["passed_checks"] = sum(1 for t in tasks if t.get("status") == "通过")
        base["failed_checks"] = sum(1 for t in tasks if t.get("status") == "未通过")
        base["avg_score"] = (
            sum(t.get("_score", 0.0) for t in tasks) / max(len(tasks), 1)
        )
        return base