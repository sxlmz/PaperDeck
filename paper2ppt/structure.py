# -*- coding: utf-8 -*-
"""结构化解析：PDF -> content_list（类 MinerU content_list.json）。

PyMuPDF 本地实现（无 GPU 版）：
  * 双栏阅读顺序：左栏从上到下 -> 右栏从上到下；
  * 块类型分类：title / text / formula / caption_figure / caption_table；
  * 块带 bbox 坐标；
  * 图/表引用追踪：正文里的 "Figure N" / "Table N" 绑定到对应资产。
真正的公式 LaTeX OCR（UniMERNet/MinerU vlm-engine）需要 8GB+ 显存，
这里只做"公式行隔离"，公式语义交给 LLM 解释。
"""
from __future__ import annotations

import re

import pymupdf

_MATH_RE = re.compile(
    r"[∑∫∏√αβγδεζηθλμξπσφψωΔΣ∈∀∃≤≥≠≈±×÷→↔∞·{}^_]|"
    r"[A-Za-z]\^\(?\(?[i0-9]\)?|R\^\{|x_\{|h_\{|\\frac|\\sum")
_FIG_REF = re.compile(r"\b(?:Figure|Fig\.?)\s*(\d+)")
_TAB_REF = re.compile(r"\bTable\s*(\d+)")


def _classify(text: str, bbox, page_w: float) -> str:
    head = text.strip()
    if re.match(r"^(Figure|Fig\.)\s*\d+", head):
        return "caption_figure"
    if re.match(r"^Table\s*\d+", head):
        return "caption_table"
    # 短行 + 含数学符号/上下标模式 -> 公式
    if len(head) < 160 and _MATH_RE.search(head) and not head.endswith((".", "。")):
        return "formula"
    # 章节标题：短、以数字开头（1 / 2.1）或全大写
    if len(head) < 80 and (re.match(r"^\d+(\.\d+)?\s+[A-Z]", head)
                           or head.isupper()):
        return "title"
    return "text"


def build_content_list(doc) -> list[dict]:
    items: list[dict] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        d = page.get_text("dict")
        mid = page.rect.width / 2
        cols = {"L": [], "R": []}
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            bx = pymupdf.Rect(b["bbox"])
            cols["L" if bx.x0 < mid else "R"].append(b)
        for col_key in ("L", "R"):
            for b in sorted(cols[col_key], key=lambda x: x["bbox"][1]):
                lines = []
                for ln in b.get("lines", []):
                    lines.append("".join(sp.get("text", "") for sp in ln.get("spans", [])))
                text = "\n".join(lines).strip()
                if not text:
                    continue
                items.append({
                    "type": _classify(text, b["bbox"], page.rect.width),
                    "page": pno + 1,
                    "bbox": [round(v, 1) for v in b["bbox"]],
                    # 这里是**结构索引**（类型 + 坐标 + 阅读顺序），不是全文载体：
                    # 完整正文在 parsed["pages"] 里，Planner 读的是那一份。
                    # 此处保留 2000 字足以覆盖分类、引用号匹配与关键句抽取。
                    "text": text[:2000],
                })
    return items


def build_citation_links(content_list: list[dict], figures: list[dict],
                         tables: list[dict]) -> dict:
    """正文 -> 图/表 的显式链接：{fig_no: [page, ...], table_no: [page, ...]}。"""
    fig_links: dict[int, list[int]] = {}
    tab_links: dict[int, list[int]] = {}
    for it in content_list:
        if it["type"] in ("caption_figure", "caption_table"):
            continue
        for m in _FIG_REF.finditer(it["text"]):
            fig_links.setdefault(int(m.group(1)), set()).add(it["page"])
        for m in _TAB_REF.finditer(it["text"]):
            tab_links.setdefault(int(m.group(1)), set()).add(it["page"])
    fig_map = {f["fig_no"]: f for f in figures}
    out = {"figures": [], "tables": []}
    for fno, pages in sorted(fig_links.items()):
        f = fig_map.get(fno)
        out["figures"].append({
            "fig_no": fno,
            "asset": f["path"] if f else None,
            "caption": f["caption"] if f else "",
            "mentioned_on_pages": sorted(pages),
        })
    for tno, pages in sorted(tab_links.items()):
        out["tables"].append({"table_no": tno, "mentioned_on_pages": sorted(pages)})
    return out
