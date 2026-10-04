# -*- coding: utf-8 -*-
"""html_preview.py：design.json -> 每页 HTML 审查稿（1280x720）+ Edge 截图。

路径 A 的「审查稿」：最终交付仍是 python-pptx，HTML 只用于让确定性检查器
和 VLM/LLM 「看见」设计稿，从而给出可执行的修改意见。

**渲染口径必须与 renderer.py 保持一致**，否则检查器在 HTML 上发现的问题
无法映射回 design.json 的 layout：
  * 画布 13.333in x 7.5in = 1280x720 px（1in = 96px）
  * 坐标全部取自 design.json 的 layout（英寸 -> px）
  * 字号 pt -> px（x * 4/3）
  * 主题色从 design["theme"] 生成 CSS 变量
  * 图片 contain-fit 进版位（同 renderer.add_picture_fit）
  * 表格列宽用 metrics.plan_columns（同 renderer.add_table）
  * 页眉页脚坐标与 renderer.HEADER_* 一致

每个布局盒子都带 data-box 标记，供 layout_checker.py 注入 JS 做确定性检查。
"""
from __future__ import annotations

import base64
import html as html_mod
from pathlib import Path
from typing import Any

from PIL import Image

from . import metrics
from .config import ASSETS_DIR, DESIGN_JSON, WORKSPACE
from .models import load_json

#: 1 英寸 = 96px（13.333 x 7.5 in = 1280 x 720）
PX_PER_IN = 96.0
PT_TO_PX = 4.0 / 3.0
SLIDE_W_PX, SLIDE_H_PX = 1280, 720

#: 页眉页脚坐标（英寸，与 renderer.py HEADER_* 一致）
HEADER_SECTION = (0.62, 0.28, 8.0, 0.30)
HEADER_TITLE = (0.62, 0.56, 12.1, 0.70)
HEADER_DIVIDER = (0.62, 1.32, 12.1, 0.035)
HEADER_FOOTER_LEFT = (0.62, 7.12, 6.0, 0.3)
HEADER_FOOTER_RIGHT = (11.6, 7.12, 1.15, 0.3)

#: 卡片版式回退（同 renderer._card_specs）
_CARD_DEFAULTS = {"x0": 0.62, "y0": 1.95, "w_total": 12.10, "height": 3.55, "gap": 0.30}

_FONT_STACK = ('"Microsoft YaHei", "微软雅黑", "Segoe UI", sans-serif')


def _in(coord: float) -> int:
    return int(round(coord * PX_PER_IN))


def _px(pt: float) -> int:
    return int(round(pt * PT_TO_PX))


def _css_color(hex_color: str) -> str:
    hex_color = str(hex_color or "").lstrip("#")
    if len(hex_color) != 6:
        return "#000000"
    return "#" + hex_color


def _esc(text: Any) -> str:
    return html_mod.escape(str(text if text is not None else ""))


def _bullet_lines(slide: dict) -> list[tuple[str, str]]:
    """bullets -> [(head, text)]，口径同 renderer.add_bullets。"""
    out = []
    for b in (slide.get("bullets") or []):
        if isinstance(b, dict):
            out.append((str(b.get("head") or ""), str(b.get("text") or "")))
        elif isinstance(b, str):
            out.append(("", b))
        else:
            out.append(("", str(b)))
    return out


def _image_data_uri(path: str | Path) -> str:
    """图片转 base64 data URI（内嵌最稳：避免 file:// 跨目录被 Edge 拦）。"""
    p = Path(path)
    if not p.is_absolute():
        # design.json 里的路径可能是相对项目根的
        cands = [p, WORKSPACE / p, ASSETS_DIR / p.name]
        p = next((c for c in cands if c.exists()), p)
    if not p.exists():
        return ""
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _img_fit_size(path: str | Path, box: tuple) -> tuple[int, int, int, int]:
    """图片 contain-fit 进版位（同 renderer.add_picture_fit），返回 (x, y, w, h) px。"""
    bx, by, bw, bh = (_in(v) for v in box)
    p = Path(path)
    if not p.is_absolute():
        cands = [p, WORKSPACE / p, ASSETS_DIR / p.name]
        p = next((c for c in cands if c.exists()), p)
    try:
        with Image.open(p) as im:
            iw, ih = im.size
    except OSError:
        iw = ih = 1
    scale = min(bw / max(1, iw), bh / max(1, ih))
    w, h = int(iw * scale), int(ih * scale)
    x = bx + (bw - w) // 2
    y = by + (bh - h) // 2
    return x, y, w, h


def _table_html(slide: dict, theme: dict) -> str:
    """表格 -> <table>，列宽用 metrics.plan_columns（同 renderer.add_table）。"""
    tb = slide.get("table") or {}
    header = [str(c or "") for c in (tb.get("header") or [])]
    rows = [[str(c or "") for c in (r or [])] for r in (tb.get("rows") or [])]
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])
    lay = slide.get("layout") or {}
    box = lay.get("table_box") or (0.62, 1.60, 6.05, 3.65)
    fractions, _size, _row_h = metrics.plan_columns(
        header, rows, box[2], box[3], 12.5)
    tot = sum(fractions) or 1.0
    widths_px = [_in(box[2] * f / tot) for f in fractions]

    head_bg = _css_color(theme.get("th_head_bg", "#1F3864"))
    head_text = _css_color(theme.get("th_head_text", "#FFFFFF"))
    light = _css_color(theme.get("light", "#F2F6FB"))
    dark = _css_color(theme.get("darktext", "#262626"))
    row_alt = _css_color(theme.get("row_alt", "#FFFFFF"))

    parts = ['<table class="ppt-table" style="width:100%;border-collapse:collapse">']
    parts.append("<colgroup>")
    for w in widths_px:
        parts.append(f'<col style="width:{w}px">')
    parts.append("</colgroup>")
    parts.append("<thead>")
    parts.append("<tr>")
    for ci in range(n_cols):
        cell = header[ci] if ci < len(header) else ""
        parts.append(
            f'<th style="background:{head_bg};color:{head_text};'
            f'padding:3px 6px;font-size:12.5px;text-align:center;'
            f'font-weight:bold;word-wrap:break-word">{_esc(cell)}</th>')
    parts.append("</tr>")
    parts.append("</thead><tbody>")
    for ri, row in enumerate(rows):
        bg = light if ri % 2 == 0 else row_alt
        parts.append(f'<tr style="background:{bg}">')
        for ci in range(n_cols):
            cell = row[ci] if ci < len(row) else ""
            parts.append(
                f'<td style="color:{dark};padding:3px 6px;font-size:12.5px;'
                f'text-align:center;word-wrap:break-word">{_esc(cell)}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _card_specs(n: int, lay: dict) -> list[tuple]:
    """卡片坐标（英寸），同 renderer._card_specs。"""
    preset = lay.get("card_specs") or []
    if n == len(preset) and n > 0:
        return [tuple(spec) for spec in preset]
    n = max(1, n)
    x0, y0, w_total, height, gap = (
        _CARD_DEFAULTS["x0"], _CARD_DEFAULTS["y0"],
        _CARD_DEFAULTS["w_total"], _CARD_DEFAULTS["height"], _CARD_DEFAULTS["gap"])
    cw = (w_total - gap * (n - 1)) / n
    return [(x0 + i * (cw + gap), y0, cw, height) for i in range(n)]


def _header_footer_html(design: dict, slide: dict, index: int, total: int,
                        theme: dict) -> str:
    """页眉页脚（坐标与 renderer._header_footer 一致）。"""
    primary = _css_color(theme.get("primary", "#1F3864"))
    secondary = _css_color(theme.get("secondary", "#2E75B6"))
    accent = _css_color(theme.get("accent", "#ED7D31"))
    gray = _css_color(theme.get("gray", "#595959"))
    sec = str(slide.get("section") or "")
    title = str(slide.get("title") or "")
    if sec and title.startswith(sec):
        sec = ""
    paper = design.get("paper") or {}
    short = paper.get("short") or (paper.get("title") or "")[:38]
    left = " · ".join(x for x in (str(short), str(paper.get("venue") or "")) if x)

    parts = []
    if sec:
        sx, sy, sw, sh = HEADER_SECTION
        parts.append(
            f'<div data-box="section_tag" style="position:absolute;'
            f'left:{_in(sx)}px;top:{_in(sy)}px;width:{_in(sw)}px;height:{_in(sh)}px;'
            f'font-size:{_px(11)}px;font-weight:bold;color:{accent}">{_esc(sec)}</div>')
    tx, ty, tw, th = HEADER_TITLE
    title_size = (slide.get("style") or {}).get("title_size") or metrics.fit_font_size(
        [title], HEADER_TITLE[2], HEADER_TITLE[3], 25.0, min_size=15.0, line_spacing=1.15)
    parts.append(
        f'<div data-box="title" style="position:absolute;'
        f'left:{_in(tx)}px;top:{_in(ty)}px;width:{_in(tw)}px;height:{_in(th)}px;'
        f'font-size:{_px(title_size)}px;font-weight:bold;color:{primary};'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_esc(title)}</div>')
    dx, dy, dw, dh = HEADER_DIVIDER
    parts.append(
        f'<div data-box="divider" style="position:absolute;'
        f'left:{_in(dx)}px;top:{_in(dy)}px;width:{_in(dw)}px;height:{_in(dh)}px;'
        f'background:{secondary}"></div>')
    if left:
        fx, fy, fw, fh = HEADER_FOOTER_LEFT
        parts.append(
            f'<div data-box="footer_left" style="position:absolute;'
            f'left:{_in(fx)}px;top:{_in(fy)}px;width:{_in(fw)}px;height:{_in(fh)}px;'
            f'font-size:{_px(9)}px;color:{gray};white-space:nowrap;overflow:hidden">{_esc(left)}</div>')
    rx, ry, rw, rh = HEADER_FOOTER_RIGHT
    parts.append(
        f'<div data-box="footer_right" style="position:absolute;'
        f'left:{_in(rx)}px;top:{_in(ry)}px;width:{_in(rw)}px;height:{_in(rh)}px;'
        f'font-size:{_px(9)}px;color:{gray};text-align:right;white-space:nowrap">{index:02d} / {total:02d}</div>')
    return "".join(parts)


def _bullets_html(slide: dict, box: tuple, base_size: float, theme: dict,
                  space_after_pt: float = 10.0, anchor_middle: bool = True,
                  box_name: str = "body_box") -> str:
    """bullets -> 绝对定位文本块。字号用 metrics.fit_font_size（同 renderer）。

    **容器位置必须保持版式坐标原值**：renderer.add_bullets 的 anchor=MIDDLE
    是「文字在框内垂直居中」，不是「把框往下移」。此前把框 top 下移的做法
    会让 body_box 底部越界、与页脚重叠，检查器连锁报一堆 H1/H6 误报。
    垂直居中改为容器内部 flex 实现（justify-content:center）。
    """
    bx, by, bw, bh = box
    dark = _css_color(theme.get("darktext", "#262626"))
    primary = _css_color(theme.get("primary", "#1F3864"))
    entries = _bullet_lines(slide)
    texts = [f"{h}：{t}" if h else t for h, t in entries]
    size = metrics.fit_font_size(texts, bw, bh, base_size, min_size=9.0,
                                 line_spacing=1.22, space_after_pt=space_after_pt)
    justify = "display:flex;flex-direction:column;justify-content:center;" if anchor_middle else ""

    lines = []
    for head, text in entries:
        if head:
            lines.append(
                f'<div class="bullet" data-text="1" style="color:{primary};'
                f'font-size:{_px(size)}px;font-weight:bold;margin-bottom:{_px(space_after_pt)}px">'
                f'<span style="color:{primary}">• {_esc(head)}：</span>'
                f'<span style="color:{dark};font-weight:normal">{_esc(text)}</span></div>')
        else:
            lines.append(
                f'<div class="bullet" data-text="1" style="color:{dark};'
                f'font-size:{_px(size)}px;margin-bottom:{_px(space_after_pt)}px">'
                f'<span style="color:{primary}">• </span>{_esc(text)}</div>')
    return (
        f'<div data-box="{box_name}" style="position:absolute;'
        f'left:{_in(bx)}px;top:{_in(by)}px;width:{_in(bw)}px;height:{_in(bh)}px;'
        f'overflow:hidden;line-height:1.22;{justify}">{"".join(lines)}</div>')


def _box_div(box: tuple, name: str, inner: str, theme: dict,
             bg: str | None = None, rounded: bool = False) -> str:
    bx, by, bw, bh = box
    style = (f'position:absolute;left:{_in(bx)}px;top:{_in(by)}px;'
             f'width:{_in(bw)}px;height:{_in(bh)}px;')
    if bg:
        style += f"background:{_css_color(bg)};"
    if rounded:
        style += "border-radius:8px;"
    return f'<div data-box="{name}" style="{style}">{inner}</div>'


def _cover_html(slide: dict, design: dict, theme: dict) -> str:
    cover_bg = _css_color(theme.get("cover_bg", "#1F3864"))
    cover_text = _css_color(theme.get("cover_text", "#FFFFFF"))
    cover_sub = _css_color(theme.get("cover_sub", "#BDD7EE"))
    accent = _css_color(theme.get("accent", "#ED7D31"))
    secondary = _css_color(theme.get("secondary", "#2E75B6"))
    paper = {**(design.get("paper") or {})}
    zh = ""
    if "：" in str(slide.get("title") or ""):
        zh = str(slide["title"]).split("：", 1)[1]
    sub = str(slide.get("subtitle") or "")
    meta = " · ".join(x for x in (str(paper.get("venue") or ""),
                                  str(paper.get("arxiv") or "")) if x)

    parts = [
        f'<div data-box="cover_bg" style="position:absolute;left:0;top:0;'
        f'width:{SLIDE_W_PX}px;height:{SLIDE_H_PX}px;background:{cover_bg}"></div>',
        f'<div style="position:absolute;left:0;top:{_in(7.28)}px;'
        f'width:{SLIDE_W_PX}px;height:{_in(0.22)}px;background:{accent}"></div>',
        f'<div style="position:absolute;left:{_in(0.9)}px;top:{_in(2.18)}px;'
        f'width:{_in(0.09)}px;height:{_in(1.5)}px;background:{accent}"></div>',
        f'<div data-box="cover_title" style="position:absolute;'
        f'left:{_in(1.05)}px;top:{_in(2.10)}px;width:{_in(11.3)}px;height:{_in(1.25)}px;'
        f'font-size:{_px(25)}px;font-weight:bold;color:{cover_text};'
        f'line-height:1.2;word-wrap:break-word">{_esc(paper.get("title") or "")}</div>']
    if zh:
        parts.append(
            f'<div data-box="cover_sub" style="position:absolute;'
            f'left:{_in(1.05)}px;top:{_in(3.28)}px;width:{_in(11.3)}px;height:{_in(0.55)}px;'
            f'font-size:{_px(17)}px;color:{cover_sub}">{_esc(zh)}</div>')
    if sub:
        parts.append(
            f'<div data-box="cover_subtitle" style="position:absolute;'
            f'left:{_in(1.05)}px;top:{_in(4.05)}px;width:{_in(11.3)}px;height:{_in(0.5)}px;'
            f'font-size:{_px(15)}px;font-weight:bold;color:{accent}">{_esc(sub)}</div>')
    parts.append(
        f'<div data-box="cover_authors" style="position:absolute;'
        f'left:{_in(1.05)}px;top:{_in(4.95)}px;width:{_in(11.3)}px;height:{_in(0.4)}px;'
        f'font-size:{_px(13)}px;color:{cover_text}">{_esc(paper.get("authors") or "")}</div>')
    parts.append(
        f'<div data-box="cover_aff" style="position:absolute;'
        f'left:{_in(1.05)}px;top:{_in(5.32)}px;width:{_in(11.3)}px;height:{_in(0.4)}px;'
        f'font-size:{_px(12)}px;color:{cover_sub}">{_esc(paper.get("affiliation") or "")}</div>')
    if meta:
        parts.append(
            f'<div data-box="cover_meta" style="position:absolute;'
            f'left:{_in(1.05)}px;top:{_in(5.72)}px;width:{_in(11.3)}px;height:{_in(0.4)}px;'
            f'font-size:{_px(12)}px;color:{accent}">{_esc(meta)}</div>')
    for i in range(6):
        parts.append(
            f'<div style="position:absolute;left:{_in(1.05 + i * 0.55)}px;'
            f'top:{_in(6.75)}px;width:{_in(0.34)}px;height:{_in(0.055)}px;'
            f'background:{secondary}"></div>')
    return "".join(parts)


def _content_html(slide: dict, design: dict, theme: dict, index: int,
                  total: int) -> str:
    st = slide.get("slide_type") or "bullets"
    lay = slide.get("layout") or {}
    style = slide.get("style") or {}
    parts = [_header_footer_html(design, slide, index, total, theme)]

    if st == "agenda":
        items = [str(x) for x in (slide.get("items") or []) if str(x).strip()]
        if not items:
            for b in (slide.get("bullets") or []):
                txt = (b.get("text") or b.get("head")) if isinstance(b, dict) else b
                if txt and str(txt).strip():
                    items.append(str(txt))
        n = max(1, len(items))
        start_y = 1.75
        row_h = min(0.86, (6.60 - start_y) / n)
        primary = _css_color(theme.get("primary", "#1F3864"))
        dark = _css_color(theme.get("darktext", "#262626"))
        white = "#FFFFFF"
        for i, item in enumerate(items):
            y = start_y + i * row_h
            parts.append(
                f'<div style="position:absolute;left:{_in(1.0)}px;top:{_in(y)}px;'
                f'width:{_in(0.62)}px;height:{_in(0.62)}px;background:{primary};'
                f'border-radius:50%;text-align:center;line-height:{_in(0.62)}px;'
                f'color:{white};font-size:{_px(15)}px;font-weight:bold">{i + 1:02d}</div>')
            parts.append(
                f'<div data-text="1" style="position:absolute;left:{_in(1.9)}px;'
                f'top:{_in(y + 0.04)}px;width:{_in(10.6)}px;height:{_in(max(0.4, row_h - 0.1))}px;'
                f'font-size:{_px(17)}px;color:{dark};overflow:hidden">{_esc(str(item)[:90])}</div>')
    elif st == "bullets":
        bx, by, bw, bh = style.get("body_box") or lay["body_box"]
        parts.append(_bullets_html(slide, (bx, by, bw, bh),
                                   style.get("bullet_size") or lay.get("bullet_size", 15),
                                   theme))
        if slide.get("takeaway"):
            tx, ty, tw, th = lay["takeaway_box"]
            parts.append(
                f'<div data-box="takeaway" style="position:absolute;'
                f'left:{_in(tx)}px;top:{_in(ty)}px;width:{_in(tw)}px;height:{_in(th)}px;'
                f'background:{_css_color(theme.get("light", "#F2F6FB"))};border-radius:8px">'
                f'<div style="position:absolute;left:{_in(tx + 0.32)}px;top:{_in(ty + 0.14)}px;'
                f'width:{_in(tw - 0.7)}px;height:{_in(th - 0.28)}px;font-size:{_px(13.5)}px;'
                f'font-weight:bold;color:{_css_color(theme.get("primary", "#1F3864"))};'
                f'overflow:hidden">{_esc(slide["takeaway"])}</div></div>')
    elif st == "two_col":
        if "right_box" not in lay:
            bx, by, bw, bh = lay.get("body_box", (0.62, 1.55, 12.1, 4.65))
            parts.append(_bullets_html(slide, (bx, by, bw, bh),
                                       lay.get("bullet_size", 14), theme,
                                       space_after_pt=12.0))
        else:
            lx, ly, lw, lh = lay["left_box"]
            parts.append(_bullets_html(slide, (lx, ly, lw, lh),
                                       lay.get("bullet_size", 14), theme,
                                       space_after_pt=12.0))
            rx, ry, rw, rh = lay["right_box"]
            if slide.get("image_path"):
                ix, iy, iw2, ih2 = _img_fit_size(slide["image_path"], (rx, ry, rw, rh))
                uri = _image_data_uri(slide["image_path"])
                if uri:
                    parts.append(
                        f'<div data-box="right_img" style="position:absolute;'
                        f'left:{ix}px;top:{iy}px;width:{iw2}px;height:{ih2}px;'
                        f'overflow:hidden"><img src="{uri}" style="width:100%;height:100%;'
                        f'object-fit:contain" data-img="1"></div>')
            cap = (slide.get("figure") or {}).get("caption", "")
            if cap:
                cx, cy, cw, ch = lay["caption_box"]
                parts.append(
                    f'<div data-text="1" style="position:absolute;left:{_in(cx)}px;'
                    f'top:{_in(cy)}px;width:{_in(cw)}px;height:{_in(ch)}px;'
                    f'font-size:{_px(10.5)}px;color:{_css_color(theme.get("gray", "#595959"))};'
                    f'text-align:center;overflow:hidden">{_esc(cap)}</div>')
    elif st == "figure":
        if "image_box" not in lay:
            bx, by, bw, bh = lay.get("body_box", (0.62, 1.55, 12.1, 4.65))
            parts.append(_bullets_html(slide, (bx, by, bw, bh),
                                       lay.get("bullet_size", 15), theme))
        else:
            if slide.get("image_path"):
                ix, iy, iw2, ih2 = _img_fit_size(slide["image_path"], lay["image_box"])
                uri = _image_data_uri(slide["image_path"])
                if uri:
                    parts.append(
                        f'<div data-box="figure_img" style="position:absolute;'
                        f'left:{ix}px;top:{iy}px;width:{iw2}px;height:{ih2}px;'
                        f'overflow:hidden"><img src="{uri}" style="width:100%;height:100%;'
                        f'object-fit:contain" data-img="1"></div>')
            cap = (slide.get("figure") or {}).get("caption", "")
            if cap:
                parts.append(
                    f'<div data-text="1" style="position:absolute;'
                    f'left:{_in(lay["caption_box"][0])}px;top:{_in(lay["caption_box"][1])}px;'
                    f'width:{_in(lay["caption_box"][2])}px;height:{_in(lay["caption_box"][3])}px;'
                    f'font-size:{_px(10)}px;color:{_css_color(theme.get("gray", "#595959"))};'
                    f'text-align:center;overflow:hidden">{_esc(cap)}</div>')
            if slide.get("bullets") and "right_box" in lay:
                rx, ry, rw, rh = lay["right_box"]
                parts.append(_bullets_html(slide, (rx, ry, rw, rh),
                                           lay.get("bullet_size", 13), theme,
                                           space_after_pt=9.0))
    elif st == "chart":
        lx, ly, lw, lh = lay["left_box"]
        parts.append(_bullets_html(slide, (lx, ly, lw, lh),
                                   lay.get("bullet_size", 14), theme,
                                   space_after_pt=11.0))
        if slide.get("chart_path"):
            ix, iy, iw2, ih2 = _img_fit_size(slide["chart_path"], lay["chart_box"])
            uri = _image_data_uri(slide["chart_path"])
            if uri:
                parts.append(
                    f'<div data-box="chart_img" style="position:absolute;'
                    f'left:{ix}px;top:{iy}px;width:{iw2}px;height:{ih2}px;'
                    f'overflow:hidden"><img src="{uri}" style="width:100%;height:100%;'
                    f'object-fit:contain" data-img="1"></div>')
        cap = (slide.get("chart") or {}).get("caption", "")
        if cap:
            parts.append(
                f'<div data-text="1" style="position:absolute;'
                f'left:{_in(lay["caption_box"][0])}px;top:{_in(lay["caption_box"][1])}px;'
                f'width:{_in(lay["caption_box"][2])}px;height:{_in(lay["caption_box"][3])}px;'
                f'font-size:{_px(10.5)}px;color:{_css_color(theme.get("gray", "#595959"))};'
                f'text-align:center;overflow:hidden">{_esc(cap)}</div>')
    elif st == "table":
        box = lay["table_box"]
        parts.append(
            f'<div data-box="table_box" style="position:absolute;'
            f'left:{_in(box[0])}px;top:{_in(box[1])}px;width:{_in(box[2])}px;'
            f'height:{_in(box[3])}px;overflow:hidden">'
            f'{_table_html(slide, theme)}</div>')
        tb = slide.get("table") or {}
        if tb.get("caption"):
            parts.append(
                f'<div data-text="1" style="position:absolute;'
                f'left:{_in(lay["table_caption"][0])}px;top:{_in(lay["table_caption"][1])}px;'
                f'width:{_in(lay["table_caption"][2])}px;height:{_in(lay["table_caption"][3])}px;'
                f'font-size:{_px(10)}px;color:{_css_color(theme.get("gray", "#595959"))};'
                f'text-align:center;overflow:hidden">{_esc(tb["caption"])}</div>')
        if slide.get("bullets") and "right_box" in lay:
            rx, ry, rw, rh = lay["right_box"]
            parts.append(_bullets_html(slide, (rx, ry, rw, rh),
                                       lay.get("bullet_size", 13.5), theme,
                                       space_after_pt=9.0))
        elif slide.get("bullets") and "note_box" in lay:
            nx, ny, nw, nh = lay["note_box"]
            parts.append(_bullets_html(slide, (nx, ny, nw, nh),
                                       lay.get("bullet_size", 11), theme,
                                       space_after_pt=4.0))
    elif st == "keycards":
        cards = [c for c in (slide.get("keycards") or []) if isinstance(c, dict)]
        specs = _card_specs(len(cards), lay)
        light = _css_color(theme.get("light", "#F2F6FB"))
        primary = _css_color(theme.get("primary", "#1F3864"))
        gray = _css_color(theme.get("gray", "#595959"))
        accent = _css_color(theme.get("accent", "#ED7D31"))
        for card, spec in zip(cards, specs):
            cx, cy, cw, ch = spec
            parts.append(
                f'<div data-box="card" style="position:absolute;left:{_in(cx)}px;'
                f'top:{_in(cy)}px;width:{_in(cw)}px;height:{_in(ch)}px;'
                f'background:{light};border-radius:8px">'
                f'<div style="position:absolute;left:{_in(cx + 0.25)}px;'
                f'top:{_in(cy + 0.28)}px;width:{_in(cw - 0.5)}px;height:{_in(0.07)}px;'
                f'background:{accent}"></div>'
                f'<div data-text="1" style="position:absolute;left:{_in(cx + 0.3)}px;'
                f'top:{_in(cy + 0.5)}px;width:{_in(cw - 0.6)}px;height:{_in(0.85)}px;'
                f'font-size:{_px(lay.get("card_size", 16))}px;font-weight:bold;'
                f'color:{primary};overflow:hidden">{_esc(card.get("head", ""))}</div>'
                f'<div data-text="1" style="position:absolute;left:{_in(cx + 0.3)}px;'
                f'top:{_in(cy + 1.4)}px;width:{_in(cw - 0.6)}px;height:{_in(ch - 1.6)}px;'
                f'font-size:{_px(12.5)}px;color:{gray};overflow:hidden">{_esc(card.get("text", ""))}</div>'
                f'</div>')
    elif st == "formula":
        fx, fy, fw, fh = lay["formula_box"]
        secondary = _css_color(theme.get("secondary", "#2E75B6"))
        light = _css_color(theme.get("light", "#F2F6FB"))
        parts.append(
            f'<div data-box="formula_box" style="position:absolute;left:{_in(fx)}px;'
            f'top:{_in(fy)}px;width:{_in(fw)}px;height:{_in(fh)}px;'
            f'background:{light};border-radius:8px;display:flex;align-items:center;'
            f'justify-content:center">'
            f'<div data-text="1" style="font-size:{_px(30)}px;font-weight:bold;'
            f'color:{secondary};text-align:center;max-width:{_in(fw - 0.6)}px;'
            f'overflow:hidden;word-wrap:break-word">{_esc(slide.get("formula") or "")}</div></div>')
        bx, by, bw, bh = lay["body_box"]
        parts.append(_bullets_html(slide, (bx, by, bw, bh),
                                   lay.get("bullet_size", 14), theme))
    return "".join(parts)


def _slide_html(slide: dict, design: dict, theme: dict, index: int,
                total: int) -> str:
    st = slide.get("slide_type") or "bullets"
    if st == "cover":
        body = _cover_html(slide, design, theme)
    else:
        body = _content_html(slide, design, theme, index, total)
    return (
        f'<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        f'<style>'
        f'*{{box-sizing:border-box;margin:0;padding:0}}'
        f'html,body{{width:{SLIDE_W_PX}px;height:{SLIDE_H_PX}px;overflow:hidden;'
        f'background:{_css_color(theme.get("bg", "#FFFFFF"))}}}'
        f'body{{font-family:{_FONT_STACK};}}'
        f'.bullet{{line-height:1.22}}'
        f'table{{table-layout:fixed}}'
        f'</style></head>'
        f'<body><div class="slide" data-slide="{_esc(slide.get("slide_id", ""))}" '
        f'data-type="{_esc(st)}" style="position:relative;width:{SLIDE_W_PX}px;'
        f'height:{SLIDE_H_PX}px;overflow:hidden">{body}</div></body></html>')


def build_html(design: dict, out_dir: Path = WORKSPACE / "html_preview") -> list[Path]:
    """design.json -> 每页 HTML 审查稿。返回 HTML 文件列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    theme = design.get("theme") or {}
    slides = design.get("slides") or []
    total = len(slides)
    files = []
    for index, s in enumerate(slides, 1):
        html = _slide_html(s, design, theme, index, total)
        path = out_dir / f"slide_{index:02d}.html"
        path.write_text(html, encoding="utf-8")
        files.append(path)
    return files


def render_pngs(html_files: list[Path],
                out_dir: Path = WORKSPACE / "html_preview",
                channel: str = "msedge") -> list[Path]:
    """用 Playwright + 系统 Edge 把每页 HTML 渲染为 1280x720 PNG。"""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    pngs: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=True)
        page = browser.new_page(viewport={"width": SLIDE_W_PX, "height": SLIDE_H_PX})
        try:
            for html_path in html_files:
                page.goto(html_path.resolve().as_uri(), wait_until="load")
                page.wait_for_timeout(120)
                out = out_dir / f"{html_path.stem}.png"
                page.screenshot(path=str(out))
                pngs.append(out)
        finally:
            browser.close()
    return pngs


def build_and_render(design_path: Path = DESIGN_JSON,
                     out_dir: Path = WORKSPACE / "html_preview",
                     channel: str = "msedge") -> tuple[list[Path], list[Path]]:
    """一键：design.json -> HTML -> PNG。返回 (html_files, pngs)。"""
    design = load_json(design_path)
    files = build_html(design, out_dir)
    pngs = render_pngs(files, out_dir, channel)
    print(f"[html_preview] {len(files)} 页 HTML + {len(pngs)} 页截图 -> {out_dir}")
    return files, pngs


if __name__ == "__main__":
    import sys
    design_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DESIGN_JSON
    files, pngs = build_and_render(design_path)
    for f in files:
        print(f)
