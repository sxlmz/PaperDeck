# -*- coding: utf-8 -*-
"""router_agent.py：Router Agent 节点 —— 审查反馈的确定性分发中心。

完整版方案的核心枢纽。Critic 产出的 issue 不再一股脑喂给单个 Designer LLM，
而是先由 Router **确定性分类**，再按类别分派给对应子能力：

  missing_data —— 缺数据/缺图/缺表 -> RAG 检索论文原文 chunk -> 证据连同
                  HTML Fixer 直接改写 HTML 源码（补内容/补图/补表格）；
  layout       —— 布局/审美/溢出/太空 -> HTML Fixer 改 HTML/CSS；
  factuality   —— 事实存疑 -> faithfulness 裁决（对照论文原文判定）。

分类判据是确定性规则（见 paper2ppt/router.py），不调 LLM 猜。

节点职责（一次调用处理一轮所有 issue）：
  1. state["issues"] 按 router.route() 分成三桶；
  2. 逐桶执行：
     - missing_data：为每页构建 RAG query -> rag_search 混合检索 ->
       证据 + 该页全部 issue 交给 html_fixer 改 HTML；
     - layout：issue 直接交给 html_fixer 改 HTML；
     - factuality：记录到 state["factuality_issues"]，由 Faithfulness 裁决
       （本轮不阻断，下一轮 critc 重新评估该页）。
  3. 记录 _html_fixed 列表（改了哪些页），供渲染/审查循环判断是否需要
     needs_render=True。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .state import AgentState

#: 每一页的 HTML 源文件（与 html_author 输出约定一致：slides/slideNN.html）
HTML_SLIDE_PATTERN = "slide{no:02d}.html"


def _slide_no(issue: dict) -> int:
    try:
        return int(issue.get("slide") or 0)
    except (TypeError, ValueError):
        return 0


def _load_html(html_dir: Path, no: int) -> Path | None:
    p = html_dir / HTML_SLIDE_PATTERN.format(no=no)
    return p if p.exists() else None


def _build_slide_issues(bucket: list[dict]) -> dict[int, list[dict]]:
    """按页聚合 issue。"""
    by_slide: dict[int, list[dict]] = {}
    for it in bucket:
        no = _slide_no(it)
        if no > 0:
            by_slide.setdefault(no, []).append(it)
    return by_slide


def router_node(state: AgentState) -> dict:
    """Router 分发节点。

    state 需带：
      issues: list[dict]（critic 产出的原始 issue）
      _html_dir: str（HTML 真源目录，html_author 的产物）
      _rag_search: callable（注入的检索函数，默认 paper2ppt.rag_search.search）
    state 新增：
      _html_fixed: list[int]（本轮改过的页号）
      _rag_evidence: dict[int, str]（每页附上的证据文本，供审计）
      factuality_issues: list[dict]（待 faithfulness 裁决的 issue）
      needs_render: bool（是否有 HTML 被改动，需重渲染）
    """
    from .. import router as router_mod
    from ..html_fixer import fix_slide_html

    issues = list(state.get("issues") or [])
    logs = list(state.get("logs") or [])
    # --from html_critic 等入口不会回填 _html_dir（它是 html_author 的输出路径）；
    # 兜底到与 html_critic 相同的 HTML 真源目录，否则 Router 会拿当前目录找
    # slideNN.html 而全部跳过修复（2026-10-04 实测 6 条 issue 全跳过）。
    from ..config import WORKSPACE as _WS
    html_dir = Path(state.get("_html_dir") or (_WS / "html_source"))
    llm = state.get("_llm")
    if not issues or not html_dir.exists() or llm is None:
        return {"_html_fixed": [], "needs_render": False,
                "factuality_issues": [], "logs": logs}

    # 1. 确定性分类
    buckets = router_mod.route(issues)
    logs.append(f"[RouterAgent] 分发：{router_mod.summarize(buckets)}")
    print(f"[RouterAgent] 分发：{router_mod.summarize(buckets)}")

    # 2. 缺数据 -> RAG 检索（每页构建 query，混合检索论文原文）
    #
    # 检索函数的默认实现**在这里惰性取**，而不是靠图里注入：
    # 原设计有个 `_inject_rag` 节点负责写 state["_rag_search"]，但那个节点
    # 没有任何边连到它（不在 STAGES/HTML_STAGES，也不在任何 add_edge），
    # 于是 state 里永远没有它，这个分支从来没执行过 —— RAG 建好了却是死代码。
    # 惰性默认实现在任何 --from 入口下都成立，还少一个图节点。
    rag_evidence: dict[int, str] = {}

    def _default_search():
        from ..rag_search import search      # 惰性导入：避免 import 期依赖 numpy/jieba
        return search

    rag_search: Callable | None = state.get("_rag_search") or _default_search()
    if buckets.get("missing_data") and rag_search is not None:
        from ..rag_search import format_evidence
        from .html_fixer_bridge import build_queries, collect_evidence

        by_slide = _build_slide_issues(buckets["missing_data"])
        for no, its in sorted(by_slide.items()):
            queries = build_queries(its)
            evidence = collect_evidence(rag_search, queries, top_k=3)
            if evidence:
                rag_evidence[no] = format_evidence(evidence, max_chars=3000)
                msg = (f"[RouterAgent] 第{no}页缺数据，RAG 检索到 "
                       f"{len(evidence)} 条证据")
            else:
                # 降级要留痕：缺数据却没检索到，通常是索引缺失或 embedding 不可用
                msg = (f"[RouterAgent] 第{no}页缺数据但 RAG 无结果"
                       f"（索引缺失 / embedding 不可用）")
            logs.append(msg)
            print(msg)

    # 3. 按页合并两类 issue（缺数据证据附上），交给 HTML Fixer 改源码
    merged: dict[int, list[dict]] = {}
    for bucket_name in ("missing_data", "layout"):
        for no, its in _build_slide_issues(buckets.get(bucket_name) or []).items():
            merged.setdefault(no, []).extend(its)

    fixed_pages: list[int] = []
    # 逐页修复改为**并发**（用户明确要求，不串行；每页是独立 LLM 调用）
    from concurrent.futures import ThreadPoolExecutor

    # 模板页（cover/toc/chapterNN/closing）由确定性引擎渲染，VLM 对它们的
    # 误报/建议没有操作价值（且 Fixer 修补会破坏模板一致性）——直接跳过。
    _fixed_ids: set[str] = set()
    try:
        import json as _json
        _mf = html_dir / "manifest.json"
        if _mf.exists():
            for _pg in (_json.loads(_mf.read_text(encoding="utf-8")) or {}).get("pages") or []:
                _sid = str(_pg.get("slide_id") or "")
                if _sid in ("cover", "toc", "closing") or _sid.startswith("chapter"):
                    _fixed_ids.add(str(_pg.get("page")).zfill(2))
    except Exception:  # noqa: BLE001
        _fixed_ids = set()

    def _fix_one(no: int, its: list[dict]) -> tuple[int, dict]:
        if f"{no:02d}" in _fixed_ids:
            return no, {"ok": False, "skip": True,
                         "error": "模板固定页（cover/toc/chapter/closing），"
                                  "由确定性引擎渲染，跳过 Fixer"}
        html_path = _load_html(html_dir, no)
        if html_path is None:
            return no, {"ok": False, "skip": True,
                         "error": f"无 HTML 源（{HTML_SLIDE_PATTERN.format(no=no)}）"}
        # VLM 报告的模板 ID（任一 issue 上可能带 template 字段），
        # 作为 hint 传给 Fixer 注入模板骨架；缺省由 Fixer 从 HTML 特征判断
        template_hint = ""
        for it in its:
            if isinstance(it, dict) and it.get("template"):
                template_hint = str(it["template"])
                break
        res = fix_slide_html(llm, html_path, its,
                             rag_evidence=rag_evidence.get(no, ""),
                             page_no=no, title=_slide_title(state, no),
                             template_hint=template_hint)
        return no, res

    tasks = sorted(merged.items())
    with ThreadPoolExecutor(max_workers=min(6, len(tasks) or 1)) as ex:
        futures = {ex.submit(_fix_one, no, its): no for no, its in tasks}
        for fut in futures:
            no, res = fut.result()
            if res.get("skip"):
                logs.append(f"[RouterAgent] 第{no}页{res['error']}，跳过")
                continue
            if res.get("ok") and res.get("changed"):
                fixed_pages.append(no)
                logs.append(f"[RouterAgent] 第{no}页 HTML 已修复"
                            f"（{res['old_len']}->{res['new_len']} 字符"
                            + (f"，移除 {len(res['removed'])} 项风险" if res["removed"] else "")
                            + "）")
            elif res.get("ok"):
                logs.append(f"[RouterAgent] 第{no}页 HTML 未变化（LLM 判定无需修改）")
            else:
                logs.append(f"[RouterAgent] 第{no}页修复失败: {res.get('error', '?')}")

    # 4. 事实存疑 issue 留给 Faithfulness 裁决（不阻断本轮渲染）
    factuality = buckets.get("factuality") or []

    return {
        "_html_fixed": fixed_pages,
        "_rag_evidence": rag_evidence,
        "factuality_issues": factuality,
        "needs_render": bool(fixed_pages),
        "logs": logs,
    }


def _slide_title(state: AgentState, no: int) -> str:
    """从 design/outline 里找该页标题（HTML 页序与 design 页序一致时可用）。"""
    for key in ("design", "outline"):
        deck = state.get(key) or {}
        slides = deck.get("slides") or []
        if 1 <= no <= len(slides):
            return str(slides[no - 1].get("title") or "")
    return ""
