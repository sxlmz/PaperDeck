# -*- coding: utf-8 -*-
"""html_fixer_bridge.py：Router -> RAG -> HTML Fixer 的桥接工具。

把「缺数据类 issue」转成可执行的检索动作，并收集证据。独立成模块是为了
router_agent 与单测都能复用，且不产生 LangGraph 状态耦合。
"""
from __future__ import annotations

from typing import Callable

from ..router import build_rag_query


def build_queries(issues: list[dict]) -> list[str]:
    """一条/多条缺数据 issue -> 检索 query 列表。

    每条 issue 最多出 1 个 query（表号/图号优先，其次 desc 本体）。
    """
    queries = []
    for it in issues or []:
        q = build_rag_query(it)
        if q and q not in queries:
            queries.append(q)
    return queries


def collect_evidence(rag_search: Callable, queries: list[str],
                     top_k: int = 3) -> list[dict]:
    """多 query 混合检索，合并去重后按分数降序返回。"""
    out: dict[str, dict] = {}
    for q in queries:
        try:
            evs = rag_search(q, top_k=top_k)
        except Exception as e:  # noqa: BLE001
            print(f"[html_fixer_bridge] RAG 检索失败（{q[:40]}）: {e}")
            continue
        for ev in evs or []:
            cid = ev.get("chunk_id")
            key = f"{ev.get('page')}:{ev.get('kind')}:{cid}"
            if key not in out or (ev.get("score") or 0) > (out[key].get("score") or 0):
                out[key] = ev
    return sorted(out.values(), key=lambda e: -(e.get("score") or 0))
