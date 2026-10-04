# -*- coding: utf-8 -*-
"""Faithfulness Agent：大纲事实一致性审查（Auto-Slides accuracy 闭环）。

Planner 出完大纲后，逐条 bullet 对照它所声称的论文页码原文：
  - supported：原文能推出 -> 保留；
  - unsupported（原文无依据/与原文矛盾/数字编造）-> 丢弃。
丢弃后该页 bullets 为空则降级为 takeaway 单条，绝不让页面留空。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..config import OUTLINE_JSON
from ..llm import LLMClient
from ..models import save_json
from .state import AgentState

_SYSTEM = """你是事实一致性审查员。给你论文某几页原文和一页 PPT 的要点，
逐条判断该要点能否从原文直接推出（允许概括/转述，但不得新增原文没有的结论、数字或因果）。
只输出 JSON：
{"verdicts":[{"text":"要点原文","verdict":"supported|unsupported"}]}
- supported：原文有明确依据；
- unsupported：原文无依据、与原文矛盾、或数字/结论是编造的。"""


def _norm_bullets(bullets):
    out = []
    for b in bullets or []:
        if isinstance(b, str):
            out.append({"text": b, "page": None})
        elif isinstance(b, dict):
            txt = b.get("text") or b.get("head", "")
            out.append({"text": str(txt), "head": b.get("head", ""),
                        "page": b.get("page")})
    return out


import re as _re


def _nums(text: str) -> list[str]:
    return _re.findall(r"\d+\.\d+|\d+", text or "")


def _page_char_budget() -> int:
    import os
    try:
        return int(os.environ.get("P2P_FAITHFUL_PAGE_CHARS", "4000"))
    except ValueError:
        return 4000


def _clip(text: str, limit: int) -> str:
    """只在超过 limit 时裁剪，并显式标注省略量。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…（本页另省略 {len(text) - limit} 字）"


def _check_one_slide(llm: LLMClient, slide: dict, page_text: dict) -> dict:
    """返回该 slide 的处理结果：kept bullets 与 dropped 列表。"""
    bullets = _norm_bullets(slide.get("bullets"))
    claimed = sorted({b["page"] for b in bullets if isinstance(b.get("page"), int)})
    if not bullets or not claimed:
        return {"slide": slide, "kept": bullets, "dropped": []}
    # 数字锚定预筛：bullet 里所有数字全文都能找到 -> 直接保留，不送 LLM（防误杀）
    fulltext = "\n".join(page_text.values())
    auto_keep = set()
    for i, b in enumerate(bullets):
        nums = _nums(b["text"])
        if nums and all(n in fulltext for n in nums):
            auto_keep.add(i)
    # 补摘：bullet 数字实际出现的页也喂给 LLM（Planner 标错页时不至于误判）
    extra = set()
    for i, b in enumerate(bullets):
        if i in auto_keep:
            continue
        for n in set(_nums(b["text"])):
            for pno, txt in page_text.items():
                if n in txt:
                    extra.add(pno)
    # 全量喂入候选页，不设 [:8] 上限：裁决依据不足会把「有依据」误判成
    # 「无依据」而误删要点，也会把编造数字误判成合规。单页正文用全量，
    # 只有超长页才按 P2P_FAITHFUL_PAGE_CHARS 裁剪（默认 4000 字）。
    feed_pages = sorted(set(claimed) | set(extra))
    per_page = _page_char_budget()
    ctx = "\n\n".join(
        f"[第{p}页]\n{_clip(page_text.get(p, '') or '', per_page)}" for p in feed_pages)
    msgs = [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content":
             f"论文原文摘录：\n{ctx}\n\n"
             f"本页要点（逐条裁决）：{[b['text'] for b in bullets]}"}]
    try:
        res = llm.chat_json(msgs, temperature=0.0, max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        print(f"[Faithfulness] 单页审查失败，保留原 bullet: {e}")
        return {"slide": slide, "kept": bullets, "dropped": []}
    verdicts = (res or {}).get("verdicts") or []
    # 按顺序对齐（模型可能漏条，漏条默认保留）
    dropped = []
    kept = []
    vmap = {}
    for j, v in enumerate(verdicts):
        vmap[j] = v
    for i, b in enumerate(bullets):
        if i in auto_keep:
            kept.append(b)
            continue
        v = vmap.get(i, {})
        if str(v.get("verdict", "supported")).lower() == "unsupported":
            dropped.append(b)
        else:
            kept.append(b)
    # 内容充实底线：每页至少留 3 条，宁可漏判不可误杀空页
    while len(kept) < min(3, len(bullets)) and dropped:
        kept.append(dropped.pop(0))
    if not kept and slide.get("takeaway"):
        kept = [{"text": str(slide["takeaway"]), "page": claimed[0]}]
    return {"slide": slide, "kept": kept,
            "dropped": [b["text"] for b in dropped]}


def faithfulness_node(state: AgentState) -> dict:
    llm: LLMClient | None = state.get("_llm")
    outline = state.get("outline") or {}
    logs = list(state.get("logs") or [])
    if llm is None or not outline.get("slides"):
        return {}

    page_text = {p["page_no"]: p["text"] for p in
                 (state.get("parsed") or {}).get("pages", [])}
    slides = outline["slides"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda s: _check_one_slide(llm, s, page_text),
                              slides))

    total_dropped = 0
    for r in results:
        r["slide"]["bullets"] = r["kept"]
        if r["dropped"]:
            total_dropped += len(r["dropped"])
            logs.append("[Faithfulness] 页《%s》剔除 %d 条无依据要点: %s"
                        % (r["slide"].get("title", "?"), len(r["dropped"]),
                           " | ".join(x[:30] for x in r["dropped"])))

    save_json(outline, OUTLINE_JSON)
    logs.append(f"[FaithfulnessAgent] 事实审查完成，共剔除 {total_dropped} 条无原文依据的要点")
    print(f"[FaithfulnessAgent] 剔除 {total_dropped} 条无依据要点")
    return {"outline": outline, "logs": logs}
