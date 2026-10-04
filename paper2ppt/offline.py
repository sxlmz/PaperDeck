# -*- coding: utf-8 -*-
"""Offline 确定性 Agent（无 LLM Key 时的兜底实现）。

职责与 LLM Agent 完全一致，只是"决策"来自规则而非大模型：
  * offline_planner : 学术资产 -> outline.json
      - 优先复用已审定的大纲（workspace/outline.json，视为规划 Agent 的知识沉淀）；
      - 无大纲时从论文结构生成骨架（标题页 + 议程 + 摘要要点 + 章节要点）。
  * offline_designer: outline -> 视觉决策表（expression/layout）
      - content_kind -> 表达形式规则表（数据对比→柱状图、机制→概念图/左图右文、架构→论文图…）。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import OUTLINE_JSON
from .models import load_json, validate_outline


# ---------------------------------------------------------------- Planner
def offline_planner(parsed: dict, outline_path: Path = OUTLINE_JSON) -> dict:
    if outline_path.exists():
        outline = load_json(outline_path)
        try:
            validate_outline(outline)
            print(f"[planner(offline)] 复用已审定大纲: {outline_path.name} "
                  f"（{len(outline['slides'])} 页）")
            return outline
        except Exception as e:  # noqa: BLE001
            print(f"[planner(offline)] 已有大纲校验失败({e})，回退骨架生成")

    # 骨架：从文本结构生成最简大纲
    pages = parsed.get("pages", [])
    head = pages[0]["text"] if pages else ""
    title_match = re.search(r"([A-Z][A-Z0-9 :\-]{10,120})", head)
    title = title_match.group(1).strip() if title_match else parsed.get("title", "未命名论文")

    slides = [
        {"slide_id": "s01", "slide_type": "cover", "title": title,
         "subtitle": "论文解读", "authors": parsed.get("author", ""),
         "affiliation": "", "meta": "", "note": ""},
        {"slide_id": "s02", "slide_type": "agenda", "title": "汇报路线",
         "items": ["研究背景", "方法设计", "实验与结果", "总结与展望"],
         "note": ""},
        {"slide_id": "s03", "slide_type": "bullets", "section": "论文要点",
         "title": "论文概述",
         "bullets": [
             {"head": "摘要", "text": head.replace("\n", " ")[:200]},
             {"head": "建议", "text": "离线骨架大纲仅含摘要要点，请配置 LLM Key 后由 Planner Agent 生成完整大纲。"},
         ],
         "note": ""},
    ]
    outline = {
        "paper": {
            "title": title, "short": "", "authors": parsed.get("author", ""),
            "affiliation": "", "venue": "", "arxiv": "",
        },
        "slides": slides,
    }
    print("[planner(offline)] 骨架大纲生成（建议配置 LLM 获取完整大纲）")
    return outline


# ---------------------------------------------------------------- Designer
# content_kind -> 视觉表达（规则表；LLM 模式给出更细的决策，此处为兜底）
_EXPRESSION_RULES = {
    "data_comparison": {"expression": "bar_chart", "layout": "chart"},
    "data_analysis": {"expression": "line_chart", "layout": "chart"},
    "table": {"expression": "table", "layout": "table"},
    "architecture": {"expression": "paper_figure", "layout": "figure"},
    "mechanism": {"expression": "concept_figure", "layout": "two_col"},
    "concept": {"expression": "concept_figure", "layout": "two_col"},
    "result_stats": {"expression": "text_only", "layout": "bullets"},
    "conclusion": {"expression": "text_only", "layout": "bullets"},
    "motivation": {"expression": "text_only", "layout": "bullets"},
}

_SLIDE_TYPE_LAYOUT = {
    "cover": "cover", "agenda": "agenda", "table": "table", "figure": "figure",
    "chart": "chart", "formula": "formula", "keycards": "keycards",
    "bullets": "bullets", "two_col": "two_col",
}


def _decision_for(slide: dict) -> dict:
    st = slide.get("slide_type")
    if st == "table":
        # 列多的表必须全宽：半宽版式（6.05"）塞 9 列会把每列压到 30~75pt，
        # 英文单词和数字会被逐字折断
        cols = len((slide.get("table") or {}).get("header") or [])
        return {"expression": "table", "layout": "table_wide" if cols >= 6 else "table"}
    # slide_type 已定死版式的页直接按其版式走
    if st in ("cover", "agenda", "figure", "formula", "keycards"):
        return {"expression": st, "layout": st}
    if st == "chart":
        kind = (slide.get("chart") or {}).get("kind", "")
        expr = "line_chart" if "line" in kind else "bar_chart"
        return {"expression": expr, "layout": "chart"}
    if st == "two_col":
        return {"expression": "concept_figure", "layout": "two_col"}
    # bullets 类页按内容类型匹配表达
    kind = slide.get("content_kind", "concept")
    rule = _EXPRESSION_RULES.get(kind, {"expression": "text_only", "layout": "bullets"})
    # 机制类若无示意图则退化为要点页
    if rule["expression"] == "concept_figure" and not (slide.get("figure") or {}).get("kind"):
        return {"expression": "text_only", "layout": "bullets"}
    return dict(rule)


def offline_designer(outline: dict) -> dict:
    decisions = {}
    for s in outline["slides"]:
        decisions[s["slide_id"]] = _decision_for(s)
    print(f"[designer(offline)] 规则决策完成：{len(decisions)} 页")
    return decisions
