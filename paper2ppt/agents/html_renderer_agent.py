# -*- coding: utf-8 -*-
"""html_renderer_agent.py：HTML 真源的交付渲染节点。

把 html_source/ 下的每页 HTML 源码转换为最终 .pptx：
  1. html2pptx.convert_html_to_pptx（npm dom-to-pptx 浏览器直出 +
     zip 级合并，无 python-pptx）；
  2. 生成版本快照（可回溯）。

注意：dom-to-pptx 由浏览器按 computed style 直出，字体度量与浏览器一致，
不再存在旧 python-pptx 重建时的字体漂移问题。
"""
from __future__ import annotations

from pathlib import Path

from .. import html2pptx
from ..config import WORKSPACE
from .state import AgentState

_HTML_DIR = WORKSPACE / "html_source"
_SNAP_DIR = WORKSPACE / "snapshots"


def _snapshot(pptx: Path) -> Path:
    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    n = len(list(_SNAP_DIR.glob("html_v*.pptx"))) + 1
    dst = _SNAP_DIR / f"html_v{n:02d}.pptx"
    dst.write_bytes(pptx.read_bytes())
    return dst


def html_renderer_node(state: AgentState) -> dict:
    html_dir = Path(state.get("_html_dir") or _HTML_DIR)
    out = Path(state.get("out_pptx") or "")
    if not out:
        from ..config import DEFAULT_OUT
        out = Path(DEFAULT_OUT)
    logs = list(state.get("logs") or [])

    # 页面清单以 manifest.json 为准，**不要 glob 目录**：
    # html_source/ 是同一次运行的产物目录，上一轮遗留的 slide19.html… 会被
    # glob 一起捡走。实测：manifest 记 18 页、目录里躺了 33 个文件，
    # 于是渲染出 31 页的 pptx（v24 那个"31 页"至少有一部分是这么来的）。
    manifest = html_dir / "manifest.json"
    html_files: list[Path] = []
    if manifest.exists():
        try:
            import json
            pages = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("pages") or []
            html_files = [html_dir / f"slide{int(p['page']):02d}.html"
                          for p in pages if p.get("page")]
        except Exception as e:  # noqa: BLE001
            logs.append(f"[HTMLRenderer] manifest 读取失败，回退 glob：{e}")
    if not html_files:
        html_files = sorted(html_dir.glob("slide*.html"))

    missing = [p.name for p in html_files if not p.exists()]
    if missing:
        logs.append(f"[HTMLRenderer] manifest 里这些页没有对应文件：{missing[:6]}"
                    f"（可能是上一轮残留的 manifest）")

    # 目录里有 manifest 之外的 slide*.html —— 明确报出来，绝不静默多渲染
    strays = sorted(p.name for p in html_dir.glob("slide*.html")
                    if p not in html_files)
    if strays:
        msg = (f"[HTMLRenderer] 忽略 {len(strays)} 个不在 manifest 里的残留文件："
               f"{strays[:6]}{' …' if len(strays) > 6 else ''}")
        logs.append(msg)
        print(msg)

    html_files = [p for p in html_files if p.exists()]
    if not html_files:
        logs.append("[HTMLRenderer] 无 HTML 源文件，跳过渲染")
        return {"pptx_path": "", "logs": logs}

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = html2pptx.convert_html_to_pptx(html_files, out)
    except Exception as e:  # noqa: BLE001
        logs.append(f"[HTMLRenderer] 转换失败: {e}")
        print(f"[HTMLRenderer] 转换失败: {e}")
        return {"pptx_path": "", "logs": logs}

    snap = _snapshot(out)
    logs.append(f"[HTMLRenderer] html2pptx 转换完成: {out.name}"
                f"（{res['pages']} 页）；快照 {snap.name}")
    print(f"[HTMLRenderer] html2pptx 转换完成: {out.name}（{res['pages']} 页）")

    # 透传 HTML 目录：Router/Fixer 需要知道 HTML 真源在哪（--from html_renderer
    # 续跑时 state 里没有 _html_dir，会导致 Router 全部“无 HTML 源”跳过修复）
    return {"pptx_path": str(out), "_html_dir": str(html_dir), "logs": logs}
