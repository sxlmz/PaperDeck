# -*- coding: utf-8 -*-
"""router.py：审查反馈分发（Router Agent）。

Critic 产出的 issue 不再一股脑喂给单个 Designer LLM，而是先由 Router
**确定性分类**（不调 LLM 猜），再按类别分派给对应的子能力：

  missing_data  —— 缺数据/缺图/缺表（VLM 说"补 Figure 1"、"表格缺 MAE 列"）
                   -> parser+RAG：反馈改造成 query -> 混合检索论文原文 chunk
                   -> 结果连同缺失反馈一起交给 HTML Fixer 落进页面
  layout        —— 布局/审美/溢出/太空（H1..H6、S2..S5、ugly/clutter）
                   -> HTML Fixer：直接改 HTML 源码（自由 CSS）
  factuality    —— 事实存疑（数字/结论与原文不符）
                   -> faithfulness 裁决：逐条对照论文原文判定 supported/unsupported

分类判据是**确定性规则**（issue.type / issue.rule / 关键词），不是 LLM 猜测：
  - rule 在 H1..H6/S2..S5         -> layout（确定性几何）
  - type 前缀 vlm: 且命中缺失关键词 -> missing_data
  - type 前缀 vlm: 且命中事实关键词 -> factuality
  - type 前缀 vlm: 其余           -> layout
  - 无分类                           -> layout（默认兜底）

输出：{"missing_data": [...], "layout": [...], "factuality": [...]}，
空类别不出现。
"""
from __future__ import annotations

import re

#: 确定性几何规则 -> layout
_LAYOUT_RULES = {"H1", "H2", "H3", "H4", "H6", "S2", "S4", "S5"}

#: 缺失类关键词（VLM/检查器说"缺图/缺数据/占位"时 -> parser+RAG）
_MISSING_PATTERNS = [
    r"缺(少|失)?\s*(图|表|数据|信息|内容|数字|列)",
    r"缺(少|失)?\s*[^，。;；]{0,6}\s*(列|行|字段)",
    r"图\s*\d+\s*(未|没有|缺失|不存在|空白)",
    r"表\s*\d+\s*(未|没有|缺失|不存在|空白)",
    r"占位",
    r"placeholder",
    r"没有(提供|给出|找到).{0,12}(图|表|数据|数值|数字)",
    r"需要(补充|加入|提供).{0,12}(图|表|数据|数值|数字)",
    r"数据?(缺失|不全|没查到)",
    r"columns?\s*(missing|absent)",
    r"figure\s*\d+\s*(missing|absent|not\s+found)",
    r"table\s*\d+\s*(missing|absent|not\s+found)",
]

#: 事实存疑类关键词 -> faithfulness
_FACTUAL_PATTERNS = [
    r"与原文(不符|不一致|矛盾)",
    r"原文(没有|无|未提及|找不到).{0,16}(数字|结论|说法|依据)",
    r"事实(错误|存疑|不准确)",
    r"数字(错误|对不上|与原文不符)",
    r"(疑似|可能|恐怕是)?(幻觉|编造)",
    r"inconsistent\s+with\s+(the\s+)?(paper|source|original)",
    r"not\s+supported\s+by",
    r"hallucinat",
    r"fabricat",
]

_MISSING_RE = [re.compile(p, re.IGNORECASE) for p in _MISSING_PATTERNS]
_FACTUAL_RE = [re.compile(p, re.IGNORECASE) for p in _FACTUAL_PATTERNS]


def classify(issue: dict) -> str:
    """单条 issue -> 'missing_data' | 'factuality' | 'layout'（确定性规则）。"""
    rule = str(issue.get("rule") or "")
    if rule in _LAYOUT_RULES:
        return "layout"
    typ = str(issue.get("type") or "")
    desc = str(issue.get("desc") or "")
    text = f"{typ} {desc}"
    if _MISSING_RE and any(r.search(text) for r in _MISSING_RE):
        return "missing_data"
    if _FACTUAL_RE and any(r.search(text) for r in _FACTUAL_RE):
        return "factuality"
    return "layout"


def route(issues: list[dict]) -> dict[str, list[dict]]:
    """整批 issue -> 三个桶。原 issue 对象原样保留（不丢 slide/severity/desc）。"""
    buckets: dict[str, list[dict]] = {"missing_data": [], "layout": [],
                                     "factuality": []}
    for it in issues or []:
        buckets[classify(it)].append(it)
    return {k: v for k, v in buckets.items() if v}


def summarize(route_map: dict[str, list[dict]]) -> str:
    parts = []
    label = {"missing_data": "缺数据/图（-> RAG）", "layout": "布局/审美（-> HTML Fixer）",
             "factuality": "事实存疑（-> Faithfulness）"}
    for k in ("missing_data", "layout", "factuality"):
        v = route_map.get(k) or []
        if v:
            parts.append(f"{label[k]} {len(v)} 条")
    return "；".join(parts) if parts else "（无待分发问题）"


def build_rag_query(issue: dict) -> str:
    """把一条 missing_data issue 改造成 RAG 检索 query。

    优先取 issue 里明确提到的图/表/数值；否则用 desc 本体。Router 不调 LLM
    猜——query 是确定性提取（表号/图号/关键词），LLM 改造只发生在 HTML Fixer
    确实需要更自然表述时（见 html_fixer）。
    """
    desc = str(issue.get("desc") or "")
    for pat in (r"(?:表|Table)\s*(\d+)", r"(?:图|Figure|Fig\.?)\s*(\d+)"):
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            kind = "表" if "表" in pat or pat.startswith("Table") else "图"
            num = m.group(1)
            return f"{kind}{num} " + desc[:200]
    return desc[:300]
