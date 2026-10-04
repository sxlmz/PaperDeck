# -*- coding: utf-8 -*-
"""关键页 VLM 结构化理解（target.md 的「跨模态对齐 / 图表精确理解」）。

思路：PyMuPDF 负责把 PDF 变成可检索的文本与素材，VLM 只用在它真正不可替代的
地方——**看图**。整篇 24 页全送 VLM 又慢又贵，所以按已有解析结果打分挑关键页
（有图注、有表、公式密集、方法/实验密集的页）。

产物落在 `parsed["vlm"]`：
    {"prompt_version": 1, "model": "...",
     "pages":    {"4": {"reasons": [...]}},
     "figures":  {"1": {...图谱/数值...}},
     "tables":   {"3": {...归一化表...}},
     "formulas": {"p4:b12": {...LaTeX + 语义...}}}

原则：**VLM 读数必须可溯源**。所有数值来自图上刻度，confidence 低于阈值的读数
不允许进入 bullets（见 planner 的溯源规则），表格文字里的数字永远优先于图内读数。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

PROMPT_VERSION = 1

# 默认不限制关键页数量：宁可慢，也不要在用户不知情时少看几页。
# 需要控成本时用 P2P_VLM_MAX_PAGES 显式设置，且跳过哪些页会被记录进 coverage。
MAX_PAGES_DEFAULT = 0            # 0 = 不限
MIN_CONFIDENCE = 0.8

# 单次 VLM 调用的 token 预算。表格要逐字转录整张表，给小了会被截断成
# 半截 JSON（实测 1800 token 时 finish_reason=length → 解析失败 → 不入缓存
# → 每次运行都重新付这 100 秒）。宁可给足。
_TOKENS = {"figure": 2048, "table": 4096, "formula": 1200}

# 送审图片的缩放倍率：表格宽、放大倍数高会让图片 token 暴涨拖慢响应
_SCALE = {"figure": 2.0, "table": 2.0, "formula": 3.0}

_VLM_WORKERS = 6


def _max_per_page(kind: str) -> int:
    """每页最多送审的块数；0 = 不限（默认）。

    旧实现硬编码 formula:1 / table:2，一页有 3 个公式时第 3 个直接消失，
    且不留痕迹。现在默认全送，需要控成本时用环境变量显式设置。
    """
    import os
    env = {"formula": "P2P_VLM_MAX_FORMULAS_PER_PAGE",
           "table": "P2P_VLM_MAX_TABLES_PER_PAGE"}[kind]
    try:
        return int(os.environ.get(env, "0"))
    except ValueError:
        return 0


def _cap(seq: list, n: int) -> list:
    """n<=0 表示不限；截断时返回原序列。"""
    return seq[:n] if n > 0 else seq

_FIGURE_SYS = """你是学术论文图表解析专家。看这张图，只报告你**确实从图上读到**的信息。
严格输出 JSON：
{"chart_type":"line|bar|scatter|heatmap|architecture|table|photo|other",
 "title":"图上标题（没有则空串）",
 "axes":{"x":{"label":"","unit":"","range":[]},"y":{"label":"","unit":"","range":[]}},
 "series":[{"name":"","points":[{"x":"","y":0.0}]}],
 "key_values":[{"label":"","value":""}],
 "takeaway":"一句工程结论（如：某方法在所有基线中最低）",
 "confidence":0.0,
 "uncertain":["无法确定的项"]}
硬性要求：
- 只填你在图上真能读到的数值；读不出来就留空数组或 null，**绝不猜测、绝不从标题推断数值**；
- 每个数值都要能在图上找到对应刻度或标注；
- confidence 反映你对所报数值的把握（0~1），拿不准就把对应项写进 uncertain。"""

_TABLE_SYS = """你是学术论文表格解析专家。看这张表格图，逐字转录。
严格输出 JSON：
{"title":"","header":["..."],"header_units":{"列名":"单位或口径"},
 "rows":[["..."]],"notes":"表注/加粗/下划线代表什么","takeaway":"","confidence":0.0}
硬性要求：
- 数字**逐字照抄**，不要计算任何派生数值，不要四舍五入，不要重排行列；
- 看不清的单元格写 "?"；
- 表注里若说明「粗体为最优」，请写进 notes。"""

_FORMULA_SYS = """你是论文公式解析专家。看这块公式（可能是公式或公式+说明）。
严格输出 JSON：
{"latex":"","reading":"用自然语言念一遍这个公式","symbols":[{"sym":"","meaning":""}],
 "role":"这个公式在方法里起什么作用","confidence":0.0}
硬编码要求：LaTeX 拿不准的部分用 \\text{?} 占位，不要编造符号。"""


# ---------------------------------------------------------------- 选页
def select_pages(parsed: dict, max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    """按已有解析结果给每页打分，挑出最值得送 VLM 的页。

    复用 parsed 里现成的 content_list / figures / tables / citation_links，
    不重新解析 PDF。
    """
    pages: Counter = Counter()
    reasons: dict[int, list[str]] = {}

    def bump(pno: int, score: int, why: str) -> None:
        pages[pno] += score
        reasons.setdefault(pno, []).append(why)

    for blk in parsed.get("content_list") or []:
        pno, typ = blk.get("page"), blk.get("type")
        if typ == "caption_figure":
            bump(pno, 3, "图注")
        elif typ == "caption_table":
            bump(pno, 3, "表注")
        elif typ == "formula":
            pages[pno] += 0
    # 公式密集页
    formula_pages = Counter(b["page"] for b in (parsed.get("content_list") or [])
                            if b.get("type") == "formula")
    for pno, n in formula_pages.items():
        if n >= 2:
            bump(pno, 2, f"公式×{n}")
    # 文字密集页（方法/实验）
    text_pages = Counter(b["page"] for b in (parsed.get("content_list") or [])
                         if b.get("type") == "text")
    for pno, n in text_pages.items():
        if n >= 20:
            bump(pno, 2, f"正文块×{n}")
    # 有实际图裁切的页
    for f in parsed.get("figures") or []:
        bump(f["page"], 3, f"图{f.get('fig_no')}")
    # 被正文引用的图表所在页
    links = parsed.get("citation_links") or {}
    for item in (links.get("figures") or []) + (links.get("tables") or []):
        for pno in item.get("mentioned_on_pages") or []:
            bump(pno, 1, "正文引用")

    ranked = sorted(pages.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked = [kv for kv in ranked if kv[1] > 0]
    if max_pages and max_pages > 0:
        ranked = ranked[:max_pages]          # 显式限制时才截断
    return [{"page": pno, "score": score,
             "reasons": sorted(set(reasons.get(pno, [])))}
            for pno, score in ranked]


def _table_digest(table: dict) -> str:
    rows = table.get("rows") or []
    head = (table.get("header") or " ".join(str(c) for c in (rows[0] if rows else [])))
    return f"{table.get('page')}|{head}|{len(rows)}"


# ---------------------------------------------------------------- 理解
def _collect_tasks(parsed: dict, pdf_path: Path, picked: list[dict]) -> list[dict]:
    """把要送审的图/表/公式整理成工作项（含待送图片字节）。

    裁图必须在这里（主线程）一次性做完：PyMuPDF 的 Document 不是线程安全的，
    而 VLM 调用要并发跑。
    """
    import pymupdf

    fig_by_page: dict[int, list[dict]] = {}
    for f in parsed.get("figures") or []:
        fig_by_page.setdefault(f["page"], []).append(f)
    tab_by_page: dict[int, list[dict]] = {}
    for t in parsed.get("tables") or []:
        tab_by_page.setdefault(t["page"], []).append(t)
    formula_by_page: dict[int, list[dict]] = {}
    for b in parsed.get("content_list") or []:
        if b.get("type") == "formula":
            formula_by_page.setdefault(b["page"], []).append(b)

    tasks: list[dict] = []
    doc = pymupdf.open(pdf_path)
    try:
        for entry in picked:
            pno = entry["page"]
            page = doc[pno - 1]

            for f in fig_by_page.get(pno, []):
                try:
                    data = Path(f["path"]).read_bytes()
                except OSError:
                    continue
                tasks.append({"kind": "figure", "key": str(f.get("fig_no")),
                              "image": data, "system": _FIGURE_SYS,
                              "extra": {"caption": f.get("caption", "")},
                              "instruction": "这是论文里的插图，请按要求输出 JSON。"})

            for t in _cap(tab_by_page.get(pno, []), _max_per_page("table")):
                bbox = t.get("bbox")
                if not bbox:
                    continue
                scale = _SCALE["table"]
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                      clip=pymupdf.Rect(*bbox), alpha=False)
                tasks.append({"kind": "table",
                              "key": str(t.get("table_no") or _table_digest(t)),
                              "image": pix.tobytes("png"), "system": _TABLE_SYS,
                              "extra": {"table_no": t.get("table_no"),
                                        "doc_rows": t.get("rows")},
                              "instruction": "这是论文里的表格，请逐字转录并按要求输出 JSON。"})

            for k, blk in enumerate(
                    _cap(formula_by_page.get(pno, []), _max_per_page("formula"))):
                bbox = blk.get("bbox")
                if not bbox:
                    continue
                scale = _SCALE["formula"]
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                      clip=pymupdf.Rect(*bbox), alpha=False)
                tasks.append({"kind": "formula", "key": f"p{pno}:b{k}",
                              "image": pix.tobytes("png"), "system": _FORMULA_SYS,
                              "extra": {},
                              "instruction": "这是论文里的公式，请解析并按要求输出 JSON。"})
    finally:
        doc.close()
    return tasks


def _run_task(llm, task: dict, refresh: bool):
    try:
        got = llm.vision_json(
            task["image"], task["instruction"], task="figure" if task["kind"] == "figure"
            else task["kind"], prompt_version=PROMPT_VERSION, system=task["system"],
            max_tokens=_TOKENS.get(task["kind"], 1200), refresh=refresh)
        return task, got
    except Exception as e:  # noqa: BLE001
        return task, e


def understand(parsed: dict, llm, pdf_path: Path, *, max_pages: int = MAX_PAGES_DEFAULT,
               refresh: bool = False, model_name: str = "") -> dict:
    """对关键页做 VLM 结构化理解，返回 parsed["vlm"] 结构。

    VLM 调用并发执行（单次调用实测可达 100 秒量级，串行跑一整篇要半小时）。
    """
    from concurrent.futures import ThreadPoolExecutor

    picked = select_pages(parsed, max_pages=max_pages)
    if not picked:
        return {}

    result: dict = {
        "prompt_version": PROMPT_VERSION,
        "model": model_name or getattr(getattr(llm, "cfg", None), "vision_model", ""),
        "pages": {str(p["page"]): {"reasons": p["reasons"], "score": p["score"]}
                  for p in picked},
        "figures": {}, "tables": {}, "formulas": {},
    }

    tasks = _collect_tasks(parsed, pdf_path, picked)
    if not tasks:
        result.update({"n_calls": 0, "n_pages": len(picked)})
        return result

    bucket = {"figure": result["figures"], "table": result["tables"],
              "formula": result["formulas"]}
    n_ok = n_fail = 0
    with ThreadPoolExecutor(max_workers=_VLM_WORKERS) as ex:
        for task, got in ex.map(lambda t: _run_task(llm, t, refresh), tasks):
            if isinstance(got, Exception):
                n_fail += 1
                print(f"[vlm_parse] {task['kind']} {task['key']} 调用失败：{got}")
                continue
            if not got:
                # 解析失败最常见的原因是响应被 max_tokens 截断成半截 JSON；
                # 静默丢弃会让「每次都重新调用」变成看不见的成本。
                n_fail += 1
                print(f"[vlm_parse] {task['kind']} {task['key']} 未返回可解析 JSON"
                      f"（可能是 token 截断），跳过")
                continue
            got.update(task["extra"])
            bucket[task["kind"]][task["key"]] = got
            n_ok += 1

    result["n_calls"] = n_ok
    result["n_failed"] = n_fail
    result["n_pages"] = len(picked)
    print(f"[vlm_parse] 关键页 {len(picked)} 页，送审 {len(tasks)} 项，"
          f"成功 {n_ok}，失败 {n_fail}")
    return result


# ---------------------------------------------------------------- 供 Planner 消费
def digest_for_planner(vlm: dict, max_chars: int = 0) -> str:
    """把 VLM 理解压成一段给 Planner 看的「已核实事实」文本。

    max_chars=0（默认）表示不截断；确需截断时会在末尾注明省略量，
    绝不静默丢弃。
    """
    if not vlm:
        return ""
    lines = []
    for no, fig in (vlm.get("figures") or {}).items():
        pf = fig.get("key_values") or []
        kv = "；".join(f"{x.get('label')}={x.get('value')}"
                       for x in pf[:6] if isinstance(x, dict)) if pf else ""
        conf = fig.get("confidence")
        lines.append(f"- 图{no}（{fig.get('chart_type', '')}"
                     + (f"，置信 {conf}" if isinstance(conf, (int, float)) else "")
                     + f"）：{fig.get('takeaway', '')}"
                     + (f" | 关键读数：{kv}" if kv else "")
                     + (f" | 图注：{fig.get('caption', '')[:80]}" if fig.get("caption") else ""))
    for no, tab in (vlm.get("tables") or {}).items():
        head = " | ".join(str(h) for h in (tab.get("header") or [])[:8])
        lines.append(f"- 表{no}：表头={head}；表注={tab.get('notes', '')[:80]}；"
                     f"结论={tab.get('takeaway', '')[:100]}")
    for k, fo in (vlm.get("formulas") or {}).items():
        lines.append(f"- 公式（{k}）：{fo.get('latex', '')} —— 含义："
                     f"{fo.get('reading', '')[:80]}；作用：{fo.get('role', '')[:60]}")
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        return (text[:max_chars]
                + f"\n（另有 {len(text) - max_chars} 字图表理解被省略）")
    return text
