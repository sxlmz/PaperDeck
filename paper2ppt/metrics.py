# -*- coding: utf-8 -*-
"""文本度量与版式计算（无第三方依赖的叶子模块）。

renderer.py 与 critic.py 必须共用同一套口径：
  * renderer 用它决定字号与列宽；
  * critic 用它反向检查「文字溢出 / 表格列过窄」。
两边各写一份的话，检查结果会和渲染结果对不上——此前几何检查恒为 0，
一部分原因就是 critic 自己那套估算忽略了 space_after。

本模块刻意不导入 pptx / matplotlib / pymupdf，可被任意模块安全引用
（critic → renderer 会顺着 designer → charts → matplotlib 把重依赖全拖进来）。

中英混排宽度约定（全仓库统一）：
    CJK 字符 ≈ 1.0 字宽，拉丁字符 ≈ 0.55 字宽。

注：CJK 判定用码点区间而非正则字符类，避免源码里的宽字符被编辑器/终端
转码破坏（此前踩过：字符类被写成字面量与转义混排，语义虽等价但不可读）。
"""
from __future__ import annotations

import math

# CJK 判定区间：部首扩展+汉字、兼容汉字、全角字符、CJK 标点
_CJK_RANGES = (
    (0x2E80, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
    (0x3000, 0x303F),
)

# 断行机会：空白、连字符、斜杠、逗号之后
_BREAK_AFTER = " \t　-/,;，·—–"

# 单元格左右内边距合计（pt），与 renderer.add_table 的 Pt(6)×2 对齐
CELL_MARGIN_PT = 12.0

# 文本框左右内边距合计（pt），与 renderer.add_textbox 的 Pt(4)×2 对齐
BOX_MARGIN_PT = 8.0

LATIN_RATIO = 0.55


def is_cjk(ch: str) -> bool:
    """单个字符是否按 CJK 全宽计算（可逐字断行）。"""
    o = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= o <= hi:
            return True
    return False


def text_units(s: str) -> float:
    """字符串的显示宽度（以「一个 CJK 字」为 1.0 单位）。"""
    s = s or ""
    cjk = sum(1 for ch in s if is_cjk(ch))
    return cjk * 1.0 + (len(s) - cjk) * LATIN_RATIO


def est_lines(text: str, size_pt: float, width_pt: float) -> int:
    """估算文本在给定字号/可用宽度下占几行（按换行符分段各自计算）。"""
    if not text:
        return 1
    per_line = max(1.0, width_pt / max(6.0, size_pt))
    total = 0
    for seg in str(text).split("\n"):
        if not seg:
            total += 1
            continue
        total += max(1, math.ceil(text_units(seg) / per_line))
    return max(1, total)


def max_token_units(text: str) -> float:
    """最长「不可断片段」的宽度。

    这是防 `FEDformer` → `FEDf orme r`、`0.176/0.237` → `0.176 7` 的关键度量：
    这类折断不是「行数算少了」，而是单个片段本身就比列宽还宽，缩字号救不了，
    必须给该列足够的宽度。行数估算（est_lines）看不到这个问题。
    """
    text = str(text or "")
    if not text:
        return 0.0
    longest = 0.0
    run = 0.0
    for ch in text:
        if is_cjk(ch):
            # CJK 逐字可断：先结算当前 run，再让该字单独成段
            longest = max(longest, run, 1.0)
            run = 0.0
            continue
        run += LATIN_RATIO
        if ch in _BREAK_AFTER:
            longest = max(longest, run)
            run = 0.0
    return max(longest, run)


def text_fits(texts, box_w_in: float, box_h_in: float, size_pt: float,
              line_spacing: float = 1.22, space_after_pt: float = 0.0) -> bool:
    """在**指定字号**下，这些文本能否装进版位。

    与 fit_font_size 互补：那个是"要多小才装得下"，这个是"给这个字号装得下吗"。
    编辑工具用它做前置校验——改完坐标若连最小字号都装不下，就该拒绝这次修改，
    而不是渲染出来再被 Critic 抓一遍。
    """
    width_pt = max(12.0, box_w_in * 72.0 - BOX_MARGIN_PT)
    height_pt = max(12.0, box_h_in * 72.0 - 4.0
                    - space_after_pt * max(0, len(texts) - 1))
    lines = sum(est_lines(t, size_pt, width_pt) for t in texts)
    return lines * size_pt * line_spacing <= height_pt


def fit_font_size(texts, box_w_in: float, box_h_in: float, start_size: float,
                  min_size: float = 9.0, line_spacing: float = 1.22,
                  space_after_pt: float = 0.0) -> float:
    """在给定版位内收缩字号直至可容纳；返回字号（不低于 min_size）。

    texts: 待排文本列表（每个元素是一个段落/bullet）。
    space_after_pt: 段后间距。此前被忽略，导致「估算装得下、渲染却溢出」。
    """
    width_pt = max(12.0, box_w_in * 72.0 - BOX_MARGIN_PT)
    height_pt = max(12.0, box_h_in * 72.0 - 4.0
                    - space_after_pt * max(0, len(texts) - 1))
    size = float(start_size)
    while size > min_size:
        lines = sum(est_lines(t, size, width_pt) for t in texts)
        if lines * size * line_spacing <= height_pt:
            return round(size, 1)
        size -= 0.5
    return float(min_size)


def plan_columns(header, rows, box_w_in: float, box_h_in: float,
                 font_size: float, min_font: float = 8.0,
                 caption_pt: float = 0.0):
    """按内容计算表格列宽，返回 (列宽fractions, 采用的字号, 行高英寸)。

    算法（顺序很重要）：
      1. 列数取表头与所有数据行的最大值，短行补空（顺带修掉行比表头长时的越界）；
      2. 每列先算「最长不可断片段」所需的最小宽度 min_pt；
      3. 若所有列的最小宽度之和超出可用宽度，按 0.5pt 降字号重算，直到装得下或触底；
      4. 剩余宽度按各列内容宽度加权分配，每列不低于其 min_pt；
      5. 归一化为 fractions，直接用于 python-pptx 的列宽设置。
    """
    header = [str(c or "") for c in (header or [])]
    rows = [[str(c or "") for c in (r or [])] for r in (rows or [])]
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])
    header = header + [""] * (n_cols - len(header))
    rows = [r + [""] * (n_cols - len(r)) for r in rows]

    avail_pt = max(24.0, box_w_in * 72.0 - 6.0)

    cols = []
    for ci in range(n_cols):
        cells = [header[ci]] + [r[ci] for r in rows]
        cols.append({
            "tok": max(max_token_units(c) for c in cells),
            "full": max(text_units(c) for c in cells),
        })

    size = float(font_size)
    while True:
        min_pt = [c["tok"] * size * 1.02 + CELL_MARGIN_PT for c in cols]
        if sum(min_pt) <= avail_pt or size <= min_font:
            break
        size = max(min_font, size - 0.5)

    min_pt = [c["tok"] * size * 1.02 + CELL_MARGIN_PT for c in cols]
    want_pt = [max(c["full"] * size * 0.55 + CELL_MARGIN_PT, min_pt[i])
               for i, c in enumerate(cols)]

    total_min = sum(min_pt)
    if total_min <= avail_pt:
        leftover = avail_pt - total_min
        slack = [want_pt[i] - min_pt[i] for i in range(n_cols)]
        slack_total = sum(slack)
        if slack_total > 0:
            widths = [min_pt[i] + leftover * slack[i] / slack_total
                      for i in range(n_cols)]
        else:
            widths = [min_pt[i] + leftover / n_cols for i in range(n_cols)]
    else:
        # 极小版位：按最小宽度等比压缩（字号已触底，只能接受）
        widths = [m * avail_pt / total_min for m in min_pt]

    tot = sum(widths) or 1.0
    fractions = [w / tot for w in widths]

    n_rows_total = len(rows) + 1
    usable_h = max(0.6, box_h_in - caption_pt / 72.0)
    # 行数少时把行高放大到 0.58"，避免表格只占版面上半屏、下半屏大片留白
    row_h = min(0.58, max(0.30, usable_h / max(1, n_rows_total)))
    return fractions, round(size, 1), round(row_h, 3)
