# -*- coding: utf-8 -*-
"""按图注裁剪论文原始图（成熟 ppt-agent 的标准做法）。

1. 在页面文本层找 "Figure" 图注位置，从其后文本解析编号；
2. 收集图注上方的 image 矩形 + 矢量绘图 bbox，合并成图区；
3. 整页高 DPI 渲染裁剪区（矢量+位图一起出来，和论文里一模一样）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf


def extract_figures(doc, outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    figures: list[dict] = []
    seen = set()
    for pno in range(doc.page_count):
        page = doc[pno]
        for hit in page.search_for("Figure"):
            # 图注必须在 "Figure" 后紧跟数字（排除正文里的 Figure N 引用句）
            head = page.get_text("text", clip=pymupdf.Rect(hit.x0, hit.y0,
                                                           hit.x0 + 90, hit.y1 + 2))
            m = re.match(r"Figure\s+(\d+)", head)
            if not m:
                continue
            fno = int(m.group(1))
            # 图注 typically 以句号/冒号结尾后跟描述；正文引用句常出现在行间
            if fno in seen:
                continue
            elems = []
            for inf in page.get_image_info():
                elems.append(pymupdf.Rect(inf["bbox"]))
            for dr in page.get_drawings():
                rr = dr["rect"]
                if rr.width > 8 and rr.height > 8:
                    elems.append(rr)
            col_center = (hit.x0 + hit.x1) / 2
            page_mid = page.rect.width / 2
            region = None
            for e in elems:
                if e.y1 > hit.y0 - 4:
                    continue
                if hit.y0 - e.y1 > 200:
                    continue
                ecx = (e.x0 + e.x1) / 2
                if abs(ecx - col_center) > page_mid + 20:
                    continue
                region = e if region is None else (region | e)
            if region is None:
                # 上方没有真正的图簇：这是正文交叉引用句，跳过
                continue
            region.x0 -= 12; region.x1 += 12
            region.y0 -= 12; region.y1 += 4
            region &= page.rect
            if region.width < 90 or region.height < 60:
                continue
            pix = page.get_pixmap(clip=region, matrix=pymupdf.Matrix(2.6, 2.6))
            path = outdir / f"fig{fno:02d}_p{pno+1}.png"
            pix.save(str(path))
            cap = page.get_text("text", clip=pymupdf.Rect(
                hit.x0, hit.y0, page.rect.width - 36, hit.y0 + 140)
            ).strip().replace("\n", " ")[:220]
            figures.append({"fig_no": fno, "page": pno + 1,
                            "path": str(path), "caption": cap})
            seen.add(fno)
    return figures
