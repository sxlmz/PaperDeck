# -*- coding: utf-8 -*-
"""Understand Agent 节点：关键页 VLM 结构化理解。

位置：parser -> **understand** -> planner。
职责：把「图/表/公式」这类 PDF 文本层拿不到语义的部分，用 VLM 转成结构化事实，
写进 `parsed["vlm"]`，供 Planner 引用精确数值而不是含糊的趋势描述。

无视觉模型 / --no-vlm / 已有结果时直接返回 {}，图仍然合法（离线可用）。
"""
from __future__ import annotations

from pathlib import Path

from .. import vlm_parse
from ..config import PARSED_JSON
from ..models import save_json
from .state import AgentState


def understand_node(state: AgentState) -> dict:
    llm = state.get("_llm")
    parsed = state.get("parsed") or {}
    logs = list(state.get("logs") or [])

    if not parsed:
        return {}
    if state.get("use_vlm") is False:
        logs.append("[UnderstandAgent] --no-vlm：跳过 VLM 解析")
        print("[UnderstandAgent] --no-vlm：跳过 VLM 解析")
        return {"logs": logs}
    if llm is None or not getattr(llm.cfg, "vision_model", ""):
        logs.append("[UnderstandAgent] 未配置视觉模型，跳过（Planner 走纯文本）")
        return {"logs": logs}
    if parsed.get("vlm") and not state.get("refresh"):
        logs.append("[UnderstandAgent] 复用已有 VLM 理解结果")
        return {"logs": logs}

    try:
        vlm = vlm_parse.understand(parsed, llm, Path(state["pdf_path"]),
                                   refresh=bool(state.get("refresh")))
    except Exception as e:  # noqa: BLE001
        logs.append(f"[UnderstandAgent] VLM 解析失败（不阻断）：{e}")
        print(f"[UnderstandAgent] VLM 解析失败（不阻断）：{e}")
        return {"logs": logs}

    if not vlm:
        logs.append("[UnderstandAgent] 未选出关键页，跳过")
        return {"logs": logs}

    parsed["vlm"] = vlm
    save_json(parsed, PARSED_JSON)

    # 关键页是被"挑出来精读"的页，不是被丢弃的内容 —— 论文全文仍完整喂给
    # Planner。这里如实记账，避免把「选择性精读」误当成「截断」。
    from ..coverage import Coverage
    cov = Coverage(state.get("coverage"))
    total_pages = len(parsed.get("pages") or [])
    n_failed = int(vlm.get("n_failed") or 0)
    cov.record("understand/VLM 关键页精读", total=total_pages,
               sent=int(vlm.get("n_pages") or 0), unit="页",
               detail=("按信息密度打分挑选；论文全文仍完整送 Planner；"
                       f"送审块 {vlm.get('n_calls', 0)} 成功 / {n_failed} 失败"),
               dropped_items=[] if not n_failed else [f"{n_failed} 个块解析失败"])

    msg = (f"[UnderstandAgent] 关键页 {vlm.get('n_pages')}/{total_pages} 页精读 / "
           f"VLM 调用 {vlm.get('n_calls')} 次（图 {len(vlm.get('figures') or {})}、"
           f"表 {len(vlm.get('tables') or {})}、公式 {len(vlm.get('formulas') or {})}）")
    logs.append(msg)
    print(msg)
    return {"parsed": parsed, "logs": logs, "coverage": cov.records}
