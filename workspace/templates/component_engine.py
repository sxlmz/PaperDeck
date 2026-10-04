# -*- coding: utf-8 -*-
"""组件化模板引擎：主题CSS变量 + 原子组件片段 + 蓝图JSON组装。

组件文件约定（components/<name>.html）：
- 纯结构，无 <style>；所有样式由主题 CSS（themes/<theme>.css）用 var() 驱动
- 占位符 {{key}} 由蓝图节点字段填充
- 列表用标记段定义 item 模板：
    <!-- item-tpl -->
    <div class="point">{{idx}} {{text}}<span class="src">{{source}}</span></div>
    <!-- /item-tpl -->
  {{items}} 位置展开为逐条渲染结果

用法：
    from component_engine import render_blueprint
    html = render_blueprint(bp_json, theme="theme-dark")
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
THEMES = ROOT / "themes"
COMPONENTS = ROOT / "components"

_ITEM_TPL_RE = re.compile(r"<!--\s*item-tpl\s*-->(.*?)<!--\s*/item-tpl\s*-->", re.S)


def load_theme_css(theme: str) -> str:
    return (THEMES / f"{theme}.css").read_text(encoding="utf-8")


def load_component(name: str) -> str:
    return (COMPONENTS / f"{name}.html").read_text(encoding="utf-8")


def _render_items(tpl: str, items: list) -> tuple[str, str]:
    """从组件片段提取 item 模板，渲染 {{items}}。返回 (替换后片段, 是否含items)。"""
    m = _ITEM_TPL_RE.search(tpl)
    if not m:
        return tpl, False
    item_tpl = m.group(1).strip("\n")
    tpl = tpl.replace(m.group(0), "").strip()
    rendered = []
    for i, it in enumerate(items, 1):
        out = item_tpl
        for k, v in it.items():
            out = out.replace("{{" + k + "}}", str(v))
        out = out.replace("{{idx}}", f"{i:02d}")
        out = out.replace("{{idx1}}", str(i))
        rendered.append(out)
    return tpl.replace("{{items}}", "\n".join(rendered)), True


def _fill_fields(tpl: str, node: dict) -> str:
    """填充普通字段占位符，清理残留。"""
    for k, v in node.items():
        if k.startswith("_") or k in ("children", "layout", "columns", "component"):
            continue
        tpl = tpl.replace("{{" + k + "}}", str(v))
    tpl = re.sub(r"\{\{[^}]+\}\}", "", tpl)
    return tpl


def render_node(node: dict) -> str:
    """渲染一个节点：原子组件或嵌套容器。"""
    if node.get("component"):
        tpl = load_component(node["component"])
        tpl, has_items = _render_items(tpl, node.get("items", []))
        tpl = _fill_fields(tpl, node)
        return tpl
    # 嵌套容器
    children = "\n".join(render_node(c) for c in node.get("children", []))
    layout = node.get("layout", "col")
    if layout == "row":
        cols = node.get("columns")
        style = f" style=\"grid-template-columns:{';'.join(cols)}\"" if cols else ""
        return f'<div class="box row"{style}>{children}</div>'
    if layout == "grid":
        n = max(len(node.get("children", [])), 1)
        style = f" style=\"grid-template-columns:repeat({n},1fr)\""
        return f'<div class="box grid"{style}>{children}</div>'
    return f'<div class="box col">{children}</div>'


def render_blueprint(bp: dict, theme: str = "theme-light") -> str:
    """蓝图 JSON -> 完整 HTML 页（1280x720）。"""
    css = load_theme_css(theme)
    children = "\n".join(render_node(c) for c in bp.get("children", []))
    layout = bp.get("layout", "col")
    body_cls = f"body {layout}"
    cols = bp.get("columns")
    body_style = f" style=\"grid-template-columns:{';'.join(cols)}\"" if cols and layout == "row" else ""

    head = ""
    if bp.get("kicker") or bp.get("title"):
        sub = f'<div class="subtitle">{bp["subtitle"]}</div>' if bp.get("subtitle") else ""
        head = (f'<div class="head"><div class="kicker">{bp.get("kicker", "")}</div>'
                f'<div class="title">{bp.get("title", "")}</div>{sub}</div>')

    foot = ""
    if bp.get("source") or bp.get("page"):
        foot = (f'<div class="foot"><div class="src">{bp.get("source", "")}</div>'
                f'<div class="page">{bp.get("page", "")}</div></div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{bp.get("title", "")}</title>
<style>
{css}
</style>
</head>
<body>
<div class="slide">
  <div class="bg-deco"></div>
  {head}
  <div class="{body_cls}"{body_style}>
    {children}
  </div>
  {foot}
</div>
</body>
</html>"""


if __name__ == "__main__":
    import json, sys
    bp = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    theme = sys.argv[2] if len(sys.argv) > 2 else "theme-light"
    out = sys.argv[3] if len(sys.argv) > 3 else "out.html"
    Path(out).write_text(render_blueprint(bp, theme), encoding="utf-8")
    print("written", out)
