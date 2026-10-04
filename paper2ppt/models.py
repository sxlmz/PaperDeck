# -*- coding: utf-8 -*-
"""中间态数据模型：outline.json / design.json 的 schema 定义与校验。

结构（与"内容与表现分离"原则一致）：
  outline.json
    {
      "paper": {...元信息...},
      "slides": [
        {
          "slide_id": "s01",
          "slide_type": "cover|agenda|bullets|two_col|figure|chart|table|keycards|formula",
          "title": "...",
          "section": "...",
          "bullets": [...],          # 或 keycards / table / chart 专用字段
          "chart": {...},            # chart 页的数据规格
          "figure": {...},           # figure 页
          "note": "讲稿"
        }, ...
      ]
    }

  design.json = outline + 每页 layout 决策（图表已落盘、图片路径、版式参数）

本模块是 slide 数据形状的**唯一权威**：designer / critic 修订 / renderer 三方
都通过 can_render() 判断「这一页能不能按这个版式渲染」，避免出现
「layout 回退了但 slide_type 没回退」这类两边不一致的状态。
"""
from __future__ import annotations

import json
from typing import Any

# outline/design 结构版本；改动字段语义时递增（LLM 缓存键会带上它）
# v3: outline 改为两级结构（blocks[] -> pages[]），每页新增 subtitle/claim/purpose
SCHEMA_VERSION = 3

SLIDE_TYPES = {
    "cover", "agenda", "bullets", "two_col",
    "figure", "chart", "table", "keycards", "formula",
}

# 每种版式渲染所必需的数据字段（缺了就一定会渲染失败或渲染成空页）
SLIDE_DATA_KEYS: dict[str, tuple[str, ...]] = {
    "cover": (),
    "agenda": (),            # items 缺失时可回退 bullets
    "bullets": ("bullets",),
    "two_col": ("bullets",),
    "figure": (),
    "chart": (),
    "table": ("table",),
    "keycards": ("keycards",),
    "formula": ("formula",),
}

# table_wide 只是**版式**，不是 slide_type：渲染时分发到 table。
# （此前 critic 修订路径会把 table_wide 写进 slide_type，制造出 validate 拒绝的状态。）
LAYOUT_TO_TYPE = {"table_wide": "table"}


class OutlineError(ValueError):
    pass


def load_json(path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(obj, path) -> None:
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- 形状判据
def type_for_layout(layout: str, default: str = "bullets") -> str:
    """版式名 -> 实际 slide_type。"""
    if not layout:
        return default
    return LAYOUT_TO_TYPE.get(layout, layout)


def can_render(slide: dict, slide_type: str | None = None,
               require_asset: bool = True) -> tuple[bool, str]:
    """这一页能否按 slide_type 渲染；返回 (是否可行, 原因)。

    require_asset=False 时只检查数据字段，不要求图片/图表已落盘——
    Planner 出大纲时图片还没裁，此时不该用素材缺失去卡它。
    """
    st = slide_type or slide.get("slide_type") or "bullets"
    for key in SLIDE_DATA_KEYS.get(st, ()):
        if not slide.get(key):
            return False, f"缺 {key}"
    if require_asset:
        has_visual = bool(slide.get("image_path") or slide.get("chart_path"))
        if st == "figure" and not has_visual:
            return False, "缺图片素材"
        if st == "chart" and not has_visual:
            return False, "缺图表素材"
        # two_col 是「左文右图」：没有右侧素材时，文字会被挤在 6.35" 的左栏里
        # 半行就换行，右半页全空——不如降级成整幅要点页。
        if st == "two_col" and not has_visual:
            return False, "缺右侧素材（左文右图退化为整幅要点页更合适）"
    return True, ""


def _bullet_texts(bullets) -> list[str]:
    out = []
    for b in (bullets or []):
        if isinstance(b, dict):
            t = b.get("text") or b.get("head") or ""
        else:
            t = b
        t = str(t).strip()
        if t:
            out.append(t)
    return out


def _cards_from(lines: list[str], limit: int = 4) -> list[dict]:
    """把要点行改写成卡片（head 取前段，text 取整句）。"""
    cards = []
    for ln in lines[:limit]:
        head, text = ln, ln
        for sep in ("：", ":", "——", " - "):
            if sep in ln:
                head, text = ln.split(sep, 1)
                break
        cards.append({"head": head[:16], "text": text[:60]})
    return cards


def _fallback_lines(slide: dict) -> list[str]:
    """从任意形状的数据里榨出可排版的文字行（供降级为 bullets 用）。"""
    lines = _bullet_texts(slide.get("bullets"))
    if lines:
        return lines
    for card in (slide.get("keycards") or []):
        if isinstance(card, dict):
            head, text = card.get("head", ""), card.get("text", "")
            lines.append(f"{head}：{text}" if head else str(text))
    tb = slide.get("table") or {}
    for row in (tb.get("rows") or [])[:4]:
        cells = [str(c) for c in (row or []) if str(c or "").strip()]
        if cells:
            lines.append(" | ".join(cells)[:70])
    if slide.get("formula"):
        lines.append(str(slide["formula"]))
    if slide.get("takeaway"):
        lines.append(str(slide["takeaway"]))
    if slide.get("title"):
        lines.append(str(slide["title"]))
    return [x for x in lines if str(x).strip()]


def normalize_slide_data(slide: dict, target: str | None = None) -> dict:
    """把一页的数据补齐成目标版式所需形状（返回副本，不改入参）。

    这是让「Critic 建议改版式」合法化的关键：bullets 页可以被真正改写成
    keycards / agenda，而不是留下一个缺数据的 slide_type 去把渲染搞崩。
    """
    out = dict(slide)
    tgt = target or out.get("slide_type") or "bullets"
    lines = _bullet_texts(out.get("bullets"))

    if tgt == "agenda" and not out.get("items"):
        items = lines or _fallback_lines(out)
        if items:
            out["items"] = [x[:90] for x in items[:6]]
    elif tgt == "keycards" and not out.get("keycards"):
        src = lines or _fallback_lines(out)
        if src:
            out["keycards"] = _cards_from(src)
    elif tgt == "bullets" and not lines:
        src = _fallback_lines(out)
        if src:
            out["bullets"] = [{"text": x} for x in src]
    return out


def normalize_outline(outline: dict) -> dict:
    """把两级 blocks 结构展平成 slides 列表（就地补 slides 键并返回 outline）。

    Planner 现在产出「四块 → 每块 2~3 个小标题 → 每页内容」的两级结构；
    下游（html_author / faithfulness / html_critic / manifest）全都按扁平的
    slides 工作，所以在这里展平一次，**下游零改动**。

    章节连续性因此成为结构属性：section 只会按块出现一次，不可能 A,B,A。
    兼容：已是扁平结构（有 slides、无 blocks）时原样返回——老 outline.json
    与 offline_planner 都走这条路。
    """
    blocks = outline.get("blocks")
    if not blocks or outline.get("slides"):
        return outline

    slides: list[dict] = []
    n = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        section = str(block.get("section") or "").strip()
        desc = str(block.get("desc") or "").strip()
        for page in (block.get("pages") or []):
            if not isinstance(page, dict):
                continue
            n += 1
            s = dict(page)
            subtitle = str(s.get("subtitle") or s.get("title") or "").strip()
            s["subtitle"] = subtitle
            s["title"] = subtitle            # 标题即小标题：同源，不可能漂移
            if not str(s.get("claim") or "").strip():
                s["claim"] = subtitle
            s["section"] = section
            s["_section_desc"] = desc        # 章节分隔页的副文案
            s["slide_id"] = s.get("slide_id") or f"s{n:02d}"
            s["slide_type"] = s.get("slide_type") or "bullets"
            s.setdefault("content_kind", "concept")
            slides.append(s)
    outline["slides"] = slides
    return outline


def validate_outline(outline: dict[str, Any]) -> None:
    """结构校验：slide_type 合法、slide_id 唯一、必填字段存在。"""
    if "slides" not in outline or not isinstance(outline["slides"], list):
        raise OutlineError("outline.json 缺少 slides 列表")
    ids = []
    for i, s in enumerate(outline["slides"]):
        if not isinstance(s, dict):
            raise OutlineError(f"第 {i} 个 slide 不是对象")
        sid = s.get("slide_id")
        if not sid:
            raise OutlineError(f"第 {i} 个 slide 缺少 slide_id")
        if sid in ids:
            raise OutlineError(f"slide_id 重复: {sid}")
        ids.append(sid)
        st = s.get("slide_type")
        if st not in SLIDE_TYPES:
            raise OutlineError(f"{sid}: 未知 slide_type {st!r}")
        # 与渲染方共用同一判据（此时不要求素材已落盘）
        ok, why = can_render(s, st, require_asset=False)
        if not ok:
            raise OutlineError(f"{sid}: {why}")
        if st == "formula" and not s.get("bullets"):
            raise OutlineError(f"{sid}: formula 页缺少 bullets")
    print(f"[planner] 大纲校验通过：共 {len(outline['slides'])} 页")


# 每页版式决策（Designer 输出），这里仅定义字段约束便于扩展
LAYOUT_KEYS = {
    "cover": ["bg", "title_size", "subtitle_size"],
    "bullets": ["body_box", "title_size", "bullet_size"],
    "chart": ["chart_path", "chart_box", "takeaway_box"],
    "figure": ["image_path", "image_box", "caption_box"],
    "table": ["table_box", "col_widths", "font_size"],
    "two_col": ["left_box", "right_box", "right_kind"],
    "agenda": ["item_box"],
    "keycards": ["card_specs"],
    "formula": ["formula_box", "body_box"],
}
