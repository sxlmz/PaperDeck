# -*- coding: utf-8 -*-
"""page_engine.py：固定页型模板引擎（TOC 大纲页 / 章节分隔页 / 内容页）。

用户拍板的模板方案：预定义固定格式模板（HTML），LLM 只填内容变量，
不重头写布局。三种页型（对应三张参考图）：

  1. toc      大纲页   —— Contents + 大标题（蓝色下划线）+ 2×2 模块网格
                           （彩色序号 01-04 + 标题 + 一行说明）
  2. chapter  章节分隔页 —— CHAPTER 0X + 章节大标题 + 一行说明（每大章节前插入）
  3. content  内容页   —— kicker + H1 标题 + 加粗 lead（总的说明放最上）
                           + flex 骰子卡片网格

内容页的卡片网格是 **flex 骰子布局**：卡片数量决定排布（1 卡占满、
2 卡=1×2、3 卡=3 列、4 卡=2×2、6 卡=2×3），wrap 自动换行；每张卡片支持
三种形态：文本卡（编号+小标题+要点）/ 图片卡（图+图注）/ 表格卡（表+表题）。

这些页面**布局固定**（CSS 写死），LLM 只输出内容变量（标题/lead/卡片项），
从源头保证排版不崩、页面饱满。生成器（页面上的模块/章节聚合）由确定性
代码完成，不调用 LLM。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"

#: markdown 强调标记：**粗体** / __粗体__ / *斜体* / `代码`
_MD_EM = re.compile(
    r"\*\*(.+?)\*\*|__(.+?)__|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|`([^`]+)`")


def clean_text(s: str) -> str:
    """展示文本净化：**去掉 markdown 强调标记** + HTML 转义。

    为什么必须有：LLM 输出的是内容 Markdown，`**切分方式**：…` 这种写法
    一路原样进了模板占位符，最终在 PPT 里显示成字面量 `**切分方式**`。
    模板引擎是「LLM 只填变量」的最后一站，这里是收口的唯一正确位置——
    让每个 renderer 各自处理，迟早会漏一个。

    注意：HTML 片段（THEAD/TBODY/CONCL 等）**不要**过这个函数。
    """
    s = str(s or "")
    prev = None
    while prev != s:                      # 处理多重/嵌套强调
        prev = s
        s = _MD_EM.sub(lambda m: next((g for g in m.groups() if g), ""), s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _load(tpl: str) -> str:
    return (PAGES / f"{tpl}.html").read_text(encoding="utf-8")


def _fill(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


#: 章节序号（大章节编排，PPT 演讲顺序）
CHAPTER_ORDER = [
    ("01", "研究背景与动机",
     "长程多变量预测中 Transformer 的困境，以及本文的出发点"),
    ("02", "方法设计",
     "序列分块（Patching）、通道独立（Channel Independence）与自监督预训练"),
    ("03", "实验与结果",
     "8 个数据集上的长程预测、回看窗口与消融实验"),
    ("04", "关键洞察与结论",
     "通道独立为何有效、核心贡献与展望"),
]


def render_chapter(chapter_no: str, title: str, desc: str = "", *,
                   source: str = "", page: str = "") -> str:
    """章节分隔页：Chapter 0X + 大标题 + 副标题（templates_1/chapter.html）。"""
    tpl = _load("chapter")
    return _fill(tpl, CHAPTER_NUM=clean_text(chapter_no), TITLE=clean_text(title),
                 SUBTITLE=clean_text(desc))


def _card_html(card: dict, idx: int) -> str:
    """渲染一张卡片。card 支持三种形态：

      {"no": "01", "head": "...", "body": "..."}            文本卡
      {"no": "01", "img": "assets/fig1.png", "caption": "..."}  图片卡
      {"no": "01", "table": {"header": [...], "rows": [[...]]},
       "tcaption": "表题"}                                    表格卡
    """
    no = card.get("no") or f"{idx:02d}"
    if card.get("img"):
        return (f'<div class="card imgcard"><div class="no nofit">{no}</div>'
                f'<div style="flex:1;min-width:0">'
                f'<img class="cimg" src="{card["img"]}">'
                f'<div class="cap">{card.get("caption", "")}</div>'
                f'</div></div>')
    if card.get("table"):
        header = [str(c or "") for c in (card["table"].get("header") or [])]
        rows = [[str(c or "") for c in (r or [])]
                for r in (card["table"].get("rows") or [])]
        n_cols = max([len(header)] + [len(r) for r in rows] + [1])
        head = "".join(f"<th>{h}</th>" for h in
                       (header + [""] * n_cols)[:n_cols])
        body = "".join(
            "<tr>" + "".join(f"<td>{c}</td>" for c in
                             (r + [""] * n_cols)[:n_cols]) + "</tr>"
            for r in rows)
        tcap = (f'<div class="tcap">{card.get("tcaption", "")}</div>'
                if card.get("tcaption") else "")
        return (f'<div class="card"><div class="no nofit">{no}</div>'
                f'<div style="flex:1;min-width:0">{tcap}'
                f'<table>{head}{body}</table></div></div>')
    # 文本卡
    head = f'<div class="ch nofit">{card.get("head", "")}</div>' if card.get("head") else ""
    return (f'<div class="card"><div class="no nofit">{no}</div>'
            f'<div style="flex:1;min-width:0">{head}'
            f'<div class="cd nofit">{card.get("body", "")}</div></div></div>')


def render_big_table(*, kicker: str, title: str, lead: str,
                     table: dict, points: list[dict] | None = None,
                     tcap: str = "", source: str = "", page: str = "") -> str:
    """大表页：**结论在上、表格在下**（表格超过卡片规模时自动降级用）。

    table:  {"header": [...], "rows": [[...]]}
    points: [{"head": "...", "body": "..."}] —— 结论要点，渲染成表格**上方**的
            分点单行列表（不再做成底部卡片）。

    旧版把要点做成底部 flex 卡片、表格占中间：表格行一多就压穿到卡片上。
    表格高度是 html2pptx 从 `getBoundingClientRect()` 读的**完整自然高度**，
    不含滚动容器的裁剪，所以在 HTML 里看着能滚、导出后却重叠。
    结论移到上方后两者纵向彻底分离，这类重叠在结构上就不可能发生。
    """
    header = [clean_text(c) for c in (table.get("header") or [])]
    rows = [[clean_text(c) for c in (r or [])] for r in (table.get("rows") or [])]
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])
    # 数值列确定性判断：某列所有数据行都可解析为数字 -> 加 class="num"
    # （readme：数字列蓝色斜体 + 右对齐，模板 td.num 已定义样式）
    def _is_num(s) -> bool:
        t = str(s or "").strip().replace(",", "").replace("%", "")
        try:
            float(t)
            return True
        except ValueError:
            return False
    num_cols = {ci for ci in range(n_cols)
                if rows and all(ci < len(r) and _is_num(r[ci]) for r in rows)}
    def _td(ci: int, c: str) -> str:
        return f'<td class="num">{c}</td>' if ci in num_cols else f"<td>{c}</td>"
    thead = "".join(f"<th>{h}</th>" for h in (header + [""] * n_cols)[:n_cols])
    tbody = "".join(
        "<tr>" + "".join(_td(ci, c) for ci, c in
                         enumerate((r + [""] * n_cols)[:n_cols]))
        + "</tr>" for r in rows)

    items = []
    for p_ in (points or [])[:5]:
        head = clean_text(p_.get("head", ""))
        body = clean_text(p_.get("body", ""))
        if not (head or body):
            continue
        hk = f'<span class="ck">{head}：</span>' if head else ""
        items.append(f'<div class="ci"><span class="dot"></span>'
                     f'<span>{hk}{body}</span></div>')
    tpl = _load("big_table")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 LEAD=clean_text(lead), TCAP=clean_text(tcap),
                 THEAD=thead, TBODY=tbody, CONCL="\n    ".join(items),
                 SOURCE=clean_text(source), PAGE=clean_text(page))


def render_big_figure(*, kicker: str, title: str, lead: str,
                        img: str, figcap: str,
                        points: list[dict] | None = None,
                        source: str = "", page: str = "",
                        title_size: str = "34px") -> str:
    """大图页：左侧大图 + 右侧要点（图卡为主时自动降级用，类似 big_table）。

    img: 图片相对路径（assets/xxx.png）；figcap: 图注；
    points: [{"no": "01", "head": "...", "body": "..."}]（右侧要点，最多 4 条）
    """
    pts = []
    for i, p_ in enumerate((points or [])[:4], 1):
        no = p_.get("no") or f"{i:02d}"
        head = f'<div class="ph nofit">{p_.get("head", "")}</div>' if p_.get("head") else ""
        pts.append(f'<div class="fig-point"><div class="no nofit">{no}</div>'
                   f'<div>{head}<div class="cd nofit">{p_.get("body", "")}</div></div></div>')
    tpl = _load("big_figure")
    tpl = tpl.replace("font-size:34px; font-weight:800; color:#0F172A;",
                      f"font-size:{title_size}; font-weight:800; color:#0F172A;", 1)
    # fig-main 占位符是 {{LEFT_BODY}}（模板无 IMGSRC/FIGCAP 变量）：
    # 必须渲染成 <div class="sub"><img/><div class="cap">图注</div></div>，
    # 否则 {{LEFT_BODY}} 未替换会泄漏到页面 -> 图永远不显示。
    if img:
        left_body = (f'<div class="sub"><img src="{img}" alt="">'
                     f'<div class="cap">{figcap}</div></div>')
    else:
        left_body = (f'<div class="sub"><div class="cap" style="text-align:center;'
                     f'color:#94A3B8;">（无图片源，仅占位）</div></div>')
    return _fill(tpl, KICKER=kicker, TITLE=title, LEAD=lead,
                 LEFT_BODY=left_body, POINTS="\n    ".join(pts),
                 SOURCE=source, PAGE=page)


def render_content(*, kicker: str, title: str, lead: str,
                   cards: list[dict], source: str = "", page: str = "",
                   title_size: str = "36px") -> str:
    """内容页：kicker + H1 标题 + 加粗 lead + flex 骰子卡片网格。

    cards: [{"head": "...", "body": "..."} / {"img":..., "caption":...} /
            {"table": {...}, "tcaption":...}]（数量决定骰子排布）
    """
    n = max(len(cards), 1)
    grid_cls = f"c{min(n, 6)}" if n in (1, 2, 3, 4, 6) else "c4"
    items = [_card_html(c, i) for i, c in enumerate(cards, 1)]
    tpl = _load("content")
    tpl = tpl.replace("font-size:36px; font-weight:800; color:#0F172A;",
                      f"font-size:{title_size}; font-weight:800; color:#0F172A;", 1)
    return _fill(tpl, KICKER=kicker, TITLE=title, LEAD=lead,
                 GRID_CLASS=grid_cls, CARDS="\n    ".join(items),
                 SOURCE=source, PAGE=page)


def render_cover(*, brand: str, title: str, subtitle: str,
                 meta_left: str = "", meta_right: str = "") -> str:
    """封面页：浅米灰底 + 波形装饰 + 期刊名（templates_1/cover.html）。

    长标题确定性自适应：按字符数降字号、估算行数、副标题随之下移，
    避免论文全标题 3 行压住副标题（此前反复出现的封面重叠根因）。
    """
    tpl = _load("cover")
    n = len(title)
    if n <= 20:
        font = 52
    elif n <= 36:
        font = 44
    elif n <= 52:
        font = 38
    else:
        font = 32
    est_lines = max(1, int(n * font * 1.02 / 960) + 1)
    sub_top = 300 + est_lines * int(font * 1.28) + 16
    html = _fill(tpl, VENUE=clean_text(meta_right or brand),
                 TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle or meta_left))
    html = html.replace('<div class="title">',
                        f'<div class="title" style="font-size:{font}px">', 1)
    html = html.replace('<div class="subtitle">',
                        f'<div class="subtitle" style="top:{sub_top}px">', 1)
    return html


def render_closing(*, kicker: str = "THANK YOU", title: str = "谢谢观看",
                   paper: str = "", authors: str = "") -> str:
    """致谢/结束页：复用 cover 模板（浅米灰 + 波形 + 大字收尾）。

    新模板集（templates_1）没有 closing 模板，closing 页与封面同一视觉语言。
    """
    tpl = _load("cover")
    return _fill(tpl, VENUE=kicker, TITLE=title,
                 SUBTITLE=(paper or "Thanks for listening"))


def render_dual_figure(*, kicker: str, title: str, lead: str,
                        note_1: str, note_2: str,
                        img_1: str, img_2: str,
                        cap_1: str, cap_2: str,
                        source: str = "", page: str = "") -> str:
    """双图对比页：顶部左右两个结论块 + 下方并排两张等大图（各自图注）。

    适合：channel-mixing vs channel-independence、有/无某组件对比。
    """
    tpl = _load("dual_figure")
    return _fill(tpl, KICKER=kicker, TITLE=title, LEAD=lead,
                 NOTE_1=note_1, NOTE_2=note_2,
                 IMG_1=img_1, IMG_2=img_2, CAP_1=cap_1, CAP_2=cap_2,
                 SOURCE=source, PAGE=page)


def render_flow_1x4(*, kicker: str, title: str, lead: str = "",
                     steps: list[dict] | None = None, subtitle: str = "",
                     source: str = "", page: str = "") -> str:
    """横向流程/大纲卡页（templates_1/flow_1x4.html）：最多 4 点横排。

    steps: [{"head": "...", "body": "..."}]（body 可为字符串或 list 要点）
    readme 约束：纯文本大纲/总结 ≤4 点时用本模板，不允许图表。
    """
    items = []
    for i, s in enumerate((steps or [])[:4], 1):
        head = clean_text(s.get("head", ""))
        body = s.get("body", "")
        lis = ""
        if isinstance(body, list):
            for b in body[:3]:
                lis += f"<li>{clean_text(str(b))}</li>"
        elif str(body).strip():
            lis += f"<li>{clean_text(str(body))}</li>"
        items.append(f'<div class="card c{i}"><div class="no nofit">{i:02d}</div>'
                     f'<div class="ch">{head}</div><ul>{lis}</ul></div>')
    tpl = _load("flow_1x4")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle or lead),
                 CARDS="\n    ".join(items))


def render_toc(*, title: str, sections: list[dict] | None = None) -> str:
    """总大纲页（templates_1/toc.html）：2×2 网格，01-04 大编号 +
    章节标题 + 描述（每块小标题要点）。

    sections: [{"no": "01", "title": "背景", "desc": "要点1；要点2"}]
    """
    tpl = _load("toc")
    kw = {"TITLE": clean_text(title)}
    for i, s in enumerate((sections or [])[:4], 1):
        kw[f"M{i}_TITLE"] = clean_text(s.get("title") or "")
        kw[f"M{i}_DESC"] = clean_text(s.get("desc") or "")
    return _fill(tpl, **kw)


def _is_num_cell(s: str) -> bool:
    t = str(s or "").strip().replace(",", "").replace("%", "").replace("$", "")
    try:
        float(t)
        return True
    except (TypeError, ValueError):
        return False


def render_table_one(*, kicker: str, title: str, subtitle: str, tcap: str,
                     table: dict, summary: str, caption: str = "") -> str:
    """单表格 + 1 条总结（templates_1/table_one.html，.trow flex 网格表）。

    模板结构是 .trow.head / .trow.cell / .trow.last + .tcol（flex 行），
    渲染层必须生成该结构（HEAD_ROW/ROWS），否则 {{HEAD_ROW}} 泄漏。
    数值列加 .tcol.num（Georgia 蓝色斜体）。
    """
    header = [clean_text(c) for c in (table.get("header") or [])]
    rows = [[clean_text(c) for c in (r or [])]
            for r in (table.get("rows") or [])]
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])

    def _cells(cells: list[str]) -> str:
        out = []
        for ci, c in enumerate((cells + [""] * n_cols)[:n_cols]):
            cls = "tcol num" if ci > 0 and _is_num_cell(c) else "tcol"
            out.append(f'<div class="{cls}">{c}</div>')
        return "".join(out)

    head_row = '<div class="trow head">' + _cells(header) + "</div>"
    body_rows = []
    for ri, r in enumerate(rows):
        last = " last" if ri == len(rows) - 1 else ""
        body_rows.append(f'<div class="trow cell{last}">' + _cells(r) + "</div>")
    tpl = _load("table_one")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle), TCAP=clean_text(tcap),
                 HEAD_ROW=head_row, ROWS="\n    ".join(body_rows),
                 CAPTION=clean_text(caption), SUMMARY=clean_text(summary))


def render_chart_notes_v(*, kicker: str, title: str, subtitle: str,
                         chart_title: str, chart: str,
                         note_1: str, note_2: str,
                         caption: str = "") -> str:
    """竖排图表说明页：图表为主体 + 最多 2 点说明（竖排在下）。

    用户约束（2026-10-03）：凡涉及图表的页只能用 chart_notes_v /
    chart_notes_h / table_summary / big_figure 四模板之一；前两者说明 ≤2 点，
    后两者 ≤1 点。模板固定 2 个 note 槽位（n1 蓝条 / n2 绿条），
    note_2 传空即只显示 1 条。
    """
    tpl = _load("chart_notes_v")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART_TITLE=clean_text(chart_title),
                 CHART=chart, NOTE_1=clean_text(note_1),
                 NOTE_2=clean_text(note_2),
                 CAPTION=clean_text(caption))


def render_chart_notes_h(*, kicker: str, title: str, subtitle: str,
                         chart_title: str, chart: str,
                         note_1: str, note_2: str,
                         caption: str = "") -> str:
    """横排图表说明页：图表为主体 + 最多 2 点说明（左右并排在下）。"""
    tpl = _load("chart_notes_h")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART_TITLE=clean_text(chart_title),
                 CHART=chart, NOTE_1=clean_text(note_1),
                 NOTE_2=clean_text(note_2),
                 CAPTION=clean_text(caption))


def _table_parts(table: dict) -> tuple[str, str, set[int]]:
    """把表格 dict 转成 THEAD/TBODY；数值列自动加 num class（三线表蓝色斜体）。

    readme：表格类模板统一学术三线表（顶/中/底黑横线，无竖线），
    数字列蓝色斜体 td.num。
    """
    header = [clean_text(c) for c in (table.get("header") or [])]
    rows = [[clean_text(c) for c in (r or [])] for r in (table.get("rows") or [])]
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])
    def _is_num(s) -> bool:
        t = str(s or "").strip().replace(",", "").replace("%", "")
        try:
            float(t)
            return True
        except ValueError:
            return False
    num_cols = {ci for ci in range(n_cols)
                if rows and all(ci < len(r) and _is_num(r[ci]) for r in rows)}
    def _td(ci: int, c: str) -> str:
        return f'<td class="num">{c}</td>' if ci in num_cols else f"<td>{c}</td>"
    thead = "".join(f"<th>{h}</th>" for h in (header + [""] * n_cols)[:n_cols])
    tbody = "".join(
        "<tr>" + "".join(_td(ci, c) for ci, c in
                         enumerate((r + [""] * n_cols)[:n_cols]))
        + "</tr>" for r in rows)
    return thead, tbody, num_cols


def render_table_notes_v(*, kicker: str, title: str, subtitle: str, tcap: str,
                         table: dict, note_1: str, note_2: str,
                         caption: str = "") -> str:
    """单表格 + 2 条说明（templates_1/table_notes_v.html，说明上下排）。"""
    thead, tbody, _ = _table_parts(table)
    tpl = _load("table_notes_v")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle), TCAP=clean_text(tcap),
                 THEAD=thead, TBODY=tbody,
                 NOTE_1=clean_text(note_1), NOTE_2=clean_text(note_2),
                 CAPTION=clean_text(caption))


def render_table_notes_h(*, kicker: str, title: str, subtitle: str, tcap: str,
                         table: dict, note_1: str, note_2: str,
                         caption: str = "") -> str:
    """单表格 + 2 条说明（templates_1/table_notes_h.html，说明左右排）。"""
    thead, tbody, _ = _table_parts(table)
    tpl = _load("table_notes_h")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle), TCAP=clean_text(tcap),
                 THEAD=thead, TBODY=tbody,
                 NOTE_1=clean_text(note_1), NOTE_2=clean_text(note_2),
                 CAPTION=clean_text(caption))


def render_dual_table_summary(*, kicker: str, title: str, subtitle: str,
                              table_1: dict, table_2: dict,
                              cap_1: str, cap_2: str, note: str) -> str:
    """双表格 + 1 条总结（templates_1/dual_table_summary.html）。"""
    th1, tb1, _ = _table_parts(table_1)
    th2, tb2, _ = _table_parts(table_2)
    tpl = _load("dual_table_summary")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 TABLE1_TITLE=clean_text(cap_1), THEAD1=th1, TBODY1=tb1,
                 TABLE2_TITLE=clean_text(cap_2), THEAD2=th2, TBODY2=tb2,
                 NOTE=clean_text(note))


def render_dual_table_notes_v(*, kicker: str, title: str, subtitle: str,
                              table_1: dict, table_2: dict,
                              cap_1: str, cap_2: str,
                              note_1: str, note_2: str) -> str:
    """双表格 + 2 条说明（templates_1/dual_table_notes_v.html）。"""
    th1, tb1, _ = _table_parts(table_1)
    th2, tb2, _ = _table_parts(table_2)
    tpl = _load("dual_table_notes_v")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 TABLE1_TITLE=clean_text(cap_1), THEAD1=th1, TBODY1=tb1,
                 TABLE2_TITLE=clean_text(cap_2), THEAD2=th2, TBODY2=tb2,
                 NOTE_1=clean_text(note_1), NOTE_2=clean_text(note_2))


def render_dual_table_notes_h(*, kicker: str, title: str, subtitle: str,
                              table_1: dict, table_2: dict,
                              cap_1: str, cap_2: str,
                              note_1: str, note_2: str) -> str:
    """双表格 + 2 条说明（templates_1/dual_table_notes_h.html）。"""
    th1, tb1, _ = _table_parts(table_1)
    th2, tb2, _ = _table_parts(table_2)
    tpl = _load("dual_table_notes_h")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 TABLE1_TITLE=clean_text(cap_1), THEAD1=th1, TBODY1=tb1,
                 TABLE2_TITLE=clean_text(cap_2), THEAD2=th2, TBODY2=tb2,
                 NOTE_1=clean_text(note_1), NOTE_2=clean_text(note_2))


def render_chart_notes_one(*, kicker: str, title: str, subtitle: str,
                           chart_title: str, chart: str, note: str,
                           caption: str = "") -> str:
    """单图表 + 一条总结（templates_1/chart_notes_one.html）。

    readme 约束：单图表说明 ≤1 点（只有一条蓝竖条总结卡）。
    """
    tpl = _load("chart_notes_one")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART_TITLE=clean_text(chart_title),
                 CHART=chart, NOTE=clean_text(note),
                 CAPTION=clean_text(caption))


def render_dual_chart_v(*, kicker: str, title: str, subtitle: str,
                        chart1_title: str, chart1: str,
                        chart2_title: str, chart2: str,
                        note_1: str, note_2: str,
                        caption_1: str = "", caption_2: str = "") -> str:
    """双图并排 + 两点说明上下排列（templates_1/dual_chart_v.html）。"""
    tpl = _load("dual_chart_v")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART1_TITLE=clean_text(chart1_title), CHART1=chart1,
                 CHART2_TITLE=clean_text(chart2_title), CHART2=chart2,
                 CAPTION1=clean_text(caption_1 or chart1_title),
                 CAPTION2=clean_text(caption_2 or chart2_title),
                 NOTE_1=clean_text(note_1), NOTE_2=clean_text(note_2))


def render_dual_chart_h(*, kicker: str, title: str, subtitle: str,
                        chart1_title: str, chart1: str,
                        chart2_title: str, chart2: str,
                        note_1: str, note_2: str,
                        caption_1: str = "", caption_2: str = "") -> str:
    """双图并排 + 两点说明左右并排（templates_1/dual_chart_h.html）。"""
    tpl = _load("dual_chart_h")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART1_TITLE=clean_text(chart1_title), CHART1=chart1,
                 CHART2_TITLE=clean_text(chart2_title), CHART2=chart2,
                 CAPTION1=clean_text(caption_1 or chart1_title),
                 CAPTION2=clean_text(caption_2 or chart2_title),
                 NOTE_1=clean_text(note_1), NOTE_2=clean_text(note_2))


def render_dual_chart_summary(*, kicker: str, title: str, subtitle: str,
                              chart1_title: str, chart1: str,
                              chart2_title: str, chart2: str,
                              note: str) -> str:
    """双图并排 + 一条总结（templates_1/dual_chart_summary.html）。"""
    tpl = _load("dual_chart_summary")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART1_TITLE=clean_text(chart1_title), CHART1=chart1,
                 CHART2_TITLE=clean_text(chart2_title), CHART2=chart2,
                 NOTE=clean_text(note))


def render_conclusion_grid(*, kicker: str, title: str, subtitle: str = "",
                           lead: str = "", cards: list[dict] | None = None,
                           source: str = "", page: str = "") -> str:
    """纯文本大纲/总结骰子卡（templates_1/conclusion_grid.html，1-6 点）。

    cards: [{"head": "...", "body": "..."}]（body 可为字符串或 list 要点）
    骰子网格：1-3 点单行、4 点 2×2、5-6 点 3×2；不允许图表。
    """
    cards = cards or []
    n = max(min(len(cards), 6), 1)
    grid_cls = f"c{n}"
    items = []
    for i, c in enumerate(cards[:6], 1):
        head = clean_text(c.get("head", ""))
        body = c.get("body", "")
        if isinstance(body, list):
            body = "；".join(str(b) for b in body[:4])
        items.append(f'<div class="card"><div class="no">{i:02d}</div>'
                     f'<div class="ch">{head}</div>'
                     f'<div class="cd">{clean_text(str(body))}</div></div>')
    tpl = _load("conclusion_grid")
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle or lead),
                 GRID_CLASS=grid_cls, CARDS="\n    ".join(items))

def render_cover_wind(*, title: str, presenter: str) -> str:
    return _fill(_load("cover_wind"), TITLE=title, PRESENTER=presenter)


def render_toc_wind(sections: list[dict]) -> str:
    mods = [f'<div class="mod"><div class="no nofit">{s["no"]}</div>'
            f'<div class="mt">{s["title"]}</div></div>' for s in sections]
    return _fill(_load("toc_wind"), MODULES="\n    ".join(mods))


def render_chapter_wind(*, part: str, title: str) -> str:
    return _fill(_load("chapter_wind"), PART=part, TITLE=title)


def render_content_wind(*, section: str, body: str) -> str:
    return _fill(_load("content_wind"), SECTION=section, BODY=body)


def render_three_col(*, section: str, cols: list[dict]) -> str:
    items = [f'<div class="col"><div class="hd">{c["title"]}</div>'
             f'<div class="card"><div class="imgbox"><img src="{c.get("img","")}"></div>'
             f'<div class="desc">{c.get("desc","")}</div></div></div>'
             for c in cols[:3]]
    return _fill(_load("three_col"), SECTION=section, COLS="\n    ".join(items))


def render_left_right_wind(*, section: str, lead: str, right_title: str, right_cap: str,
                           img: str = "", right_img: str = "") -> str:
    return _fill(_load("left_right"), SECTION=section, LEAD=lead,
                 RIGHT_TITLE=right_title, RIGHT_CAP=right_cap,
                 IMG=img, RIGHT_IMG=right_img)


def render_three_card_wind(*, section: str, subtitle: str, cards: list[dict]) -> str:
    items = [f'<div class="card"><div class="hd">{c["title"]}</div>'
             f'<div class="body">{c["body"]}</div></div>' for c in cards[:3]]
    return _fill(_load("three_card"), SECTION=section, SUBTITLE=subtitle,
                 CARDS="\n    ".join(items))


def render_img_grid_wind(*, section: str, caption: str, imgs: list[str], n_cells: int = 5) -> str:
    cells = "\n    ".join(f'<div class="cell"><img src="{p}"></div>' for p in imgs[:n_cells])
    return _fill(_load("img_grid"), SECTION=section, CELLS=cells, CAPTION=caption)


def render_closing_wind(*, section: str, body: str, who: str) -> str:
    return _fill(_load("closing_wind"), SECTION=section, BODY=body, WHO=who)
def _rcard_html(no, head, text):
    """left_right 模板右侧的说明卡：白底 + 左色条 + 彩色序号 + 标题 + 说明。"""
    return (f'<div class="rcard"><div class="no">{no:02d}</div>'
            f'<div class="ch">{clean_text(head)}</div>'
            f'<div class="cd">{clean_text(text)}</div></div>')


def render_left_right_chart(*, kicker, title, subtitle,
                            chart_title, chart, caption,
                            cards):
    """左图/表 + 右侧多卡说明（templates_1/left_right_chart.html）。

    readme 约束：仅当「有图/表」且「说明文字 2 点以上」时用；右侧卡片
    2~4 张（flex 上下排列，白底 + 蓝/绿/橙/紫左色条 + 彩色序号）。
    超过 4 点必须拆成两页（调用方截断到 4，Author prompt 强制拆分）。
    CAPTION 放图注，卡片只写总结/解读。
    """
    tpl = _load("left_right_chart")
    cards_html = "\n    ".join(
        _rcard_html(i, c.get("head", ""), c.get("text", ""))
        for i, c in enumerate((cards or [])[:4], 1))
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 CHART_TITLE=clean_text(chart_title),
                 CHART=chart, CAPTION=clean_text(caption),
                 CARDS=cards_html)


def render_left_right_table(*, kicker, title, subtitle,
                            table_title, table, caption,
                            cards):
    """左三线表 + 右侧多卡说明（templates_1/left_right_table.html）。

    表格结构用 .trow flex 网格（同 table_one）：表头 HEAD_ROW + 数据行 ROWS，
    数值列 .tcol.num 蓝色。右侧卡片规则同 render_left_right_chart。
    """
    header = [clean_text(c) for c in (table.get("header") or [])]
    rows = [[clean_text(c) for c in (r or [])]
            for r in (table.get("rows") or [])]
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])

    def _cells(cells):
        out = []
        for ci, c in enumerate((cells + [""] * n_cols)[:n_cols]):
            cls = "tcol num" if ci > 0 and _is_num_cell(c) else "tcol"
            out.append(f'<div class="{cls}">{c}</div>')
        return "".join(out)

    head_row = '<div class="trow head">' + _cells(header) + "</div>"
    body_rows = []
    for ri, r in enumerate(rows):
        last = " last" if ri == len(rows) - 1 else ""
        body_rows.append(f'<div class="trow cell{last}">' + _cells(r) + "</div>")
    tpl = _load("left_right_table")
    cards_html = "\n    ".join(
        _rcard_html(i, c.get("head", ""), c.get("text", ""))
        for i, c in enumerate((cards or [])[:4], 1))
    return _fill(tpl, KICKER=clean_text(kicker), TITLE=clean_text(title),
                 SUBTITLE=clean_text(subtitle),
                 TABLE_TITLE=clean_text(table_title),
                 HEAD_ROW=head_row, ROWS="\n    ".join(body_rows),
                 CAPTION=clean_text(caption),
                 CARDS=cards_html)
