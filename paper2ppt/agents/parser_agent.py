# -*- coding: utf-8 -*-
"""Parser Agent 节点：PDF/LaTeX -> 结构化学术资产（parsed_paper.json）。

本节点为"工具型 Agent"：调用 parser.parse_pdf 完成高保真解析
（文本结构 / 图片清单 / 表格 / 元信息），输出可供后续 Agent 消费的结构化资产。
公式以 PDF 文本层 LaTeX 直取；扫描件/公式 OCR 由 MinerU 替换（见 README 路线）。
"""
from __future__ import annotations

from pathlib import Path

from .. import parser as parser_mod
from ..config import ASSETS_DIR, PARSED_JSON, PAPER_DIR
from .state import AgentState


def parser_node(state: AgentState) -> dict:
    pdf_path = state.get("pdf_path") or ""
    if not pdf_path or not Path(pdf_path).exists():
        pdfs = sorted(PAPER_DIR.glob("*.pdf"))
        if not pdfs:
            raise FileNotFoundError(f"{PAPER_DIR} 下未找到输入 PDF")
        pdf_path = str(pdfs[0])

    # Redis 缓存命中则直接复用（键 = PDF 内容哈希 + 解析版本）；
    # 缓存里的解析质量不达标时会自动重解析并更新缓存
    parsed, cached = parser_mod.parse_pdf_cached(
        Path(pdf_path), PARSED_JSON,
        use_cache=state.get("use_cache", True),
        refresh=bool(state.get("reparse")),
        force=bool(state.get("reparse")))

    # 论文关键图资产（Figure 1 / Figure 3）：assets 缺失时裁剪生成（幂等）
    if not sorted(ASSETS_DIR.glob("fig*.png")):
        try:
            import pymupdf
            doc = pymupdf.open(pdf_path)
            crops = [  # (page_1based, rect_per_mille, filename)
                (4, (0.165, 0.095, 0.835, 0.487), "fig1_architecture"),
                (14, (0.200, 0.548, 0.812, 0.688), "fig3_forecast_visualization"),
            ]
            for pno, (x1, y1, x2, y2), name in crops:
                page = doc[pno - 1]
                r = pymupdf.Rect(page.rect.width * x1, page.rect.height * y1,
                                 page.rect.width * x2, page.rect.height * y2)
                page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=r, alpha=False) \
                    .save(str(ASSETS_DIR / f"{name}.png"))
        except Exception as e:  # noqa: BLE001
            print(f"[ParserAgent] 图资产裁剪失败（不阻断）：{e}")

    ok, qmsg = parser_mod.quality_report(parsed)
    src = "缓存" if cached else "重新解析"
    log = (state.get("logs") or []) + [
        f"[ParserAgent] 完成（{src}）：{parsed['total_pages']} 页 / "
        f"{len(parsed['image_inventory'])} 图 / {len(parsed['tables'])} 表"
        f"{'' if ok else ' ⚠ 解析质量偏低'}"
    ]
    print(f"[ParserAgent] {src}：{qmsg}")
    return {"parsed": parsed, "logs": log, "pdf_path": pdf_path}
