# -*- coding: utf-8 -*-
"""html_author_agent.py：HTML 真源的内容页作者（LLM 只填内容，不写布局）。

用户拍板的模板方案：布局完全由预定义模板（page_engine）决定，LLM 只输出
**纯内容 Markdown**（标题/lead/要点/表格/图引用），md_engine 渲染成完整
1280×720 HTML。本节点：

  1. 并行调 LLM 生成每个内容页的 Markdown（页级并行，_AUTHORS 线程）；
  2. md_engine 确定性渲染（P3：lead 必写强制兜底；大表自动降级 big_table）；
  3. 固定页确定性编排：封面 + TOC 大纲 + 章节分隔页 + 结尾页（不调 LLM，
     照 page_engine 模板，保证全篇背景/版式一致——用户此前多次指出
     "背景颜色不一致"的根因就是固定页由 LLM 自由写）；
  4. 写 manifest.json（页序 -> slide_id/title，供 critic/renderer 对齐）。

审查反馈注入（P2/P4/P5）：ContentCheck / 叙事链 / 证据完整性的结论作为
补写指令追加给 Author（见 _gen_feedback），重写聚焦缺失点而非推倒重来。
"""
from __future__ import annotations

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import WORKSPACE
from ..models import save_json
from ..sandbox import sanitize_html
from .state import AgentState

HTML_DIR = WORKSPACE / "html_source"
_AUTHORS = int(os.environ.get("P2P_AUTHOR_WORKERS", "8"))

#: LLM 输出协议：只输出内容 Markdown（布局由模板决定，LLM 不许写 CSS/布局）。
_SYSTEM = """你是学术 PPT 的内容作者。给出一页论文内容卡片，你只输出这一页的
**内容 Markdown**（不写任何 HTML/CSS/布局）——布局由固定模板渲染，你只填内容。

协议：
- front-matter（--- 开头 --- 结尾）可选字段：kicker（栏目小标签）、
  lead（**必写**：本页核心结论一句话，模板里加粗置顶）、
  page_type（chart_notes_one|chart_notes_v|chart_notes_h|table_one|
  table_notes_v|table_notes_h|dual_chart_summary|dual_chart_v|dual_chart_h|
  dual_table_summary|dual_table_notes_v|dual_table_notes_h|left_right_chart|left_right_table|flow_1x4|
  conclusion_grid；默认自动判断：有表格选表格类、有图片选图表类、
  纯文字按点数选 flow_1x4/conclusion_grid）、
  callout_title（底部"要点"条的标题，默认"要点"）；
- 标题：一个 ## 行（页面 H1，一句话核心结论）；
- 要点：- 开头，每条 ≤ 45 字；小标题用「**小标题**：内容」格式；
- 图片：![图注](assets/figNN.png)——图必须从论文资产清单里选，
  禁止编造不存在的图；**宽度超过 1100px 的超宽图（如 fig06/fig07）
  放进模板会缩到不可读，禁止引用**；
- 表格：完整 Markdown 管道表（| 列 | 列 |），渲染层自动三线表 +
  数值列蓝色斜体，你只管给出完整表格；
- 页型选择（布局由模板决定，你只声明 page_type 并按模板填内容）：
  · **表格主体**（有数据表格时必须选表格类）：table_one（表格+1 条总结）/
    table_notes_v（表格+2 条说明，上下）/ table_notes_h（表格+2 条说明，左右）；
  · **图片主体**（有图表/图片时必须选图表类）：chart_notes_one（图+1 条总结）/
    chart_notes_v（图+2 条说明，上下）/ chart_notes_h（图+2 条说明，左右）；
  · **双图对比**：dual_chart_summary（2 图+1 条总结）/ dual_chart_v /
    dual_chart_h（2 图+2 条说明），放**恰好 2 张图**；
  · **双表对比**：dual_table_summary（2 表+1 条总结）/ dual_table_notes_v /
    dual_table_notes_h（2 表+2 条说明），放**恰好 2 张表**；
  · **左图/表 + 右侧多卡说明**（有图/表且说明文字 ≥2 点时）：
    left_right_chart（左侧单图）/ left_right_table（左侧三线表），右侧
    2~4 张说明卡（每张「**小标题**：说明」）；说明超过 4 点必须拆成两页，
    每页最多 4 张卡，禁止硬塞一页；
  · **纯文本大纲/总结**（不允许任何图表/图片/表格）：≤4 点用 flow_1x4
    （每条「**小标题**：说明」格式），>4 点用 conclusion_grid（5-6 点骰子卡）；
- **图表约束（硬性，像工具说明一样遵守）**：
  ① **表格与图片模板严格分开**：有表格只能用表格类（table_*/dual_table_*），
     有图片只能用图表类（chart_notes_*/dual_chart_*），**严禁混用**
     （图表模板里塞表格、表格模板里塞图片都是违规）；
  ② 说明条数上限：one/summary 版**最多 1 条**（每条 ≤ 40 字），
     v/h 版**最多 2 条**；
  ③ flow_1x4 / conclusion_grid / toc / chapter 页型**禁止放任何图表**；
  ④ toc 大纲页是纯文字，**绝不允许出现任何图表或表格**。

密度约束（硬性，搬自成熟 PPT Agent SlideGen 的 content >= 40 且 <= 400 words）：
- 每页**非标题正文描述的总词数必须 ≥40 词且 ≤400 词**（中文按"每个汉字 1 词"
  近似：即正文约 120~1200 字；标题、图注、表格单元格不计入，要点/lead/说明计入）；
- **禁止敷衍式描述**：正文不得只有 1~2 句话或占位式空话（"详见原文"、
  "该部分重要"这类一律不许）；每页正文必须有实质性展开，把方法怎么做、
  结论为什么成立讲透；
- 词数超出 400 则信息过载，请删减次要细节收敛。

书面化纪律（硬性）：
- 所有描述必须是**完整、通顺的书面语句**（学术书面语），禁止口语化、
  电报式短语、碎片化罗列；要点也必须是完整句子（如「**小标题**：完整的一句话说明」），
  不允许"提出了新方法""见实验部分"这类半截话。

纪律：
1. lead 必写（一句话总结本页核心结论）；
2. 所有数字、指标、表号必须来自论文原文/资产清单，禁止编造；
3. 〔pX〕页码引用**不进正文**（模板会自动剥除）；
4. 每页 3~5 条要点，宁少勿多；标题承诺什么，要点必须展开什么。"""


_README_CACHE: str | None = None


def _load_readme() -> str:
    """templates_1/README.md 全文（模块级缓存），注入 Author prompt 让 LLM
    每次填内容前明确所有模板约束。文件变更后进程重启生效。"""
    global _README_CACHE
    if _README_CACHE is None:
        p = Path(__file__).resolve().parents[2] / "templates_1" / "README.md"
        try:
            _README_CACHE = p.read_text(encoding="utf-8")
        except OSError as e:
            _README_CACHE = ""
            print(f"[HTMLAuthor] 读取 templates_1/README.md 失败: {e}")
    return _README_CACHE or ""

def _table_md(table: dict) -> str:
    """slide.table -> Markdown 管道表。"""
    header = table.get("header") or []
    rows = table.get("rows") or []
    n_cols = max([len(header)] + [len(r) for r in rows] + [1])
    header = (header + [""] * n_cols)[:n_cols]
    lines = ["| " + " | ".join(str(c or "") for c in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows:
        cells = (r + [""] * n_cols)[:n_cols]
        lines.append("| " + " | ".join(str(c or "") for c in cells) + " |")
    return "\n".join(lines)


def _content_card(slide: dict, parsed: dict) -> str:
    """把 slide 数据拼成给 LLM 的内容卡片（标题/要点/表格/图资产）。"""
    lines = []
    t = str(slide.get("title") or "")
    if t:
        lines.append(f"页面标题：{t}")
    sec = str(slide.get("section") or "")
    if sec:
        lines.append(f"所属章节：{sec}")
    purpose = str(slide.get("purpose") or "")
    if purpose:
        lines.append(f"本页 purpose（必须兑现）：{purpose}")
    bl = slide.get("bullets") or []
    if bl:
        lines.append("要点素材：")
        for b in bl:
            if isinstance(b, str):
                lines.append(f"  - {b}")
            elif isinstance(b, dict):
                head = b.get("head") or ""
                txt = b.get("text") or b.get("body", "")
                pno = b.get("page")
                ref = f"〔p{pno}〕" if isinstance(pno, int) else ""
                lines.append(f"  - **{head}**：{txt} {ref}".rstrip())
    tb = slide.get("table")
    if tb:
        lines.append("表格数据（转 Markdown 表）：")
        lines.append(_table_md(tb))
    if slide.get("takeaway"):
        lines.append(f"（可选）takeaway：{slide['takeaway']}")
    ev = slide.get("_rag_evidence")
    if ev:
        lines.append(f"检索到的论文证据（只能引用，不可编造）：\n{str(ev)[:1500]}")
    figs = (parsed or {}).get("figures") or []
    if figs:
        lines.append("论文图资产（只能从这里选 src）：")
        for f in figs:
            p = Path(str(f.get("path") or ""))
            lines.append(f"  - 图 {f.get('fig_no')}: assets/{p.name}"
                        f"（{str(f.get('caption') or '')[:80]}）")
    return "\n".join(lines)


def _theme() -> str:
    t = str(os.environ.get("P2P_THEME", "theme-light")).strip()
    return t if t in ("theme-light", "theme-dark", "theme-paper") else "theme-light"


def _paper_label(slide: dict) -> str:
    """页脚 source：论文标签。"""
    return str(slide.get("_paper") or "")


def _content_judge_enabled() -> bool:
    """内容审查开关：P2P_CONTENT_JUDGE=0 时跳过（默认开启）。"""
    return os.environ.get("P2P_CONTENT_JUDGE", "1") == "1"


def _total_pages() -> int:
    try:
        return max(1, int(os.environ.get("P2P_TOTAL_PAGES", "0")))
    except ValueError:
        return 1


def _ensure_assets(html_dir: Path) -> None:
    """把 workspace/assets 下的论文图复制到 html_source/assets（img src 相对引用）。"""
    from ..config import ASSETS_DIR
    dst = html_dir / "assets"
    dst.mkdir(parents=True, exist_ok=True)
    if ASSETS_DIR.exists():
        for p in ASSETS_DIR.glob("*.png"):
            if not (dst / p.name).exists():
                try:
                    (dst / p.name).write_bytes(p.read_bytes())
                except OSError:
                    pass


def _extract_md(text: str) -> str:
    """从 LLM 输出里提取 Markdown（容忍 ``` 代码块包裹）。"""
    m = re.search(r"```(?:markdown|md)?\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


def _gen_feedback(slide: dict) -> str:
    """把 slide 上的审查结论拼成 Author 可执行的补写指令（P2/P4/P5 反馈）。"""
    parts = []
    if str(slide.get("content_check") or "") == "unfulfilled":
        parts.append("【ContentCheck】页面未兑现标题/purpose 承诺：" +
                     str(slide.get("content_missing") or "请补充承诺内容"))
    na = str(slide.get("narrative_action") or "")
    if na == "rewrite":
        parts.append("【叙事链】该页 purpose 与标题脱节或太空泛，请重写聚焦：" +
                     str(slide.get("narrative_reason") or "聚焦本页 unique 结论"))
    elif na == "merge":
        parts.append("【叙事链】该页与相邻页信息重叠，请只保留本页 unique 观点")
    for it in slide.get("evidence_issues") or []:
        parts.append("【证据完整性】" + str(it.get("desc") or ""))
    return "\n".join(parts)


def rewrite_slide(llm, slide: dict, parsed: dict, out_path: Path, index: int,
                  retries: int = 2):
    """按页重写（供 Router content 桶调用）：复用 _gen_one，把审查反馈注入。

    重写后补页脚页号重排（编排时统一做的 replace 不经过单页重写，
    否则残留 "00 / 00" 会被几何检查器抓为页脚重叠——实测根因）。
    """
    res = _gen_one(llm, slide, parsed, out_path, index, retries=retries)
    if res[1] and out_path.exists():
        html = out_path.read_text(encoding="utf-8")
        total = _deck_total(out_path.parent)
        html = html.replace("00 / 00", f"{index} / {total}")
        out_path.write_text(html, encoding="utf-8")
    return res


def _deck_total(html_dir: Path) -> int:
    """从 manifest.json 读整册页数（页脚 'N / TOTAL'）。"""
    mf = html_dir / "manifest.json"
    if mf.exists():
        try:
            import json
            return len((json.loads(mf.read_text(encoding="utf-8"))
                        .get("pages") or []))
        except Exception:  # noqa: BLE001
            pass
    return 31


def _gen_one(llm, slide: dict, parsed: dict, out_path: Path, index: int,
             retries: int = 2):
    """生成一页：LLM 出 Markdown -> md_engine 渲染 HTML。

    返回 (index, ok, path, removed, err, stats)。
    stats = {"md_chars": n, "html_chars": n, "theme": str}（token 对比用）
    """
    card = _content_card(slide, parsed)
    # 页型由 LLM 按内容自主选择（system prompt 里有「可用页型卡片」清单）：
    #   单图表/表 -> chart_notes_v/h/one / table_summary；双图 -> dual_chart_*；
    #   纯文本大纲/总结 -> flow_1x4（≤4 点）/ conclusion_grid（>4 点）；
    #   单张大图配文 -> layout=row / col；常规并列 -> content（默认）。
    _readme = _load_readme()
    user = (f"【模板约束——先阅读下方 README 并严格遵守其中所有约束，再填写本页内容】\n"
            f"{_readme}\n\n"
            f"请把下面这一页的内容卡片写成 Markdown 内容。\n"
            f"按内容从「可用页型卡片」中选最合适的页型：图表为主体页用\n"
            f"chart_notes_v/h/one 或 table_summary（双图用 dual_chart_v/h/summary）；\n"
            f"纯文本大纲/总结 ≤4 点用 flow_1x4、>4 点用 conclusion_grid（禁图表）；\n"
            f"单张大图配文用 layout=row；其余默认 layout=content（骰子卡片）。\n"
            f"lead 必写（本页核心结论一句话）。\n\n"
            f"{card}\n\n输出 Markdown。")
    # 审查反馈注入（P2/P4/P5）：ContentCheck / 叙事链 / 证据完整性的结论
    # 作为**补写指令**追加给 Author，让重写针对缺失点聚焦而不是推倒重来
    # （照 ppt-master visual-review.md §5：子代理带 spec_says vs
    # render_delivers 差距回到 Author 补内容）。
    _fb = _gen_feedback(slide)
    msgs = [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user}]
    if _fb:
        msgs.append({"role": "user", "content":
                     "【审查反馈——必须针对反馈补写/修改，其余内容保持不变】\n"
                     + _fb})
    md = ""
    last_err = ""
    for attempt in range(1 + retries):
        try:
            md = llm.chat_text(msgs, temperature=0.3, max_tokens=6000)
        except Exception as e:  # noqa: BLE001
            last_err = f"LLM 调用失败: {e}"
            md = ""
        extracted = _extract_md(str(md or ""))
        if extracted and len(extracted.strip()) > 20:
            md = extracted
            break
        if attempt < retries:
            time.sleep(2 * (attempt + 1))

    stats = {"md_chars": len(str(md or "")), "html_chars": 0, "theme": _theme()}
    if not md or len(md.strip()) <= 20:
        debug_dir = out_path.parent / "_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"slide{index:02d}_raw.txt").write_text(
            str(md or ""), encoding="utf-8")
        (debug_dir / f"slide{index:02d}_err.txt").write_text(
            last_err or "空响应", encoding="utf-8")
        return index, False, out_path, [], last_err or "LLM 输出不是有效 Markdown", stats

    # ---- 内容审查闭环（用户拍板 2026-10-04：judge 只审 LLM 生成的内容，
    # 不审整页布局；审查通过后才渲染模板，不合格带具体 issue 反馈重写）----
    # 维度：① 充实度（非标题正文 40~400 词，SlideGen 密度约束搬入，程序硬闸门）
    #       ② 书面化（禁口语/电报/占位空话）③ 相关性（兑现标题与 purpose）。
    title = str(slide.get("title") or "")
    section = str(slide.get("section") or "")
    purpose = str(slide.get("purpose") or "")
    judge_rounds = 0
    md_words = 0
    judge_passed = True
    judge_issues: list[str] = []
    if _content_judge_enabled():
        from .content_judge_agent import judge_content
        for _j in range(3):  # 1 次审查 + 最多 2 次重写（用户强调防死循环）
            judge_rounds += 1
            verdict = judge_content(llm, title, section, purpose, md)
            md_words = verdict.get("word_count") or 0
            judge_passed = bool(verdict.get("pass"))
            judge_issues = [str(i) for i in (verdict.get("issues") or [])]
            stats["judge_rounds"] = judge_rounds
            stats["md_words"] = md_words
            if judge_passed:
                break
            if _j >= 2:  # 已达重写上限，用最后一次输出（记录 warning）
                break
            msgs.append({"role": "user", "content":
                         "【内容审查反馈——必须针对以下每条意见逐条修改对应内容，"
                         "其余内容保持不变】\n" +
                         "\n".join(f"- {i}" for i in judge_issues)})
            try:
                md = _extract_md(str(llm.chat_text(
                    msgs, temperature=0.3, max_tokens=6000) or ""))
            except Exception as e:  # noqa: BLE001
                last_err = f"内容重写失败: {e}"
                break
            if not md or len(md.strip()) <= 20:
                break
        if not judge_passed:
            print(f"[HTMLAuthor] 第{index}页内容审查 {judge_rounds} 轮仍未通过"
                  f"（正文词数 {md_words}），仍渲染最终版；问题摘要："
                  f"{judge_issues[:2]}")

    # md_engine 渲染成完整 HTML
    try:
        sys_path = str(Path(__file__).resolve().parents[2] / "workspace" / "templates")
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from md_engine import render_markdown
        res = render_markdown(md, theme=_theme(),
                              source=_paper_label(slide),
                              page="00 / 00",   # 页号在编排时统一重排
                              figures=parsed.get("figures") or [])  # “图 N”补图
        html = res["html"]
        stats["html_chars"] = len(html)
        if res["css_blocked"]:
            print(f"[HTMLAuthor] 第{index}页 CSS 覆盖被拦 {len(res['css_blocked'])} 项"
                  f"（{res['css_blocked'][:2]}）")
        if res.get("fm", {}).get("lead_auto"):
            print(f"[HTMLAuthor] 第{index}页 lead 缺失，已自动兜底提取")
    except Exception as e:  # noqa: BLE001
        last_err = f"md_engine 渲染失败: {e}"
        debug_dir = out_path.parent / "_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"slide{index:02d}_err.txt").write_text(
            last_err + "\n---md---\n" + str(md), encoding="utf-8")
        return index, False, out_path, [], last_err, stats

    # 图片相对路径：md 里写 assets/xxx.png，需在 html_source/assets/ 下有副本
    _ensure_assets(out_path.parent)

    cleaned = sanitize_html(html, assets_dir=out_path.parent / "assets")
    out_path.write_text(cleaned["html"], encoding="utf-8")
    return index, True, out_path, cleaned["removed"], None, stats


def html_author_node(state: AgentState) -> dict:
    llm = state.get("_llm")
    outline = state.get("outline") or {}
    parsed = state.get("parsed") or {}
    logs = list(state.get("logs") or [])

    # 失败要响：以前这里直接 return，后续节点全部"无 HTML 源文件，跳过"，
    # 整条流水线会"成功"结束却**一个 pptx 都没产出**——静默的空结果比报错危险得多。
    if llm is None:
        raise RuntimeError(
            "[HTMLAuthor] 路径 B（P2P_HTML_SOURCE=1）需要 LLM：没有 Key 时不会产出"
            "任何页面。请配置 P2P_LLM_API_KEY，或改用路径 A（不设 P2P_HTML_SOURCE）"
            "的 offline 模式。")
    if not outline.get("slides"):
        raise RuntimeError(
            "[HTMLAuthor] 大纲为空（outline['slides'] 缺失）——这是上游 Planner 的问题，"
            "不是可以渲染的状态。请检查 planner 节点日志。")

    HTML_DIR.mkdir(parents=True, exist_ok=True)
    slides = list(outline["slides"])
    # 生成一个 page->slide 映射清单，供 render/critic/router 按页序对齐
    manifest = {"pages": []}

    # ---- 页序编排：TOC 大纲页 + 章节分隔页（确定性生成，不调 LLM）----
    # 最终页序：
    #   slide01 封面 -> slide02 TOC 大纲 -> [CH 分隔页 -> 内容页...] -> 总结
    _tpl_path = str(Path(__file__).resolve().parents[2] / "workspace" / "templates")
    if _tpl_path not in sys.path:
        sys.path.insert(0, _tpl_path)
    from page_engine import (render_chapter, render_cover, render_closing,
                             render_toc)

    def _fixed_html(tpl_html: str) -> str:
        cleaned = sanitize_html(tpl_html, assets_dir=HTML_DIR / "assets")
        return cleaned["html"]

    # 1) 先并行生成所有内容页（LLM）。
    #    封面页（slide_type=cover）由 render_cover 模板确定性渲染，不调 LLM：
    #    - 节省一次 LLM 调用（token）；
    #    - 避免 LLM 生成的"论文速览"内容页既当封面又进正文 -> 重复页 bug。
    gen_slides = [(i, s) for i, s in enumerate(slides, 1)
                  if str(s.get("slide_type", "")) != "cover"]
    results = []
    if len(gen_slides) > 1:
        with ThreadPoolExecutor(max_workers=_AUTHORS) as ex:
            futures = []
            for i, s in gen_slides:
                out_path = HTML_DIR / f"_raw{i:02d}.html"
                futures.append(ex.submit(_gen_one, llm, s, parsed, out_path, i))
            results = [f.result() for f in futures]
    elif gen_slides:
        i, s = gen_slides[0]
        results = [_gen_one(llm, s, parsed, HTML_DIR / f"_raw{i:02d}.html", i)]

    ok_n = 0
    stats_all = []
    for index, ok, path, removed, err, stats in results:
        stats_all.append({"index": index, **stats})
        if ok:
            ok_n += 1
            judge_txt = (f"，内容审查 {stats['judge_rounds']} 轮通过"
                         f"（正文 {stats['md_words']} 词）"
                         if stats.get("judge_rounds") else "")
            logs.append(f"[HTMLAuthor] 第{index}页生成 {path.name}"
                        f"（{stats['html_chars']} 字符{judge_txt}）")
        else:
            logs.append(f"[HTMLAuthor] 第{index}页生成失败: {err}")
            print(f"[HTMLAuthor] 第{index}页生成失败: {err}")

    # 2) 编排最终页序并落盘 slideNN.html（固定页确定性生成 + 内容页重命名）。
    #    章节分隔页：按 outline 的 section 分组，分组切换时插入章节页。
    cover_slide = next((s for s in slides
                        if str(s.get("slide_type", "")) == "cover"), {})
    paper_title = str(cover_slide.get("title")
                      or (outline.get("paper") or {}).get("title")
                      or "论文分享")
    meta_left = str((outline.get("paper") or {}).get("author") or "")
    meta_right = str((outline.get("paper") or {}).get("venue") or "")

    pages: list[dict] = []   # 最终页序 [{path, slide_id, title, section}]
    # cover
    cover_html = _fixed_html(render_cover(
        brand="Paper2PPT", title=paper_title,
        subtitle=str(cover_slide.get("subtitle") or "学术论文解读"),
        meta_left=meta_left, meta_right=meta_right))
    pages.append({"path": "slide01.html", "slide_id": "cover",
                  "title": paper_title, "section": "", "html": cover_html})
    # 章节顺序与描述**从大纲派生**，不再用 page_engine 里硬编码的 CHAPTER_ORDER。
    # 旧代码调 render_toc() 不传参，目录列的是写死的 4 个模块名，而实际章节页
    # 是按 section 变化动态插的——两者从不对应（v23 的目录与路线就互相矛盾）。
    section_meta: list[tuple[str, str]] = []
    for s in slides:
        sec = str(s.get("section") or "").strip()
        if sec and (not section_meta or section_meta[-1][0] != sec) \
                and sec not in [x[0] for x in section_meta]:
            section_meta.append((sec, str(s.get("_section_desc") or "")))
    # TOC 大纲页：按 readme「纯文本大纲类」规则——章节 ≤4 用 flow_1x4
    # （横向卡），>4 用 conclusion_grid（骰子卡）。每章卡片 = 章节名 +
    # 该章内容页小标题列表（LLM 为每个大纲生成 2-3 个小标题）。
    ch_titles: dict[str, list[str]] = {}
    for s in slides:
        sec = str(s.get("section") or "").strip()
        if sec:
            ch_titles.setdefault(sec, []).append(str(s.get("title") or ""))
    # TOC 总大纲页：固定用新 toc.html 模板（2×2 网格：01-04 大编号 +
    # 章节名 + 该章小标题描述），恒放第二页。四大块固定 -> 恒 4 项。
    sections = [{"no": f"{i:02d}", "title": sec,
                 "desc": "；".join(ch_titles.get(sec, [])[:3])}
                for i, (sec, _desc) in enumerate(section_meta, 1)]
    toc_html = _fixed_html(render_toc(title="汇报大纲", sections=sections))
    pages.append({"path": "slide02.html", "slide_id": "toc",
                  "title": "汇报大纲", "section": "", "html": toc_html})

    # 内容页：**按章节分组**插章节页。
    # 旧实现是「section 一变就插一个章节页」，只要大纲的 section 不连续
    # （A,B,A）就会插出重复章节——实测 v24 因此变成 31 页 12 个章节、
    # 同一章节出现两次、顺序错乱。现在按 section_meta 的顺序分组，
    # 章节数恒等于大纲里的章节数，且与目录一一对应。
    group_of: dict[str, str] = {sec: sec for sec, _ in section_meta}
    chapter_no = 0
    emitted: set[str] = set()
    for index, ok, path, removed, err, stats in results:
        if not ok:
            continue
        slide = next((s for i, s in gen_slides if i == index), {})
        section = str(slide.get("section") or "").strip()
        # 只在本章节**首次**出现时插一次章节页
        if section and section in group_of and section not in emitted:
            emitted.add(section)
            chapter_no += 1
            desc = dict(section_meta).get(section, "")
            chap_html = _fixed_html(render_chapter(
                f"{chapter_no:02d}", section, desc))
            pages.append({"path": f"slide{len(pages) + 1:02d}.html",
                          "slide_id": f"chapter{chapter_no:02d}",
                          "title": section, "section": section,
                          "html": chap_html})
        pages.append({"path": f"slide{len(pages) + 1:02d}.html",
                      "slide_id": str(slide.get("slide_id") or f"s{index:02d}"),
                      "title": str(slide.get("title") or ""),
                      "section": section,
                      "html": path.read_text(encoding="utf-8")})

    # closing（结尾页）
    closing_html = _fixed_html(render_closing(paper=paper_title))
    pages.append({"path": f"slide{len(pages) + 1:02d}.html", "slide_id": "closing",
                  "title": "谢谢观看", "section": "", "html": closing_html})

    # 3) 清理上一轮遗留：目录必须是一次运行的完整快照。
    # 旧代码只删 _raw*.html，上一轮的 slideNN.html 原地不动；渲染端若按目录
    # glob 就会把它们一起渲染（实测 manifest 18 页却渲染出 31 页的 pptx）。
    # **必须在写盘之前清理**，否则会把本轮刚写的页面删掉。
    for pat in ("_raw*.html", "slide*.html", "manifest.json"):
        for old in HTML_DIR.glob(pat):
            try:
                old.unlink()
            except OSError:
                pass

    # 4) 页号重排：把模板里"00 / 00"替换为实际页号（模板 PAGE 占位）。
    total = len(pages)
    for p in pages:
        html = p["html"]
        page_no = p["path"][5:7].lstrip("0") or "1"
        html = html.replace("00 / 00", f"{page_no} / {total}")
        (HTML_DIR / p["path"]).write_text(html, encoding="utf-8")
        manifest["pages"].append({
            "page": p["path"][5:7], "slide_id": p["slide_id"],
            "title": p["title"], "section": p["section"]})

    save_json(manifest, HTML_DIR / "manifest.json")
    logs.append(f"[HTMLAuthor] 编排完成：{total} 页"
                f"（内容 {ok_n} 页 + 固定页 {total - ok_n} 页）")
    print(f"[HTMLAuthor] 编排完成：{total} 页（内容 {ok_n} 页）")
    return {"_html_dir": str(HTML_DIR), "logs": logs}
