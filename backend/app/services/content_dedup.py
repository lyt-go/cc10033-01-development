"""内容去重领域服务：采集内容指纹去重、相似度计算与重复处理。

跨模块数据流：
- content_dedup ← seed_url: 种子URL爬取内容送入去重检测
"""
from __future__ import annotations

import hashlib
import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ContentDedupService(BaseModuleService):
    MODULE = "content_dedup"
    ENTITY = "去重记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["记录编号", "内容指纹", "原始URL", "重复来源URL", "相似度", "发现时间", "处理方式", "保留策略"]
    REQUIRED_FIELDS = ["记录编号", "内容指纹", "原始URL"]
    STATUS_ORDER = ["唯一内容", "完全重复", "高度相似", "已合并"]
    ACTION_RULES = {"标记重复": "完全重复", "标记相似": "高度相似", "合并内容": "已合并"}

    # 内置指纹库，用于模拟去重检测
    _fingerprint_db: dict[str, dict[str, Any]] = {}

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_simhash"] = self._compute_simhash(values.get("内容指纹", ""))
        entry["_content_length"] = 0
        entry["_duplicate_check_ts"] = time.time()
        store.record_metric("content_dedup", "record_created", 1,
                            {"url": values.get("原始URL", "")})
        return entry, []

    def check_duplicate(self, entry_id: int, content: str, url: str) -> dict[str, Any]:
        """检查内容是否重复。模拟SimHash相似度检测。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"status": "unknown", "similarity": 0.0}
        content_hash = hashlib.md5(content.encode()).hexdigest()
        entry["_content_length"] = len(content)
        if content_hash in self._fingerprint_db:
            existing = self._fingerprint_db[content_hash]
            entry["status"] = "完全重复"
            entry["重复来源URL"] = existing["url"]
            entry["相似度"] = "100%"
            entry["处理方式"] = "跳过重复"
            store.record_metric("content_dedup", "exact_duplicate", 1,
                                {"url": url})
            return {"status": "完全重复", "similarity": 1.0,
                    "duplicate_of": existing["url"]}
        # 近似检测
        sim_score = self._approximate_match(content)
        if sim_score > 0.90:
            entry["status"] = "高度相似"
            entry["相似度"] = f"{sim_score * 100:.0f}%"
            entry["处理方式"] = "标记相似"
            store.record_metric("content_dedup", "similar_content", 1,
                                {"similarity": f"{sim_score:.2f}"})
            return {"status": "高度相似", "similarity": sim_score,
                    "best_match_score": sim_score}
        # 唯一内容
        entry["status"] = "唯一内容"
        entry["相似度"] = "新内容"
        entry["处理方式"] = "保留入库"
        self._fingerprint_db[content_hash] = {
            "url": url,
            "added_at": time.time(),
            "fingerprint": entry.get("内容指纹", ""),
        }
        store.record_metric("content_dedup", "unique_content", 1,
                            {"url": url})
        return {"status": "唯一内容", "similarity": 0.0, "fingerprint": content_hash}

    def merge_with_duplicates(self, entry_id: int, target_entry_ids: list[int]) -> dict[str, Any] | None:
        """合并重复内容，保留主记录。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "已合并"
        entry["保留策略"] = f"已合并{len(target_entry_ids)}条记录"
        for tid in target_entry_ids:
            dup = store.find(self.MODULE, tid)
            if dup:
                dup["status"] = "已合并"
                dup["保留策略"] = f"归入记录#{entry_id}"
        store.record_metric("content_dedup", "content_merged", len(target_entry_ids),
                            {"master_id": str(entry_id)})
        return entry

    def _compute_simhash(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:8]

    def _approximate_match(self, content: str) -> float:
        """模拟相似度计算。"""
        if not self._fingerprint_db:
            return 0.0
        return random.uniform(0.3, 0.95)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        records = store.rows(self.MODULE)
        base["unique_count"] = sum(1 for r in records if r.get("status") == "唯一内容")
        base["duplicate_count"] = sum(1 for r in records if r.get("status") == "完全重复")
        base["merged_count"] = sum(1 for r in records if r.get("status") == "已合并")
        base["fingerprint_db_size"] = len(self._fingerprint_db)
        return base