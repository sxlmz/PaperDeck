# -*- coding: utf-8 -*-
"""layout_checker.py：确定性浏览器布局检查（HTML 审查稿 -> issues）。

思路来自 PPT-master 的 Visual Review Rubric，但针对 HTML 渲染稿实现：
用 Playwright 打开每页 HTML，注入 JS 读取布局树（getBoundingClientRect /
scrollHeight / computed style），**零 LLM、零 token** 地判定排版问题。

**2026-10-04 收窄（用户拍板）**：整页审查只查一类问题——**文字溢出**
（H2：字数太多导致内容溢出容器/卡片、被裁剪）。同时为每条溢出问题
**定位溢出源文字**（哪个 data-text 元素溢出、文字内容是什么），
反馈给主 LLM（HTML Fixer）让它压缩该段文字或拆分为两条要点。

其他规则（越界/重叠/碰撞/对比度/太空旷/对齐/网格）不再作为审查问题
输出：布局由模板确定性渲染兜底，内容充实度由 content judge 兜底。

输出与 critic.py 的 issue 结构兼容：
  {"slide": int, "type": "overflow", "severity": "hard",
   "desc": str, "rule": "H2", "culprit": "溢出源文字"}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import WORKSPACE
from .html_preview import SLIDE_H_PX, SLIDE_W_PX

#: 与 renderer/critic 的 issue type 对齐的映射
_RULE_TO_TYPE = {
    "H2": "overflow",
}

#: 检查脚本（注入每页执行，返回 issue 列表）。
#: 只做 H2 文本溢出检测 + 溢出源定位（用户拍板 2026-10-04）。
_CHECK_JS = r"""
() => {
  const W = %W%, H = %H%;
  const TOL = 2;  // px 容差
  const issues = [];

  function rect(el) {
    const r = el.getBoundingClientRect();
    return {left: r.left, top: r.top, right: r.right, bottom: r.bottom,
            width: r.width, height: r.height};
  }
  function boxIn(el) {  // px -> 英寸（与 design.json layout 坐标同口径，1in=96px）
    const r = rect(el);
    return [+(r.left / 96).toFixed(3), +(r.top / 96).toFixed(3),
            +((r.right - r.left) / 96).toFixed(3), +((r.bottom - r.top) / 96).toFixed(3)];
  }

  // ---- H2 文本溢出（唯一检查项）：data-box 容器内容超出自身 ----
  // 只查含文本的容器（纯图容器不算）。溢出时定位"溢出源"：溢出量最大的
  // data-text 元素（回退取文本最长的），把那段文字截出来喂给主 LLM，
  // 让它压缩该段或拆成两条要点——否则 LLM 只知道"哪页溢出了"，
  // 不知道"哪段文字太多导致的溢出"。
  for (const box of document.querySelectorAll('[data-box]')) {
    if (!box.querySelector('[data-text="1"]')) continue;  // 纯图容器不查
    const overV = box.scrollHeight > box.clientHeight + TOL;
    const overH = box.scrollWidth > box.clientWidth + TOL;
    if (!overV && !overH) continue;

    let culprit = null;
    let bestOv = 0, bestLen = 0;
    for (const t of box.querySelectorAll('[data-text="1"]')) {
      const ov = (t.scrollHeight - t.clientHeight) + (t.scrollWidth - t.clientWidth);
      const len = (t.textContent || '').trim().length;
      if (ov > bestOv) { bestOv = ov; culprit = t; }
      if (len > bestLen) bestLen = len;
    }
    if (!culprit) {
      // 容器整体溢出但单个文本未溢出（行高/内边距挤压）：取文本最长的元素
      for (const t of box.querySelectorAll('[data-text="1"]')) {
        const len = (t.textContent || '').trim().length;
        if (len === bestLen && len > 0) { culprit = t; break; }
      }
    }
    const raw = (culprit && culprit.textContent) ? culprit.textContent.trim() : '';
    const snippet = raw.slice(0, 80);
    const dim = overV
      ? `内容高 ${box.scrollHeight}px > 容器高 ${box.clientHeight}px`
      : `内容宽 ${box.scrollWidth}px > 容器宽 ${box.clientWidth}px`;
    issues.push({
      rule: 'H2', severity: 'hard',
      selector: box.getAttribute('data-box'),
      box_in: boxIn(box),
      culprit: snippet,
      desc: `文本溢出（${dim}）；溢出源文字："${snippet}${raw.length > 80 ? '…' : ''}"。` +
            `请压缩该段文字，或拆分为两条要点`,
    });
  }

  return issues;
}
"""


def check_html_page(html_path: Path, channel: str = "msedge") -> list[dict[str, Any]]:
    """打开单页 HTML，注入 JS 做确定性检查，返回 issue 列表。"""
    from playwright.sync_api import sync_playwright

    script = _CHECK_JS.replace("%W%", str(SLIDE_W_PX)).replace("%H%", str(SLIDE_H_PX))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=True)
        page = browser.new_page(viewport={"width": SLIDE_W_PX, "height": SLIDE_H_PX})
        try:
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(100)
            result = page.evaluate(script)
        finally:
            browser.close()
    return result if isinstance(result, list) else []


def check_deck(html_files: list[Path] | None = None,
               out_dir: Path = WORKSPACE / "html_preview",
               channel: str = "msedge") -> dict[int, list[dict[str, Any]]]:
    """检查整份 HTML 审查稿，返回 {slide_no: [issue, ...]}。"""
    if html_files is None:
        html_files = sorted(out_dir.glob("slide_*.html"))
    out: dict[int, list[dict[str, Any]]] = {}
    for html_path in html_files:
        # 文件名可能是 slide05.html（流水线实际产物）或 slide_05.html（旧约定）。
        # 旧实现只按 "_" 切分，遇 slide05 会 ValueError 然后落到 no=0——
        # 结果**所有页的 issue 全塞进 key 0**，既定位不到页，还会互相覆盖。
        import re as _re
        m = _re.search(r"(\d+)", html_path.stem)
        no = int(m.group(1)) if m else 0
        issues = check_html_page(html_path, channel)
        for it in issues:
            it["type"] = _RULE_TO_TYPE.get(it.get("rule"), "layout")
        out[no] = issues
    return out


def to_critic_issues(deck_issues: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """把 {slide: [issues]} 转成 critic.py 兼容的 issue 列表。

    box_in 已由 JS 侧换算成英寸（1in=96px），与 design.json 的 layout
    坐标同口径，LLM 修订时可直接换算到 set_geometry 参数。
    """
    flat = []
    for slide_no, issues in sorted(deck_issues.items()):
        for it in issues:
            entry = {
                "slide": slide_no,
                "type": it.get("type") or _RULE_TO_TYPE.get(it.get("rule"), "layout"),
                "severity": it.get("severity", "soft"),
                "rule": it.get("rule", ""),
                "desc": it.get("desc", ""),
                "_html": True,   # 标记来源：HTML 审查稿
            }
            if it.get("culprit"):
                entry["culprit"] = str(it["culprit"])  # 溢出源文字（Fixer 定位用）
            box = it.get("box_in")
            if isinstance(box, (list, tuple)) and len(box) == 4:
                entry["box_in"] = [float(v) for v in box]
            flat.append(entry)
    return flat


if __name__ == "__main__":
    import sys
    from .config import DESIGN_JSON
    from .html_preview import build_html

    design_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DESIGN_JSON
    from .models import load_json
    design = load_json(design_path)
    htmls = build_html(design)
    result = check_deck(htmls)
    print(json.dumps({str(k): v for k, v in result.items()}, ensure_ascii=False, indent=2))
    n = sum(len(v) for v in result.values())
    print(f"[layout_checker] {len(result)} 页，共 {n} 个问题")
