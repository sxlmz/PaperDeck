# -*- coding: utf-8 -*-
"""md_engine.py：Markdown 内容协议 -> 组件蓝图 -> 完整 HTML。

设计目标（用户拍板的最终形态）：
  - LLM 只输出**纯内容 Markdown**（标题/要点/图片/表格/引用），不写任何
    HTML/CSS 结构；
  - 布局由 LLM 在 front-matter 里**显式判断**（layout/columns），引擎按
    蓝图组装；布局决策是"选参数"而不是"写代码"；
  - 微调能力：LLM 可声明组件级参数（图片大小/位置、文字大小/颜色）以及
    一段**白名单过滤后的 CSS 覆盖**——只允许改颜色/字号/尺寸/间距等
    外观属性，禁止 position/display/grid/flex 等布局属性（防止 LLM
    重头写布局，只允许"哪里不满调哪里"）。

Markdown 协议示例：
    ---
    layout: row
    columns: 1.3fr,0.8fr
    img_width: 60%
    title_size: 32px
    title_color: #38BDF8
    css: |
      .point .txt { font-size: 20px; }
      .callout { background: rgba(56,189,248,.10); }
    ---

    ## 图 1：PatchTST 整体架构

    ![图1：(a) 多通道共享主干](assets/fig01_p4.png)

    - 多变量序列按通道拆分，共享同一主干（图1a）
    - 每通道经 Instance Norm + Patching 生成 token（图1b）

    > 通道独立 + Patching 是长程预测最关键的归纳偏置

    | Dataset | T | MSE |
    |---|---|---|
    | Weather | 96 | 0.149 |

块类型映射：
    ##        -> 页标题（head.title）
    ![alt]()  -> figure 组件（alt 作为图注，src 为图片路径）
    - item    -> bullets 组件（每项 text + source，source 从“（xx）”提取）
    > quote   -> callout 组件（title/text 合并为一句）
    | table   -> table 组件（首行 thead，第二行分隔符跳过）
    段落      -> text 组件
"""
from __future__ import annotations

import re
from pathlib import Path

from component_engine import render_blueprint

#: CSS 覆盖白名单：只允许外观属性，禁止布局与字号属性。
#: 字号（font-size/line-height）一律由模板 class 统一控制——LLM 内容不应改字号。
_ALLOWED_CSS_PROPS = {
    "color", "font-weight", "font-style",
    "letter-spacing", "text-align", "text-shadow",
    "width", "height", "max-width", "max-height", "min-width", "min-height",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-radius", "background", "background-color", "background-image",
    "box-shadow", "opacity", "object-fit", "object-position",
}
_BLOCKED_CSS_PROPS = {
    "position", "display", "grid-template-columns", "grid-template-rows",
    "grid-auto-flow", "grid-area", "grid-column", "grid-row",
    "flex", "flex-direction", "justify-content", "align-items",
    "left", "top", "right", "bottom", "z-index", "transform",
    "overflow", "float", "visibility",
    # 字号纪律：LLM 不得通过 css: 覆盖字号/行高（模板统一控制）
    "font-size", "line-height",
}
_SELECTOR_OK = re.compile(r"^[\w\s#.\[\]'\"=:>,-]+$")  # 允许逗号分隔的多选择器


def _parse_frontmatter(md: str) -> tuple[dict, str]:
    """解析开头的 --- ... --- 块。返回 (fm, 剩余正文)。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", md, re.DOTALL)
    if not m:
        return {}, md
    fm: dict = {}
    body = md[m.end():]
    css_lines: list[str] = []
    in_css = False
    for line in m.group(1).splitlines():
        if line.strip() == "css: |" or line.strip() == "css:":
            in_css = True
            continue
        if in_css:
            if line.startswith("  ") or line.strip() == "":
                css_lines.append(line[2:] if line.startswith("  ") else line)
                continue
            in_css = False
        if ":" in line and not in_css:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    if css_lines:
        fm["css"] = "\n".join(css_lines)
    return fm, body


def _split_blocks(md: str) -> list[dict]:
    """把 Markdown 正文切成块列表（保持顺序）。"""
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        # 标题
        m = re.match(r"^#{2,3}\s+(.*)$", line)
        if m:
            level = len(line) - len(line.lstrip("#"))
            blocks.append({"kind": "title", "level": level, "text": m.group(1).strip()})
            i += 1
            continue
        # 图片
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if m:
            blocks.append({"kind": "figure", "caption": m.group(1).strip(),
                           "src": m.group(2).strip()})
            i += 1
            continue
        # 引用（连续 > 合并为一个 callout）
        if line.startswith(">"):
            quotes = []
            while i < n and lines[i].lstrip().startswith(">"):
                quotes.append(lines[i].lstrip()[1:].strip())
                i += 1
            blocks.append({"kind": "callout", "lines": quotes})
            continue
        # 无序列表（连续 - 合并）
        if re.match(r"^\s*-\s+", line):
            items = []
            while i < n and re.match(r"^\s*-\s+", lines[i]):
                items.append(re.sub(r"^\s*-\s+", "", lines[i].rstrip()))
                i += 1
            blocks.append({"kind": "bullets", "items": items})
            continue
        # 表格（连续 | 行）
        if line.lstrip().startswith("|"):
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            blocks.append({"kind": "table", "rows": rows})
            continue
        # 普通段落（合并连续非空行）
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not lines[i].lstrip().startswith(("|", "-", ">", "#", "!")):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"kind": "text", "text": " ".join(para)})
    return blocks


def _parse_table(rows: list[str]) -> dict:
    """Markdown 表格 -> {thead:[...], items:[{cells:...}]}。"""
    def split(row: str) -> list[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    thead = []
    items = []
    for idx, row in enumerate(rows):
        cells = split(row)
        if idx == 0:
            thead = cells
            continue
        # 分隔行 |---|---| 跳过
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        td = "".join(
            f"<td>{_escape(c)}</td>" for c in cells)
        items.append({"cells": td})
    return {"thead": "".join(f"<th>{_escape(c)}</th>" for c in thead),
            "items": items}


def _parse_table_rows(rows: list[str]) -> dict:
    """Markdown 表格 -> {header: [...], rows: [[...]]}（供卡片表格用）。"""
    def split(row: str) -> list[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    header: list[str] = []
    body: list[list[str]] = []
    for idx, row in enumerate(rows):
        cells = split(row)
        if idx == 0:
            header = cells
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        body.append(cells)
    return {"header": header, "rows": body}


def _match_fig_ref(text: str):
    """从文本中提取图号引用（图 N / Figure N / Fig. N），无则返回 None。"""
    m = re.search(r"(?:图|Figure|Fig\.?)\s*(\d{1,2})", text, re.IGNORECASE)
    return m.group(1) if m else None


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


#: markdown 强调标记：**粗体** / __粗体__ / *斜体* / `代码`
_MD_EM = re.compile(
    r"\*\*(.+?)\*\*|__(.+?)__|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|`([^`]+)`")


def _strip_em(s: str) -> str:
    """去掉 markdown 强调标记，只留文字。

    这是**所有 bullet 文本的唯一入口**（卡片头、结论要点、大表页都从
    _parse_bullet 取文本），所以在这里收口。收口前的现象：LLM 写
    `**切分方式**：…`，`**` 一路进模板占位符，最终在 PPT 里显示成字面量。
    让每个 renderer 各自处理，迟早会漏一个。
    """
    s = str(s or "")
    prev = None
    while prev != s:                 # 处理多重强调
        prev = s
        s = _MD_EM.sub(lambda m: next((g for g in m.groups() if g), ""), s)
    return s


#: 图注/表注句式（templates_1/README.md：底部文字卡只允许总结/解读，
#  不允许写图注/表注）——收集说明文字时确定性过滤，不依赖 LLM 自觉。
_CAPTION_LIKE_RE = re.compile(
    r"^(?:图|表)\s*\d+\s*[：:]|^(?:Figure|Table)\s*\d+[.:]", re.I)


def _is_caption_like(s: str) -> bool:
    """判断一段文字是否"图注/表注"（如「表 1：数据集统计」「图 2：MSE 对比」）。

    先剥 markdown 强调（LLM 常写 **表 7**：…），仅命中行首的标注式图注/表注；
    "从表 1 可看出…"这类解读不命中（保留）。
    """
    return bool(_CAPTION_LIKE_RE.match(_strip_em(str(s or "")).strip()))


def _parse_bullet(text: str) -> dict:
    """要点文本 -> {text, source}。

    source 支持两种来源标注（渲染到 .src 小字，不进正文）：
      - 页码标注：〔p4〕/〔P.4〕/〔论文第4页〕（author 卡片里用 〔px〕 标记）
      - 图引用：  （图1a）/（Figure 2）等圆括号末尾内容
    """
    text = _strip_em(text)
    src = ""
    m = re.search(r"[〔\[【]\s*(?:论文第\s*)?[Pp]\.?\s*(\d{1,2})\s*页?\s*[〕\]】]\s*$", text, re.IGNORECASE)
    if m:
        src = f"〔p{m.group(1)}〕"
        text = text[:m.start()].strip()
    else:
        m2 = re.search(r"[（(]([^（）()]{1,40})[）)]\s*$", text)
        if m2:
            src = f"（{m2.group(1).strip()}）"
            text = text[:m2.start()].strip()
    return {"text": text, "source": src}


def _filter_css(css: str) -> tuple[str, list[str]]:
    """白名单过滤 CSS 覆盖段。返回 (过滤后 CSS, 被拦掉的规则描述)。"""
    if not css or not css.strip():
        return "", []
    rules: list[str] = []
    blocked: list[str] = []
    # 简单按 {} 拆分规则
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = m.group(1).strip()
        body = m.group(2)
        if not _SELECTOR_OK.match(selector):
            blocked.append(f"{selector} 选择器非法")
            continue
        kept: list[str] = []
        for decl in body.split(";"):
            decl = decl.strip()
            if not decl or ":" not in decl:
                continue
            prop = decl.split(":", 1)[0].strip().lower()
            if prop in _ALLOWED_CSS_PROPS:
                kept.append(decl)
            elif prop in _BLOCKED_CSS_PROPS:
                blocked.append(f"{selector} {prop}（布局属性禁止）")
            else:
                blocked.append(f"{selector} {prop}（不在白名单）")
        if kept:
            rules.append(f"{selector} {{ {'; '.join(kept)}; }}")
    return "\n".join(rules), blocked


def _fig_key(f: dict | None) -> str:
    """fig_no 归一化为 'figNN'（兼容 int 1 / '1' / 'Figure 1' / 'fig01'）。"""
    if not f:
        return ""
    v = str(f.get("fig_no") or "").strip().lower()
    digits = re.sub(r"\D", "", v)
    return "fig" + digits.zfill(2) if digits else v


def _resolve_asset(src: str, figures: list | None = None) -> str:
    """把图片 src 解析为 assets/<真实文件名>。

    三级解析（LLM 常写简写/编号，真实资产是 figNN_pXX.png）：
      1. basename 精确匹配 figures 里的真实文件名；
      2. 按 "fig<N>" 编号匹配（fig1 / Figure 1 / fig_1 -> fig01_p4.png）；
      3. 都匹配不到原样归一化返回（渲染时缺图会被 VLM/几何检查抓到）。
    sandbox 会拦截盘符路径，所以统一转 assets/ 相对路径。
    """
    src = str(src or "").replace("\\", "/")
    base = Path(src).name
    figures = figures or []
    # 1) 精确匹配真实文件名
    for f in figures:
        if base and base == Path(str(f.get("path") or "")).name:
            return "assets/" + base
    # 2) 按图编号匹配
    m = re.search(r"(?:fig(?:ure)?[\s_\-]*)?(\d+)", base, re.IGNORECASE)
    if m:
        want = str(int(m.group(1)))
        for f in figures:
            if str(f.get("fig_no")) == want or str(f.get("id", "")).endswith(want):
                return "assets/" + Path(str(f.get("path") or "")).name
    # 3) 原样归一化
    if re.match(r"^[A-Za-z]:/", src) or "assets/" in src:
        src = "assets/" + base
    return src


#: 图片宽度准入阈值（用户约束 2026-10-03）：宽度超过该限制的图片
#: 不要放进模板——超宽/超重图塞进图表槽会被缩到不可读（如 fig06 1370px 宽
#: 竖图、fig07 1166px）。阈值取 1100px（图区宽 1120 减少量余量）。
_IMG_WIDTH_LIMIT = 1100


def _asset_too_wide(src: str, figures: list | None = None) -> bool:
    """读图片真实像素宽，> _IMG_WIDTH_LIMIT 判定为超宽（不放图）。

    在可能的位置找资产文件：workspace/assets、项目根 assets、html_source/assets。
    """
    from PIL import Image
    resolved = _resolve_asset(src, figures)
    base = Path(str(resolved).replace("assets/", "")).name
    if not base:
        return False
    candidates = [
        Path(__file__).resolve().parent / ".." / "assets" / base,   # workspace/assets
        Path(__file__).resolve().parent / "assets" / base,
        Path("assets") / base,
    ]
    for cand in candidates:
        if cand.exists():
            try:
                with Image.open(cand) as im:
                    return im.width > _IMG_WIDTH_LIMIT
            except Exception:  # noqa: BLE001
                return False
    return False


def _norm_asset(src: str) -> str:
    """把图片 src 归一化为 assets/<文件名>（sandbox 拦截盘符路径）。"""
    src = str(src or "").replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", src) or "assets/" in src:
        src = "assets/" + Path(src).name
    return src

    """把图片 src 归一化为 assets/<文件名>（sandbox 拦截盘符路径）。"""
    src = str(src or "").replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", src) or "assets/" in src:
        src = "assets/" + Path(src).name
    return src


def render_markdown(md: str, theme: str = "theme-light",
                    source: str = "", page: str = "",
                    figures: list | None = None) -> dict:
    """Markdown 内容协议 -> 完整 HTML 页。

    返回 {"html": str, "layout": str, "blocks": [...], "css_blocked": [...],
          "fm": {...}}
    """
    fm, body = _parse_frontmatter(md)
    blocks = _split_blocks(body)

    # 组装蓝图 children
    children: list[dict] = []
    title = ""
    kicker = fm.get("kicker", "")
    for b in blocks:
        if b["kind"] == "title":
            if b["level"] == 2 and not title:
                title = b["text"]
            elif b["level"] == 3 and not kicker:
                kicker = b["text"]
            continue
        if b["kind"] == "figure":
            children.append({"component": "figure",
                             "src": b["src"], "caption": b["caption"]})
        elif b["kind"] == "bullets":
            children.append({"component": "bullets",
                             "items": [_parse_bullet(t) for t in b["items"]]})
        elif b["kind"] == "callout":
            text = " ".join(b["lines"])
            children.append({"component": "callout",
                             "title": fm.get("callout_title", "要点"),
                             "text": text})
        elif b["kind"] == "table":
            t = _parse_table(b["rows"])
            children.append({"component": "table", **t})
        elif b["kind"] == "text":
            children.append({"component": "text", "text": b["text"]})

    # 布局：front-matter 显式指定（row/col/grid），默认 col
    layout = fm.get("layout", "col")
    columns = None
    if layout == "row" and fm.get("columns"):
        columns = [c.strip() for c in fm["columns"].split(",") if c.strip()]

    # 风格：front-matter 可选 theme（LLM 从 3 套主题里挑，未声明用调用方默认）
    theme_choice = str(fm.get("theme") or "").strip()
    if theme_choice in ("theme-light", "theme-dark", "theme-paper"):
        theme = theme_choice

    # 固定页型：toc / chapter / 图表类 / 纯文本大纲类（新模板集，见 README）
    # 布局完全预定义（page_engine 模板），LLM 只填内容变量；
    # 表格与图片模板严格分开，content 兜底按内容确定性路由。
    figures = figures or []
    from page_engine import (render_chapter, render_content,
                             render_flow_1x4,
                             render_chart_notes_v, render_chart_notes_h,
                             render_chart_notes_one,
                             render_table_one, render_table_notes_v,
                             render_table_notes_h,
                             render_dual_table_summary, render_dual_table_notes_v,
                             render_dual_table_notes_h,
                             render_dual_chart_v, render_dual_chart_h,
                             render_dual_chart_summary, render_conclusion_grid,
                             render_left_right_chart, render_left_right_table)
    from page_engine import _table_parts

    # 方法块强制配框架图（用户约束 2026-10-03：讲方法时若有框架图
    # 必须结合框架图讲，不允许出现表格）。fig01=PatchTST 架构图。
    _kb_m = str(kicker or "").strip()
    if _kb_m in ("方法", "METHOD") and fm.get("page_type") not in (
            "chart_notes_v", "chart_notes_h", "chart_notes_one",
            "dual_chart_v", "dual_chart_h", "dual_chart_summary"):
        _f1 = next((f for f in figures
                    if _fig_key(f) == "fig01"
                    and not _asset_too_wide(str(f.get("path") or ""), figures)), None)
        if _f1:
            _cap = str(_f1.get("caption") or "")[:120]
            _p1 = ""
            for _b in blocks:
                if _b["kind"] == "bullets" and _b.get("items"):
                    _p1 = _parse_bullet(_b["items"][0])["text"]
                    break
                if _b["kind"] == "text":
                    _p1 = str(_b.get("text") or "")
                    break
            html = render_chart_notes_one(
                kicker=kicker, title=title,
                subtitle=str(fm.get("subtitle") or ""),
                chart_title=_cap,
                chart=('<img src="' + _resolve_asset(str(_f1.get("path") or ""), figures)
                       + '" alt="" style="max-width:100%;max-height:100%;'
                       'object-fit:contain;">'),
                note=_p1, caption=_cap)
            return {"html": html, "layout": "chart_notes_one",
                    "blocks": blocks, "css_blocked": [], "fm": fm,
                    "columns": columns}

    if fm.get("page_type") == "toc" or (layout == "toc" and not children):
        # 新模板集没有 toc 模板：toc 大纲页按 readme 规则渲染
        # （≤4 点 flow_1x4 / >4 点 conclusion_grid；纯文字，禁图表）
        _toc_steps = [{"head": "", "body": "目录占位"}]
        html = render_flow_1x4(kicker="CONTENTS", title=title or "汇报大纲",
                               steps=_toc_steps,
                               source=source or fm.get("source", ""),
                               page=page or fm.get("page", ""))
        return {"html": html, "layout": "toc", "blocks": blocks,
                "css_blocked": [], "fm": fm, "columns": columns}
    if fm.get("page_type") == "chapter" or (layout == "chapter" and not children):
        html = render_chapter(
            str(fm.get("chapter_no", "01")),
            title or "", fm.get("desc", ""),
            source=source or fm.get("source", ""),
            page=page or fm.get("page", ""))
        return {"html": html, "layout": "chapter", "blocks": blocks,
                "css_blocked": [], "fm": fm, "columns": columns}

    lead = str(fm.get("lead") or "").strip()
    if _is_caption_like(lead):   # lead 是总结句，不允许出现图注/表注
        lead = ""

    # ---- 图表类固定页型（新模板集 2026-10-03：表格与图片模板严格分开）----
    # 强制规则（templates_1/README.md）：
    #   出现数据表格 -> 只能选表格类模板（table_one / table_notes_v/h /
    #     dual_table_summary / dual_table_notes_v/h）；
    #   出现图表/图片 -> 只能选图表类模板（chart_notes_one/v/h /
    #     dual_chart_summary/v/h）；
    #   严禁在图表模板里塞表格、在表格模板里塞图表。
    # 说明条数上限：one/summary 版 ≤1，v/h 版 ≤2（确定性截断）。
    # 图片宽度准入：超宽图（>1100px）不进图表槽（会缩到不可读）。
    pt = str(fm.get("page_type") or "").strip()
    _CHART_ONLY = {"chart_notes_v", "chart_notes_h", "chart_notes_one",
                   "dual_chart_v", "dual_chart_h", "dual_chart_summary"}
    _TABLE_ONLY = {"table_one", "table_notes_v", "table_notes_h",
                   "dual_table_summary", "dual_table_notes_v",
                   "dual_table_notes_h"}
    if pt in _CHART_ONLY or pt in _TABLE_ONLY:
        figs = [b for b in blocks if b["kind"] == "figure"]
        tbls = [b for b in blocks if b["kind"] == "table"]
        figs = [b for b in figs
                if not _asset_too_wide(str(b.get("src") or ""), figures)]
        limit = 2 if (pt.endswith("_v") or pt.endswith("_h")) else 1
        notes: list[str] = []
        for b in blocks:
            if b["kind"] == "callout":
                _joined = " ".join(b["lines"]).strip()
                if _joined and not _is_caption_like(_joined):
                    notes.append(_joined)
            elif b["kind"] == "bullets":
                for t in b["items"]:
                    _btxt = _parse_bullet(t)["text"].strip()
                    if _btxt and not _is_caption_like(_btxt):
                        notes.append(_btxt)
                    if len(notes) >= limit:
                        break
            elif b["kind"] == "text":
                # text 块的换行已被解析器吃成空格：按「句末标点 + 空白」
                # 切分，保证说明条数上限生效（LLM 常把多条要点写成一整段）
                for _seg in re.split(r"(?<=[。！？])\s+",
                                      str(b.get("text") or "")):
                    _seg = _seg.strip()
                    if _seg and not _is_caption_like(_seg):
                        notes.append(_seg)
                        if len(notes) >= limit:
                            break
            if len(notes) >= limit:
                break
        # ---- 单图类（chart_notes_one/v/h）：图表槽优先页内图，其次表格 ----
        if pt in ("chart_notes_v", "chart_notes_h", "chart_notes_one"):
            if figs:
                chart_html = ('<img src="' + _resolve_asset(figs[0]["src"], figures)
                              + '" alt="" style="max-width:100%;max-height:100%;'
                              'object-fit:contain;">')
                chart_title = str(figs[0].get("caption") or "")
            elif tbls:
                tt = _parse_table_rows(tbls[0]["rows"])
                thead, tbody, _ = _table_parts(tt)
                chart_html = ("<table><thead><tr>" + thead
                              + "</tr></thead><tbody>" + tbody + "</tbody></table>")
                chart_title = str(fm.get("table_caption") or "")
            else:
                chart_html = '<div style="color:#94A3B8;">（无图表源，请补图/表）</div>'
                chart_title = ""
            note_1 = notes[0] if len(notes) > 0 else ""
            note_2 = notes[1] if len(notes) > 1 else ""
            if pt == "chart_notes_one":
                html = render_chart_notes_one(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart_title=chart_title, chart=chart_html,
                    note=note_1 or note_2, caption=chart_title)
            elif pt == "chart_notes_v":
                html = render_chart_notes_v(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart_title=chart_title, chart=chart_html,
                    note_1=note_1, note_2=note_2, caption=chart_title)
            else:
                html = render_chart_notes_h(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart_title=chart_title, chart=chart_html,
                    note_1=note_1, note_2=note_2, caption=chart_title)
            return {"html": html, "layout": pt, "blocks": blocks,
                    "css_blocked": [], "fm": fm, "columns": columns}
        # ---- 单表类（table_one / table_notes_v/h）：必须真有表格 ----
        if pt in ("table_one", "table_notes_v", "table_notes_h"):
            if tbls:
                tt = _parse_table_rows(tbls[0]["rows"])
                note_1 = notes[0] if len(notes) > 0 else ""
                note_2 = notes[1] if len(notes) > 1 else ""
                _tcap = (str(fm.get("table_caption") or "")
                         or str(tbls[0].get("caption") or ""))
                if pt == "table_one":
                    html = render_table_one(
                        kicker=kicker, title=title,
                        subtitle=str(fm.get("subtitle") or ""),
                        tcap=_tcap,
                        table=tt, summary=note_1 or note_2, caption=_tcap)
                elif pt == "table_notes_v":
                    html = render_table_notes_v(
                        kicker=kicker, title=title,
                        subtitle=str(fm.get("subtitle") or ""),
                        tcap=_tcap,
                        table=tt, note_1=note_1, note_2=note_2, caption=_tcap)
                else:
                    html = render_table_notes_h(
                        kicker=kicker, title=title,
                        subtitle=str(fm.get("subtitle") or ""),
                        tcap=_tcap,
                        table=tt, note_1=note_1, note_2=note_2, caption=_tcap)
                return {"html": html, "layout": pt, "blocks": blocks,
                        "css_blocked": [], "fm": fm, "columns": columns}
            # 声明了表格页型却没有表格 -> 落到 content 兜底
        # ---- 双表类（dual_table_summary / notes_v / notes_h）----
        if pt in ("dual_table_summary", "dual_table_notes_v",
                  "dual_table_notes_h"):
            if len(tbls) >= 2:
                t1 = _parse_table_rows(tbls[0]["rows"])
                t2 = _parse_table_rows(tbls[1]["rows"])
                cap1 = str(tbls[0].get("caption")
                           or fm.get("table_caption") or "表 1")
                cap2 = str(tbls[1].get("caption") or "表 2")
                note_1 = notes[0] if len(notes) > 0 else ""
                note_2 = notes[1] if len(notes) > 1 else ""
                if pt == "dual_table_summary":
                    html = render_dual_table_summary(
                        kicker=kicker, title=title,
                        subtitle=str(fm.get("subtitle") or ""),
                        table_1=t1, table_2=t2, cap_1=cap1, cap_2=cap2,
                        note=note_1 or note_2)
                elif pt == "dual_table_notes_v":
                    html = render_dual_table_notes_v(
                        kicker=kicker, title=title,
                        subtitle=str(fm.get("subtitle") or ""),
                        table_1=t1, table_2=t2, cap_1=cap1, cap_2=cap2,
                        note_1=note_1, note_2=note_2)
                else:
                    html = render_dual_table_notes_h(
                        kicker=kicker, title=title,
                        subtitle=str(fm.get("subtitle") or ""),
                        table_1=t1, table_2=t2, cap_1=cap1, cap_2=cap2,
                        note_1=note_1, note_2=note_2)
                return {"html": html, "layout": pt, "blocks": blocks,
                        "css_blocked": [], "fm": fm, "columns": columns}

# ---- left_right 页型：左图/表 + 右侧 2~4 张说明卡 ----
    # readme（templates_1/README.md）规则：仅当「有图/表」且「说明文字 2 点以上」
    # 时用；右侧卡片最少 2 张、最多 4 张（白底 + 左色条 + 彩色序号 + 标题 + 说明）；
    # 说明超过 4 点必须拆两页（此处确定性截断到 4；Author prompt 强制拆分）。
    if pt in ("left_right_chart", "left_right_table"):
        figs = [b for b in blocks if b["kind"] == "figure"]
        figs = [b for b in figs
                if not _asset_too_wide(str(b.get("src") or ""), figures)]
        tbls = [b for b in blocks if b["kind"] == "table"]

        # 右侧卡片：bullets（拆「标题：说明」）> callout > text 句段，最多 4 张
        _cards: list[dict] = []

        def _split_head(t: str) -> tuple[str, str]:
            t = _strip_em(t).strip()   # 先剥 markdown 强调，避免 ** 泄漏进卡片标题
            m = re.match(r"^(.*?[：:])\s*(.+)$", t)
            if m and len(m.group(1)) <= 30:
                return m.group(1).rstrip("：:").strip(), m.group(2).strip()
            return "", t

        def _card_ok(_hd: str, _tx: str) -> bool:
            # 兜底：head 是纯「表 N / 图 N」（如"表 7"）的卡片必然是图注/表注，
            # 不进右侧卡片；表注/图注只允许出现在左侧 CAPTION。
            if re.match(r"^[图表]\s*\d+$", _hd or ""):
                return False
            return not _is_caption_like(_hd + "：" + _tx)

        for b in blocks:
            if b["kind"] == "bullets":
                for _t in b["items"]:
                    _bt = _parse_bullet(_t)
                    if _is_caption_like(_bt["text"]):
                        continue
                    _hd, _tx = _split_head(_bt["text"])
                    if _card_ok(_hd, _tx):
                        _cards.append({"head": _hd, "text": _tx})
            elif b["kind"] == "callout":
                for _line in b["lines"]:
                    if _is_caption_like(_line):
                        continue
                    _hd, _tx = _split_head(_line)
                    if _card_ok(_hd, _tx):
                        _cards.append({"head": _hd, "text": _tx})
            elif b["kind"] == "text":
                for _seg in re.split(r"(?<=[。！？])\s+",
                                     str(b.get("text") or "")):
                    _seg = _seg.strip()
                    if _seg and not _is_caption_like(_seg):
                        _hd, _tx = _split_head(_seg)
                        if _card_ok(_hd, _tx):
                            _cards.append({"head": _hd, "text": _tx})
            if len(_cards) >= 4:
                break
        # 兜底：卡片不足 2 张时用 lead 补足（readme 要求至少 2 张）
        if len(_cards) < 2 and lead:
            _cards.insert(0, {"head": "", "text": lead})
        _cards = _cards[:4]

        if pt == "left_right_chart":
            if figs:
                _cap = str(figs[0].get("caption") or "")
                _chart_html = ('<img src="' + _resolve_asset(figs[0]["src"], figures)
                               + '" alt="" style="max-width:100%;max-height:100%;'
                               'object-fit:contain;">')
                html = render_left_right_chart(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart_title=_cap, chart=_chart_html,
                    caption=_cap, cards=_cards)
                return {"html": html, "layout": pt, "blocks": blocks,
                        "css_blocked": [], "fm": fm, "columns": columns}
            # 声明了图表版式却没有图 -> 落到 content 兜底
        else:  # left_right_table
            if tbls:
                _tt = _parse_table_rows(tbls[0]["rows"])
                _tcap = (str(fm.get("table_caption") or "")
                         or str(tbls[0].get("caption") or ""))
                html = render_left_right_table(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    table_title=_tcap, table=_tt,
                    caption=_tcap, cards=_cards)
                return {"html": html, "layout": pt, "blocks": blocks,
                        "css_blocked": [], "fm": fm, "columns": columns}
            # 声明了表格版式却没有表格 -> 落到 content 兜底

    # ---- 双图对比页（readme：涉及两张图表对比时必须在 dual_chart_v /
    #       dual_chart_h / dual_chart_summary 中选一个；说明 2/2/1 条）----
    if fm.get("page_type") in ("dual_chart_v", "dual_chart_h",
                               "dual_chart_summary", "dual_figure"):
        # dual_figure 是旧页型名，映射到 dual_chart_v（readme 双图三选一）
        if pt == "dual_figure":
            pt = "dual_chart_v"
        figs = [b for b in blocks if b["kind"] == "figure"]
        # 超宽图不入双图槽（与主分支同一准入规则）
        figs = [b for b in figs
                if not _asset_too_wide(str(b.get("src") or ""), figures)]
        notes: list[str] = []
        for b in blocks:
            if b["kind"] == "callout":
                _joined = " ".join(b["lines"]).strip()
                if _joined and not _is_caption_like(_joined):
                    notes.append(_joined)
            elif b["kind"] == "bullets":
                for t in b["items"]:
                    _btxt = _parse_bullet(t)["text"].strip()
                    if _btxt and not _is_caption_like(_btxt):
                        notes.append(_btxt)
            elif b["kind"] == "text":
                for _seg in re.split(r"(?<=[。！？])\s+",
                                      str(b.get("text") or "")):
                    if _seg.strip():
                        notes.append(_seg.strip())
            if len(notes) >= limit:
                break
        if len(figs) >= 2:
            cap1 = str(figs[0].get("caption") or "")
            cap2 = str(figs[1].get("caption") or "")
            img1 = _resolve_asset(figs[0]["src"], figures)
            img2 = _resolve_asset(figs[1]["src"], figures)
            n1 = notes[0] if len(notes) > 0 else ""
            n2 = notes[1] if len(notes) > 1 else ""
            if pt == "dual_chart_summary":
                html = render_dual_chart_summary(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart1_title=cap1, chart1=img1,
                    chart2_title=cap2, chart2=img2,
                    note=n1 or n2)
            elif pt == "dual_chart_h":
                html = render_dual_chart_h(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart1_title=cap1, chart1=img1,
                    chart2_title=cap2, chart2=img2,
                    note_1=n1, note_2=n2)
            else:
                html = render_dual_chart_v(
                    kicker=kicker, title=title,
                    subtitle=str(fm.get("subtitle") or ""),
                    chart1_title=cap1, chart1=img1,
                    chart2_title=cap2, chart2=img2,
                    note_1=n1, note_2=n2)
            return {"html": html, "layout": pt, "blocks": blocks,
                    "css_blocked": [], "fm": fm, "columns": columns}
        # 单图：降级为 chart_notes_one（新模板集无 big_figure；
        # 图 + 1 条说明，说明=页面其余要点的第一条）
        if len(figs) == 1:
            _all_pts: list[str] = []
            for b in blocks:
                if b["kind"] == "bullets":
                    for t in b["items"]:
                        _all_pts.append(_parse_bullet(t)["text"])
                elif b["kind"] == "callout":
                    _all_pts.append(" ".join(b["lines"]))
                elif b["kind"] == "text":
                    _all_pts.append(b["text"])
            fig0 = figs[0]
            _fcap = str(fig0.get("caption") or "")
            html = render_chart_notes_one(
                kicker=kicker, title=title,
                subtitle=str(fm.get("subtitle") or ""),
                chart_title=_fcap,
                chart=('<img src="' + _resolve_asset(fig0["src"], figures)
                       + '" alt="" style="max-width:100%;max-height:100%;'
                       'object-fit:contain;">'),
                note=_all_pts[0] if _all_pts else "", caption=_fcap)
            return {"html": html, "layout": "chart_notes_one",
                    "blocks": blocks, "css_blocked": [], "fm": fm,
                    "columns": columns}

    # ---- flow_1x4：横向 4 步流程页（LLM 显式 page_type 选择）----
    if fm.get("page_type") == "flow_1x4":
        steps: list[dict] = []
        for b in blocks:
            if b["kind"] == "bullets":
                for t in b["items"]:
                    pb = _parse_bullet(t)
                    if _is_caption_like(pb["text"]):
                        continue
                    cm = re.match(r"^(.{1,14}?)[：:](.*)$", pb["text"])
                    if cm and cm.group(1).strip() and cm.group(2).strip():
                        steps.append({"head": cm.group(1).strip(),
                                      "body": cm.group(2).strip()})
                    else:
                        steps.append({"head": "", "body": pb["text"]})
            elif b["kind"] == "text":
                steps.append({"head": "", "body": b["text"]})
            if len(steps) >= 4:
                break
        if steps:
            html = render_flow_1x4(
                kicker=kicker, title=title, lead=lead,
                steps=steps, source=source or fm.get("source", ""),
                page=page or fm.get("page", ""))
            return {"html": html, "layout": "flow_1x4", "blocks": blocks,
                    "css_blocked": [], "fm": fm, "columns": columns}

    # ---- conclusion_grid：纯文本大纲/总结骰子卡（readme：超过 4 点必选）----
    if fm.get("page_type") == "conclusion_grid":
        cards: list[dict] = []
        for b in blocks:
            if b["kind"] == "bullets":
                for t in b["items"]:
                    pb = _parse_bullet(t)
                    if _is_caption_like(pb["text"]):
                        continue
                    cm = re.match(r"^(.{1,14}?)[：:](.*)$", pb["text"])
                    if cm and cm.group(1).strip() and cm.group(2).strip():
                        cards.append({"head": cm.group(1).strip(),
                                      "body": cm.group(2).strip()})
                    else:
                        cards.append({"head": "", "body": pb["text"]})
            elif b["kind"] == "text":
                cards.append({"head": "", "body": b["text"]})
            if len(cards) >= 6:
                break
        if cards:
            html = render_conclusion_grid(
                kicker=kicker, title=title, lead=lead,
                cards=cards, source=source or fm.get("source", ""),
                page=page or fm.get("page", ""))
            return {"html": html, "layout": "conclusion_grid",
                    "blocks": blocks, "css_blocked": [], "fm": fm,
                    "columns": columns}

    # ---- content 兜底（新模板集无 content/big_table/big_figure 模板）----
    # readme 强制：表格与图片模板严格分开；纯文本大纲类禁图表。
    # 路由顺序：有表格 -> 整页三线表；有图片 -> 图表模板；纯文字 ->
    # conclusion_grid / flow_1x4。
    big_tbl = None
    for b in blocks:
        if b["kind"] == "table":
            try:
                big_tbl = _parse_table_rows(b["rows"])
            except Exception:          # 解析失败就退回卡片分支，别让整页崩掉
                big_tbl = None
            break
    if big_tbl is not None:
        _ns: list[str] = []
        for b in blocks:
            if b["kind"] == "bullets":
                for t in b["items"]:
                    _bt2 = _parse_bullet(t)["text"].strip()
                    if _bt2 and not _is_caption_like(_bt2):
                        _ns.append(_bt2)
                    if len(_ns) >= 2:
                        break
            elif b["kind"] == "text":
                for _seg in re.split(r"(?<=[。！？])\s+",
                                      str(b.get("text") or "")):
                    _seg = _seg.strip()
                    if _seg and not _is_caption_like(_seg):
                        _ns.append(_seg)
                    if len(_ns) >= 2:
                        break
            if len(_ns) >= 2:
                break
        _tcap2 = str(fm.get("table_caption") or "")
        html = render_table_notes_v(
            kicker=kicker, title=title,
            subtitle=str(fm.get("subtitle") or ""),
            tcap=_tcap2,
            table=big_tbl,
            note_1=_ns[0] if len(_ns) > 0 else "",
            note_2=_ns[1] if len(_ns) > 1 else "", caption=_tcap2)
        return {"html": html, "layout": "table_notes_v", "blocks": blocks,
                "css_blocked": [], "fm": fm, "columns": columns}
    cards = []
    for b in blocks:
        if b["kind"] == "bullets":
            for t in b["items"]:
                # 先检测 markdown 图片语法（_parse_bullet 会把 (assets/x.png) 当来源剥掉）
                _mimg = re.match(r"^!\[([^\]]*)\]\s*(?:\(([^)]*)\))?", t.strip())
                if _mimg:
                    _cap = (_mimg.group(1) or "").strip()
                    _src = (_mimg.group(2) or "").strip()
                    if _src:
                        cards.append({"img": _resolve_asset(_src, figures),
                                      "caption": (_cap or t.strip())[:120]})
                        continue
                    # ![图1] 无路径 -> 当普通文本处理（可能走补图）
                    t = _cap
                pb = _parse_bullet(t)
                body = pb["text"]
                head = ""
                # 小标题：要点 -> head/body 拆分
                cm = re.match(r"^(.{1,14}?)[：:](.*)$", body)
                if cm and cm.group(1).strip() and cm.group(2).strip():
                    head, body = cm.group(1).strip(), cm.group(2).strip()
                # 图 N：文本卡 -> 自动补论文原图
                fig_no = _match_fig_ref(head or body)
                if fig_no:
                    hit = next((f for f in figures
                                if str(f.get("fig_no")) == str(fig_no)), None)
                    if hit:
                        rel = Path(str(hit.get("path", ""))).name
                        cards.append({"img": f"assets/{rel}",
                                      "caption": (body or head)[:120]})
                        continue
                if _is_caption_like(body):
                    continue   # 图注/表注不进文字卡（"图 N"已在上方自动配图）
                cards.append({"head": head, "body": body})   # 〔pX〕 不进正文
        elif b["kind"] == "figure":
            # src 归一化：绝对路径/盘符 -> assets/<文件名>（sandbox 拦截盘符路径；
            # _ensure_assets 已把 workspace/assets 复制到 html_source/assets）
            src = _resolve_asset(str(b.get("src") or ""), figures)
            cap = str(b.get("caption") or "")
            # 清理 markdown 残留：](路径 尾部碎片
            cap = re.sub(r"\]\s*\([^)]*\)?\s*$", "", cap).strip()
            cap = re.sub(r"^[!\[\]]+", "", cap).strip()
            cards.append({"img": src, "caption": cap[:120]})
        elif b["kind"] == "table":
            cards.append({"table": _parse_table_rows(b["rows"]),
                         "tcaption": fm.get("table_caption", "")})
    # 图片宽度准入：超宽图从图卡中剔除（用户约束，会缩到不可读）
    img_cards = [c for c in cards if c.get("img")
                 and not _asset_too_wide(str(c.get("img") or ""), figures)]
    other_cards = [c for c in cards if not c.get("img")]
    if len(img_cards) >= 2:
        # 双图主体 -> dual_chart_v（2 条说明取其余文字卡）
        _ns2: list[str] = [c.get("body") or c.get("head") or ""
                           for c in other_cards if c.get("body") or c.get("head")]
        html = render_dual_chart_v(
            kicker=kicker, title=title,
            subtitle=str(fm.get("subtitle") or ""),
            chart1_title=str(img_cards[0].get("caption") or ""),
            chart1=img_cards[0].get("img", ""),
            chart2_title=str(img_cards[1].get("caption") or ""),
            chart2=img_cards[1].get("img", ""),
            note_1=_ns2[0] if len(_ns2) > 0 else "",
            note_2=_ns2[1] if len(_ns2) > 1 else "")
        return {"html": html, "layout": "dual_chart_v", "blocks": blocks,
                "css_blocked": [], "fm": fm, "columns": columns}
    if len(img_cards) == 1:
        # 单图主体 -> chart_notes_one（图 + 其余文字第一条做说明）
        ic = img_cards[0]
        _ns1 = [c.get("body") or c.get("head") or ""
                for c in other_cards if c.get("body") or c.get("head")]
        _icap = str(ic.get("caption") or "")
        html = render_chart_notes_one(
            kicker=kicker, title=title,
            subtitle=str(fm.get("subtitle") or ""),
            chart_title=_icap,
            chart=('<img src="' + str(ic.get("img") or "")
                   + '" alt="" style="max-width:100%;max-height:100%;'
                   'object-fit:contain;">'),
            note=_ns1[0] if _ns1 else "", caption=_icap)
        return {"html": html, "layout": "chart_notes_one",
                "blocks": blocks, "css_blocked": [], "fm": fm,
                "columns": columns}
    # 方法/实验块自动配论文原图（引擎兜底：LLM 漏写图片时补图，
    # 避免 PPT 全程无图；纯文本大纲类/背景/展望仍禁图表）
    auto_figs: list[dict] = []
    if not img_cards:
        _kb = str(kicker or "").strip()
        if _kb in ("方法", "实验", "METHOD", "EXPERIMENT"):
            _want = ["fig01", "fig02", "fig03", "fig04", "fig05"]
            for _w in _want:
                _hit = next((f for f in figures
                             if _fig_key(f) == _w
                             and not _asset_too_wide(
                                 str(f.get("path") or ""), figures)), None)
                if _hit:
                    _rel = _resolve_asset(str(_hit.get("path") or ""), figures)
                    auto_figs.append(
                        {"img": '<img src="' + _rel + '" alt="" '
                         'style="max-width:100%;max-height:100%;'
                         'object-fit:contain;">',
                         "caption": str(_hit.get("caption") or "")[:120]})
        auto_figs = auto_figs[:2]
    if auto_figs:
        img_cards = list(auto_figs)
        other_cards = [c for c in other_cards
                       if c.get("head") or c.get("body")]
        _ns2 = [c.get("body") or c.get("head") or ""
                for c in other_cards if c.get("body") or c.get("head")]
        if len(img_cards) >= 2:
            html = render_dual_chart_v(
                kicker=kicker, title=title,
                subtitle=str(fm.get("subtitle") or ""),
                chart1_title=str(img_cards[0].get("caption") or ""),
                chart1=img_cards[0].get("img", ""),
                chart2_title=str(img_cards[1].get("caption") or ""),
                chart2=img_cards[1].get("img", ""),
                note_1=_ns2[0] if len(_ns2) > 0 else "",
                note_2=_ns2[1] if len(_ns2) > 1 else "")
            return {"html": html, "layout": "dual_chart_v", "blocks": blocks,
                    "css_blocked": [], "fm": fm, "columns": columns}
        _ic = img_cards[0]
        html = render_chart_notes_one(
            kicker=kicker, title=title,
            subtitle=str(fm.get("subtitle") or ""),
            chart_title=str(_ic.get("caption") or ""),
            chart=('<img src="' + str(_ic.get("img") or "")
                   + '" alt="" style="max-width:100%;max-height:100%;'
                   'object-fit:contain;">'),
            note=_ns2[0] if _ns2 else "",
            caption=str(_ic.get("caption") or ""))
        return {"html": html, "layout": "chart_notes_one",
                "blocks": blocks, "css_blocked": [], "fm": fm,
                "columns": columns}
    # 纯文字：≤4 点 flow_1x4 / >4 点 conclusion_grid（禁图表）
    _txt = [c for c in other_cards
            if c.get("head") or c.get("body")]
    if len(_txt) <= 4:
        html = render_flow_1x4(
            kicker=kicker, title=title, lead=lead,
            steps=_txt, source=source or fm.get("source", ""),
            page=page or fm.get("page", ""))
        return {"html": html, "layout": "flow_1x4", "blocks": blocks,
                "css_blocked": [], "fm": fm, "columns": columns}
    html = render_conclusion_grid(
        kicker=kicker, title=title, lead=lead,
        cards=_txt[:6], source=source or fm.get("source", ""),
        page=page or fm.get("page", ""))
    return {"html": html, "layout": "conclusion_grid", "blocks": blocks,
            "css_blocked": [], "fm": fm, "columns": columns}

    # 兜底：无 blocks 内容（理论不可达）
    return {"html": "", "layout": layout, "blocks": blocks,
            "css_blocked": [], "fm": fm, "columns": columns}


if __name__ == "__main__":
    import sys
    md_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("out.html")
    theme = sys.argv[3] if len(sys.argv) > 3 else "theme-light"
    md = md_path.read_text(encoding="utf-8")
    res = render_markdown(md, theme=theme)
    out_path.write_text(res["html"], encoding="utf-8")
    print("layout:", res["layout"], "| blocks:", len(res["blocks"]),
          "| css_blocked:", len(res["css_blocked"]))
