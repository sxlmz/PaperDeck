# -*- coding: utf-8 -*-
"""sandbox.py：HTML 真源的沙箱策略（LLM 写 HTML/CSS 代码生成的安全闸门）。

目标：LLM 自由写 HTML 是「代码生成」，必须隔离三类风险：
  1. **外链资源**（http(s) 图片/CSS/字体）——渲染时拖慢、失效、泄露内网、
     或审查时与本地不一致。净化阶段一律删除，本地有同名资源则替换。
  2. **脚本注入**（<script> / on* 事件 / javascript: URL / <iframe>）——
     渲染沙箱里执行任意 JS 等于代码执行，全删。
  3. **越界路径**（绝对路径 / 上级目录 ../ / file://）——只允许相对路径。

三个入口：
  - sanitize_html(html, assets_dir)：静态净化（写盘前）。
  - render_html(html_path, ...)：Playwright 渲染 + **路由拦截**（只放行
    workspace 内本地资源，其余 abort），仿 PPTAgent core.open_slide 的
    route 拦截，但本实现不依赖其缺失的 deeppresenter 包。
  - render_with_sandbox(html_path)：净化后渲染 PNG + 同时返回阻断清单。
"""
from __future__ import annotations

import re
from pathlib import Path

#: 外链资源标签（http/https 的 img/source/video/audio/link/script）：无本地副本时
#: 整标签删除（仅删属性会残留 alt="x" 之类的碎标签）
_EXTERNAL_TAG = re.compile(
    r'<(img|source|video|audio|link|script)\b[^>]*?(?:https?:)?//[^"\'>\s]+[^>]*>',
    re.IGNORECASE)

#: 外链资源（http/https 且本地无同名文件时，由回调决定保留或删除）
_EXTERNAL_SRC = re.compile(
    r'(<(?:img|source|video|audio|link|script)\b[^>]*?\b(?:src|href)\s*=\s*'
    r'["\'])(https?:)?//[^"\']+(["\'])', re.IGNORECASE)

#: 内联事件处理器（onclick/onload/...）
_INLINE_EVENT = re.compile(r'\son[a-z]+\s*=\s*"[^"]*"|\son[a-z]+\s*=\s*\'[^\']*\'',
                           re.IGNORECASE)

#: javascript: / data: 危险 URL（整标签删除，防碎标签残留）
_DANGEROUS_TAG = re.compile(
    r'<[a-z]+\b[^>]*?(?:href|src|action)\s*=\s*["\']\s*'
    r'(?:javascript|vbscript|data:text/html)[:"\'][^>]*>',
    re.IGNORECASE | re.DOTALL)
_DANGEROUS_URL = re.compile(
    r'(href|src|action)\s*=\s*["\']\s*(?:javascript|vbscript|data:text/html)[:"\']',
    re.IGNORECASE)

#: 脚本/iframe/object/embed 标签（整块删除）
_SCRIPT_BLOCK = re.compile(
    r'<(script|iframe|object|embed)\b[^>]*>.*?</\1\s*>|<(script|iframe|object|embed)\b[^>]*/>',
    re.IGNORECASE | re.DOTALL)

#: 越界路径：绝对路径 / 上级目录 / file://（整标签删除）
_TRAVERSAL_TAG = re.compile(
    r'<[a-z]+\b[^>]*?(?:src|href)\s*=\s*["\']'
    r'(?:file:|[A-Za-z]:[\\/]|/[^"\']*[\\/]|\.\./)[^"\']*["\'][^>]*>',
    re.IGNORECASE | re.DOTALL)
_TRAVERSAL = re.compile(r'(?:src|href)\s*=\s*["\'](?:file:|[A-Za-z]:[\\/]|/|\.\./)[^"\']*["\']',
                        re.IGNORECASE)

#: 允许的本地资源扩展名
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".woff",
                ".woff2", ".ttf", ".otf"}


def sanitize_html(html: str, assets_dir: Path | None = None) -> dict:
    """净化 HTML 源码。返回 {"html": 净化后文本, "removed": [描述, ...]}。

    assets_dir：本地资源目录；外链图片若本地存在同名文件则改写为相对路径，
    否则整段删除（保留一个占位注释，提示 LLM 这是被沙箱拦掉的）。
    """
    removed: list[str] = []
    out = html or ""

    # 1. 脚本/iframe/object/embed 整块删除
    def _drop_script(m: re.Match) -> str:
        tag = (m.group(1) or m.group(2) or "").lower()
        removed.append(f"<{tag}> 块被沙箱移除（脚本/内嵌风险）")
        return ""

    out, n = _SCRIPT_BLOCK.subn(_drop_script, out)
    if n:
        removed.append(f"移除 {n} 个脚本/内嵌块")

    # 2. 内联事件
    n = len(_INLINE_EVENT.findall(out))
    if n:
        out = _INLINE_EVENT.sub("", out)
        removed.append(f"移除 {n} 个内联事件处理器（on*）")

    # 3. javascript:/data:text/html URL（整标签删除）
    n = len(_DANGEROUS_TAG.findall(out))
    if n:
        out = _DANGEROUS_TAG.sub("", out)
        removed.append(f"移除 {n} 个危险 URL 标签")
    n = len(_DANGEROUS_URL.findall(out))
    if n:
        out = _DANGEROUS_URL.sub("", out)
        removed.append(f"清理 {n} 个残留危险 URL 属性")

    # 4. 外链资源：本地有同名文件则改写为相对路径，否则整标签删除
    def _ext_tag(m: re.Match) -> str:
        tag_text = m.group(0)
        src_m = re.search(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', tag_text,
                          re.IGNORECASE)
        if src_m:
            url = src_m.group(1)
            name = Path(url.split("?")[0]).name
            if assets_dir is not None and (assets_dir / name).exists():
                removed.append(f"外链 {name} -> 本地同名资源 {name}")
                return re.sub(r'(?:src|href)\s*=\s*["\'][^"\']+["\']',
                              f'src="{name}"', tag_text, count=1,
                              flags=re.IGNORECASE)
        removed.append(f"外链标签被移除（无本地副本）: {tag_text[:80]}")
        return ""

    n = len(_EXTERNAL_TAG.findall(out))
    if n:
        out = _EXTERNAL_TAG.sub(_ext_tag, out)
        removed.append(f"处理 {n} 个外链资源标签")

    # 残留的孤立 src="http..." 属性（非标准标签形态）
    n = len(_EXTERNAL_SRC.findall(out))
    if n:
        out = _EXTERNAL_SRC.sub("", out)
        removed.append(f"清理 {n} 个残留外链属性")

    # 5. 越界路径（整标签删除）
    n = len(_TRAVERSAL_TAG.findall(out))
    if n:
        out = _TRAVERSAL_TAG.sub("", out)
        removed.append(f"移除 {n} 个越界路径标签（绝对路径/../）")
    n = len(_TRAVERSAL.findall(out))
    if n:
        out = _TRAVERSAL.sub("", out)
        removed.append(f"清理 {n} 个残留越界路径属性")

    return {"html": out, "removed": removed}


def render_html_sandboxed(html_path: Path, out_png: Path | None = None,
                          channel: str = "msedge", width: int = 1280,
                          height: int = 720,
                          allow_root: Path | None = None) -> dict:
    """Playwright 渲染 + 路由拦截（只放行 allow_root 内的本地资源）。

    仿 PPTAgent core.open_slide 的 route 拦截：任何非本地 file:// 或不在
    allow_root 内的资源直接 abort，并记录阻断清单。返回
    {"png": str|None, "blocked": [url,...], "width": w, "height": h}
    """
    from playwright.sync_api import sync_playwright

    allow_root = allow_root or html_path.parent
    blocked: list[str] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=channel, headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})

            def _route(route):
                url = route.request.url
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if parsed.scheme == "file":
                    from urllib.parse import unquote
                    p = Path(unquote(parsed.path))
                    try:
                        inside = p.resolve().is_relative_to(allow_root.resolve())
                    except Exception:
                        inside = False
                    if inside:
                        route.continue_()
                        return
                blocked.append(url)
                route.abort()

            page.route("**/*", _route)
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(120)
            result = {"png": None, "blocked": blocked, "width": width, "height": height}
            if out_png is not None:
                page.screenshot(path=str(out_png))
                result["png"] = str(out_png)
            return result
        finally:
            browser.close()


def sanitize_file(html_path: Path, assets_dir: Path | None = None) -> dict:
    """对 HTML 文件做净化，写回原文件（原地覆盖），返回 {removed, bytes}。"""
    src = html_path.read_text(encoding="utf-8")
    res = sanitize_html(src, assets_dir)
    html_path.write_text(res["html"], encoding="utf-8")
    return {"removed": res["removed"], "bytes": len(res["html"].encode("utf-8"))}
