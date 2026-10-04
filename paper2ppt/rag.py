# -*- coding: utf-8 -*-
"""检索缓存机制（Planner Agent 的 fetch_references 工具）。

设计目标（对应方案中的"引入检索缓存"）：
  * 论文引用经典文献时，Agent 可获取权威解释补充背景知识，避免幻觉；
  * 所有检索结果按 (paper_id, 概念词) 做磁盘缓存，二次命中零网络成本；
  * 当前里程碑不接外部搜索（P2P_RAG_ENABLED=0），工具返回缓存命中或提示未启用；
    后续接入 arXiv / Semantic Scholar 时只需实现 `_fetch_remote`。

缓存结构（JSON Lines）：
  reference_cache/
    v1/
      <paper_id>.jsonl   每行一条：{"concept":..., "summary":..., "source":..., "cached_at":...}
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .config import WORKSPACE

CACHE_DIR = Path(os.environ.get("P2P_RAG_CACHE_DIR", str(WORKSPACE / "reference_cache")))
ENABLED = os.environ.get("P2P_RAG_ENABLED", "0") == "1"


def _safe_key(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", s).strip("_") or "x"


class ReferenceCache:
    """磁盘检索缓存：get/set + 过期策略。"""

    def __init__(self, paper_id: str, cache_dir: Path = CACHE_DIR):
        self.paper_id = _safe_key(paper_id)
        self.path = cache_dir / "v1" / f"{self.paper_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                self._index[item["concept"]] = item
            except Exception:  # noqa: BLE001
                continue

    def _flush(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            for item in self._index.values():
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def get(self, concept: str, max_age_days: float = 90):
        item = self._index.get(concept)
        if not item:
            return None
        age = (time.time() - item.get("cached_at", 0)) / 86400
        if age > max_age_days:
            return None
        return item

    def set(self, concept: str, summary: str, source: str) -> dict:
        item = {"concept": concept, "summary": summary,
                "source": source, "cached_at": time.time()}
        self._index[concept] = item
        self._flush()
        return item


def _fetch_remote(concept: str) -> str | None:
    """占位：接入 arXiv/Semantic Scholar 的远程检索。
    当前里程碑未启用（P2P_RAG_ENABLED=0），返回 None 表示未启用。"""
    return None


def fetch_references(paper_id: str, concepts: list[str]) -> dict:
    """Planner Agent 的引用补全工具。
    返回 {concept: {summary, source, from_cache}}；未命中的概念标记未启用/未命中。"""
    cache = ReferenceCache(paper_id)
    out = {}
    for c in concepts:
        hit = cache.get(c)
        if hit:
            out[c] = {**hit, "from_cache": True}
            continue
        if not ENABLED:
            out[c] = {"summary": "", "source": "RAG 未启用（P2P_RAG_ENABLED=0）",
                      "from_cache": False, "note": "未检索"}
            continue
        try:
            summary = _fetch_remote(c)
        except Exception as e:  # noqa: BLE001
            out[c] = {"summary": "", "source": f"检索失败: {e}",
                      "from_cache": False}
            continue
        if summary:
            item = cache.set(c, summary, "remote")
            out[c] = {**item, "from_cache": False}
        else:
            out[c] = {"summary": "", "source": "未找到权威解释",
                      "from_cache": False}
    return out
