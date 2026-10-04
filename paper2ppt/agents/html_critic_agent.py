# -*- coding: utf-8 -*-
"""html_critic_agent.py：HTML 真源的审查闭环节点。

路径 A 的 critic_node 审的是 design.json 派生出的 HTML 审查稿；完整版方案
的 HTML 是**真源**，所以审查直接作用于 html_source/ 下的页面文件：
  1. 渲染：每页 HTML -> PNG（sandbox 路由拦截，只放行本地资源）；
  2. 确定性检查：layout_checker 注入浏览器读布局树（零 token）；
  3. VLM 看图（qwen3.7-flash）：看图诊断；
  4. 结果写入 state["issues"]，交给 Router 分发。

**2026-10-04 审查范围收窄（用户拍板）**：layout_checker 与 VLM 都**只查
文字溢出**（字数太多导致字体溢出容器/卡片/被裁剪），并且必须**定位溢出源
文字**——告诉主 LLM 是哪段内容太多导致的溢出，让 LLM 压缩该段文字或
拆分为两条要点。内容充实度/书面化/相关性由生成阶段的内容 Judge 把关，
布局与模板一致性由确定性渲染保证，不再作为整页审查项。

迭代上限与单调守卫沿用 critic_node 的纪律（用户明确要求：最多循环评审
三次，到阈值直接输出，防止死循环）：
  - max_critic_rounds（默认 3）封顶；
  - 每轮结束后若问题数 >= 历史最优，回滚到最优 HTML 版本并停止；
  - HTML 版本备份：每次渲染前把当前 HTML 快照到 workspace/html_source/.bak/，
    回滚即恢复该页 HTML 文件。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import issues as issues_mod
from ..config import WORKSPACE
from ..coverage import Coverage
from .state import AgentState

_HTML_DIR = WORKSPACE / "html_source"
_BAK_DIR = _HTML_DIR / ".bak"
_PNG_DIR = WORKSPACE / "html_render"


def _backup_html(html_dir: Path, round_no: int) -> None:
    """把当前 HTML 快照到 .bak/round{N}/（回滚用）。"""
    dst = _BAK_DIR / f"round{round_no}"
    dst.mkdir(parents=True, exist_ok=True)
    for p in html_dir.glob("slide*.html"):
        shutil.copy2(p, dst / p.name)


def _restore_html(html_dir: Path, round_no: int) -> list[str]:
    """回滚到某轮备份。返回恢复的文件名列表。"""
    src = _BAK_DIR / f"round{round_no}"
    if not src.exists():
        return []
    restored = []
    for p in src.glob("slide*.html"):
        shutil.copy2(p, html_dir / p.name)
        restored.append(p.name)
    return restored


def _html_files(html_dir: Path) -> list[Path]:
    files = sorted(html_dir.glob("slide*.html"))
    return files


def _run_vlm_page(llm, png: Path, context: str = "") -> dict:
    from ..vlm import _run_vlm_slide
    return _run_vlm_slide(llm, png, context)


def _html_review_issues(html_files: list[Path], png_dir: Path) -> tuple[list[dict], list[Path]]:
    """渲染 + 确定性检查。返回 (issues, pngs)。"""
    from ..html_preview import render_pngs
    from ..layout_checker import check_deck, to_critic_issues

    pngs = render_pngs(html_files, png_dir)
    det = to_critic_issues(check_deck(html_files))
    return det, pngs


def html_critic_node(state: AgentState) -> dict:
    import os as _os
    llm = state.get("_llm")
    logs = list(state.get("logs") or [])
    # 完全跳过审查（--no-critic / P2P_SKIP_CRITIC=1）：不渲染 PNG、
    # 不跑 layout_checker、不调 VLM，直接出终稿。用于"先看模板效果"。
    if _os.environ.get("P2P_SKIP_CRITIC", "0") == "1":
        logs.append("[HTMLCritic] P2P_SKIP_CRITIC=1：跳过全部审查，直接出终稿")
        return {"issues": [], "needs_render": False, "logs": logs}
    html_dir = Path(state.get("_html_dir") or _HTML_DIR)
    if not html_dir.exists() or not _html_files(html_dir):
        logs.append("[HTMLCritic] 无 HTML 源文件，跳过审查")
        return {"issues": [], "needs_render": False, "logs": logs}

    rounds = int(state.get("critic_rounds", 0)) + 1
    max_rounds = int(state.get("max_critic_rounds", 3))
    html_files = _html_files(html_dir)
    png_dir = _PNG_DIR

    _backup_html(html_dir, rounds)
    det, pngs = _html_review_issues(html_files, png_dir)
    issues = list(det)
    logs.append(f"[HTMLCritic] 第{rounds}轮确定性检查 {len(det)} 问题"
                f"（hard {sum(1 for i in det if i.get('severity') == 'hard')}）")

    ctx = {}
    parsed = state.get("parsed") or {}
    pages = {p.get("page_no"): (p.get("text") or "")
             for p in (parsed.get("pages") or [])}
    manifest = {}
    mf = html_dir / "manifest.json"
    if mf.exists():
        import json as _json
        try:
            manifest = _json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    for i, s in enumerate(manifest.get("pages") or [], 1):
        chunks = [f"本页标题：{s.get('title','')}"]
        for pno in range(1, max(pages) + 1 if pages else 1):
            if pno in pages:
                chunks.append(f"[论文第{pno}页]\n{pages[pno][:600]}")
        ctx[i] = "\n".join(chunks)

    advices: list[str] = []
    if llm is not None and getattr(llm, "cfg", None) and getattr(llm.cfg, "vision_model", None) and pngs:
        import re as _re
        from concurrent.futures import ThreadPoolExecutor

        def _page_no(p: Path) -> int:
            m = _re.search(r"(\d+)", p.stem)
            return int(m.group(1)) if m else 0

        # VLM 每页并行（用户明确要求，不串行）
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(
                lambda p: (p, _run_vlm_page(llm, p, ctx.get(_page_no(p), ""))), pngs))
        for png, res in results:
            if not isinstance(res, dict):
                continue
            slide_no = _page_no(png)
            page_template = str(res.get("template") or "")
            for it in (res.get("issues") or []):
                if isinstance(it, dict) and it.get("desc"):
                    entry = {
                        "slide": slide_no,
                        "type": "vlm:" + str(it.get("type", "review")),
                        "desc": str(it["desc"]),
                        "severity": str(it.get("severity", "")),
                    }
                    if page_template:
                        entry["template"] = page_template
                    issues.append(entry)
            adv = str(res.get("advice", "")).strip()
            if adv and adv.lower() not in ("ok", "none"):
                advices.append(f"第{slide_no}页: {adv}")
        logs.append(f"[HTMLCritic] VLM 并行看图 {len(pngs)} 页，共 {len(issues)} 问题")

    cov = Coverage(state.get("coverage"))
    cov.record("html_critic/审查覆盖", total=len(html_files), sent=len(pngs),
               unit="页", detail="全量渲染审查")

    # 单调守卫 + 迭代上限（用户明确要求，防止死循环）
    prev_issues = state.get("_prev_issues") or []
    if prev_issues:
        print(issues_mod.summarize(prev_issues, issues, rounds))
    best_n = int(state.get("_best_issue_count", 10 ** 9))
    finalized = bool(state.get("_finalized"))

    if issues and not finalized and rounds < max_rounds:
        if len(issues) >= best_n:
            # 改完问题反而更多：回滚到最优 HTML 版本，停止审查
            restored = _restore_html(html_dir, rounds - 1 if rounds > 1 else 1) \
                if _BAK_DIR.exists() else []
            report = (f"第 {rounds} 轮问题数未下降（{len(issues)} >= 最优 {best_n}），"
                      f"回滚到最优版本并停止审查" + (f"，恢复 {len(restored)} 页" if restored else "") + "。")
            logs.append(f"[HTMLCritic] {report}")
            return {"critic_rounds": rounds, "issues": issues,
                    "needs_render": False, "_finalized": True,
                    "critic_report": report, "logs": logs,
                    "coverage": cov.records, "_prev_issues": issues}

        best_n = len(issues)
        report = (f"第 {rounds}/{max_rounds} 轮：{len(issues)} 问题（当前最优），"
                  f"交 Router 分发修复后重渲染。")
        logs.append(f"[HTMLCritic] {report}")
        return {"critic_rounds": rounds, "issues": issues, "needs_render": True,
                "_best_issue_count": best_n, "_finalized": False,
                "critic_report": report, "logs": logs,
                "coverage": cov.records, "_prev_issues": issues}

    if issues and not finalized and _BAK_DIR.exists() and len(issues) > best_n:
        restored = _restore_html(html_dir, rounds)
        report = (f"第 {rounds} 轮已达轮数上限，但问题数（{len(issues)}）多于历史最优"
                  f"（{best_n}），回滚到最优版本出终稿。")
        logs.append(f"[HTMLCritic] {report}")
        return {"critic_rounds": rounds, "issues": issues,
                "needs_render": False, "_finalized": True,
                "critic_report": report, "logs": logs,
                "coverage": cov.records, "_prev_issues": issues}

    status = f"HTML 审查结束（{rounds} 轮）：剩余 {len(issues)} 个问题"
    if advices:
        status += "\n[VLM 美化建议]\n  - " + "\n  - ".join(advices[:8])
    if issues:
        status += "\n" + "\n".join(
            f"  - 第 {it.get('slide', '?')} 页 [{it.get('type')}] {it.get('desc')}"
            for it in issues)
    logs.append(f"[HTMLCritic] 审查结束，剩余 {len(issues)} 个问题")
    return {"critic_rounds": rounds, "issues": issues, "needs_render": False,
            "critic_report": status, "logs": logs,
            "coverage": cov.records, "_prev_issues": issues}
