# -*- coding: utf-8 -*-
"""html2pptx.py：HTML 真源 -> PPTX 转换器（dom-to-pptx 路线，无 python-pptx）。

2026-10-03 起转换层整体切换：放弃 python-pptx 重建（可编辑性承诺解除），
改走 npm dom-to-pptx@2.1.2（浏览器内按 computed style 直出原生形状/表格）：

  1. 把本次要转换的 slideNN.html（含 assets/ 图片目录）复制到临时目录；
  2. 调 node tools/dom2pptx/batch_export.mjs 逐页转出单页 pptx
     （Edge headless，1280x720 视口 -> 10"x5.625" 16:9 幻灯片）；
  3. 调 python tools/dom2pptx/merge_pptx.py 按 OPC zip 级合并成多页 pptx
     （media 重命名、rels/sldIdLst/[Content_Types] 重写）。

表格样式等由浏览器按模板 CSS 渲染（浅灰表头+网格线+蓝色斜体数值），
不再经过 python-pptx 默认主题，三线表问题从根上消失。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOM2PPTX_DIR = PROJECT_ROOT / "tools" / "dom2pptx"
BATCH_JS = DOM2PPTX_DIR / "batch_export.mjs"
MERGE_PY = DOM2PPTX_DIR / "merge_pptx.py"

SLIDE_W, SLIDE_H = 1280, 720  # HTML 画布（px），与模板一致


def _find_node() -> str:
    """定位 node（优先系统 PATH，常见安装位置兜底）。"""
    for cand in ("node", "node.exe"):
        p = shutil.which(cand)
        if p:
            return p
    for p in (r"C:\Program Files\nodejs\node.exe",
              r"C:\Program Files (x86)\nodejs\node.exe"):
        if Path(p).exists():
            return p
    raise FileNotFoundError("找不到 node，请先安装 Node.js")


def convert_html_to_pptx(html_files: list[Path], out: Path,
                         node: str | None = None) -> dict:
    """把一组 HTML 页转换为多页 PPTX。

    参数与旧 python-pptx 版保持一致（调用方 html_renderer_agent 无需改动）：
      html_files: 本页清单（manifest 过滤后的 slideNN.html 列表）
      out:        输出 .pptx 路径
    返回 {"pages": N}。
    """
    html_files = [Path(p) for p in html_files if Path(p).exists()]
    if not html_files:
        raise ValueError("没有可转换的 HTML 文件")

    node_exe = node or _find_node()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="p2p-dom2pptx-") as td:
        tmp = Path(td)
        html_dir = tmp / "html"
        pages_dir = tmp / "pages"
        html_dir.mkdir()
        pages_dir.mkdir()

        # 复制页面文件（保留 slideNN.html 命名）+ 图片资产目录
        src_root = html_files[0].parent
        for p in html_files:
            shutil.copy2(p, html_dir / p.name)
        assets = src_root / "assets"
        if assets.exists():
            shutil.copytree(assets, html_dir / "assets", dirs_exist_ok=True)

        # 1) 逐页 dom-to-pptx 转换（浏览器内直出）
        run = subprocess.run(
            [node_exe, str(BATCH_JS), str(html_dir), str(pages_dir)],
            capture_output=True, text=True, encoding="utf-8", timeout=900)
        if run.returncode != 0:
            tail = "\n".join((run.stdout or "").splitlines()[-8:])
            raise RuntimeError(f"dom-to-pptx 批量转换失败：{tail}")

        n_pages = len(list(pages_dir.glob("slide*.pptx")))
        if n_pages != len(html_files):
            raise RuntimeError(
                f"页数不一致：输入 {len(html_files)}，转换出 {n_pages}")

        # 2) zip 级合并为多页 pptx（纯 zipfile+lxml，无 python-pptx）
        merge = subprocess.run(
            [sys.executable, str(MERGE_PY), str(pages_dir), str(out)],
            capture_output=True, text=True, encoding="utf-8", timeout=300)
        if merge.returncode != 0:
            tail = "\n".join((merge.stdout or "").splitlines()[-8:])
            err = "\n".join((merge.stderr or "").splitlines()[-8:])
            raise RuntimeError(f"合并失败：{tail}\n{err}")

    if not out.exists():
        raise RuntimeError(f"转换产物不存在：{out}")
    return {"pages": n_pages, "pptx_path": str(out)}


if __name__ == "__main__":
    files = [Path(a) for a in sys.argv[1:-1]]
    out = Path(sys.argv[-1])
    import json
    print(json.dumps(convert_html_to_pptx(files, out), ensure_ascii=False, indent=2))
