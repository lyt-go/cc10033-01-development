"""种子URL领域服务：种子URL层级管理、爬取频率与调度计划。

跨模块数据流：
- seed_url → content_dedup: 爬取内容去重检测
- seed_url → scheduler: URL种子关联调度计划
"""
from __future__ import annotations

import hashlib
import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SeedUrlService(BaseModuleService):
    MODULE = "seed_url"
    ENTITY = "种子URL"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["种子编号", "URL地址", "所属站点", "深度层级", "优先级", "爬取频率", "上次爬取", "下次计划"]
    REQUIRED_FIELDS = ["种子编号", "URL地址", "所属站点"]
    STATUS_ORDER = ["待爬取", "爬取中", "已完成", "已冻结"]
    ACTION_RULES = {"发起爬取": "爬取中", "标记完成": "已完成", "冻结种子": "已冻结"}

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_url_hash"] = self._hash_url(values.get("URL地址", ""))
        entry["_crawl_history"] = []
        entry["_child_urls"] = []
        entry["_content_hash"] = ""
        entry["_created_ts"] = time.time()
        store.record_metric("seed_url", "seed_created", 1,
                            {"site": values.get("所属站点", "unknown")})
        return entry, []

    def start_crawl(self, entry_id: int, scheduler_plan_id: int | None = None) -> dict[str, Any] | None:
        """发起爬取，关联调度计划。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待爬取":
            return None
        entry["status"] = "爬取中"
        entry["上次爬取"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["pending"] = True
        if scheduler_plan_id:
            store.pipeline_trace(
                "seed_url", "scheduler", entry_id, scheduler_plan_id,
                "start_crawl", {"url": entry.get("URL地址", "")}
            )
        store.record_metric("seed_url", "crawl_started", 1,
                            {"url": entry.get("URL地址", ""),
                             "depth": str(entry.get("深度层级", 0))})
        return entry

    def feed_to_dedup(self, entry_id: int, dedup_record_id: int, content: str) -> dict[str, Any] | None:
        """爬取内容送入去重检测。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        content_hash = hashlib.md5(content.encode()).hexdigest()
        entry["_content_hash"] = content_hash
        store.pipeline_trace(
            "seed_url", "content_dedup", entry_id, dedup_record_id,
            "feed_content", {"hash": content_hash[:12]}
        )
        store.record_metric("seed_url", "content_fed_to_dedup", 1,
                            {"hash": content_hash[:8]})
        return entry

    def discover_child_urls(self, entry_id: int, child_urls: list[str]) -> dict[str, Any] | None:
        """发现子URL，记录到种子列表。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        for child_url in child_urls:
            entry["_child_urls"].append({
                "url": child_url,
                "discovered_at": time.time(),
                "hash": self._hash_url(child_url),
            })
        entry["深度层级"] = str(int(entry.get("深度层级", 0)) + 1)
        store.record_metric("seed_url", "child_urls_discovered", len(child_urls),
                            {"parent": str(entry_id)})
        return entry

    def schedule_next_crawl(self, entry_id: int) -> dict[str, Any] | None:
        """计算下次爬取时间。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        interval = self._parse_interval(entry.get("爬取频率", "24h"))
        next_ts = time.time() + interval
        entry["下次计划"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_ts))
        entry["_next_crawl_ts"] = next_ts
        return entry

    def _hash_url(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def _parse_interval(self, freq: str) -> float:
        freq = str(freq).strip().lower()
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        for unit, mul in multipliers.items():
            if freq.endswith(unit):
                try:
                    return float(freq[:-len(unit)]) * mul
                except ValueError:
                    pass
        return 86400

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        seeds = store.rows(self.MODULE)
        base["pending_seeds"] = sum(1 for s in seeds if s.get("status") == "待爬取")
        base["completed_seeds"] = sum(1 for s in seeds if s.get("status") == "已完成")
        base["total_child_urls"] = sum(len(s.get("_child_urls", [])) for s in seeds)
        return base