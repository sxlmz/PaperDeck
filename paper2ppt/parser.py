# -*- coding: utf-8 -*-
"""Parser 阶段：PDF -> parsed_paper.json（结构化学术资产）。

覆盖：元信息、逐页文本、图片清单、表格（尽力而为）、公式（LaTeX 由
pdf 文本层直取，公式级 OCR 留待后续 MinerU 替换）。
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from .config import PARSED_JSON
from .models import save_json

# 解析产物结构版本：改了抽取逻辑就递增，旧缓存自动失效
# v4: 表格补 bbox + table_no（供 VLM 精确裁图表送审）
PARSER_VERSION = 4


def parse_pdf(pdf_path: Path, out_path: Path = PARSED_JSON) -> dict:
    doc = pymupdf.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text", sort=True)
        pages.append({"page_no": i + 1, "text": text, "char_count": len(text)})

    meta = dict(doc.metadata or {})
    image_inventory = []
    seen = set()
    for i, page in enumerate(doc):
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                info = doc.extract_image(xref)
                image_inventory.append({
                    "xref": xref, "page": i + 1, "ext": info["ext"],
                    "width": info["width"], "height": info["height"],
                    "size_bytes": len(info["image"]),
                })
            except Exception as e:  # noqa: BLE001
                image_inventory.append({"xref": xref, "page": i + 1, "error": str(e)})

    tables = []
    for i, page in enumerate(doc):
        try:
            for t in page.find_tables().tables:
                rows = t.extract()
                if rows and any(c for r in rows for c in (r or [])):
                    tables.append({
                        "page": i + 1,
                        "rows": rows,
                        # bbox 用于给 VLM 精确裁图；此前被丢弃，导致表格没法单独送审
                        "bbox": [round(float(v), 1) for v in t.bbox],
                    })
        except Exception:  # noqa: BLE001
            pass

    # 按图注裁剪论文原始图（架构图/结果曲线，矢量+位图整页渲染）
    from .figures import extract_figures
    assets_dir = out_path.parent / "assets"
    figures = extract_figures(doc, assets_dir)

    # 结构化 content_list + 图/表引用链接
    from .structure import build_content_list, build_citation_links
    content_list = build_content_list(doc)
    citation_links = build_citation_links(content_list, figures, tables)

    # 给每个表配一个稳定的编号（"表 N"，来自同页的 caption_table 块），
    # 后续 bullet 才能引用「（表 3）」而不是含糊的「文中表格」
    _assign_table_numbers(tables, content_list)

    result = {
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "metadata": meta,
        "total_pages": doc.page_count,
        "pages": pages,
        "image_inventory": image_inventory,
        "tables": tables,
        "figures": figures,
        "content_list": content_list,
        "citation_links": citation_links,
    }
    save_json(result, out_path)
    print(f"[parser] 完成: pages={doc.page_count} images={len(image_inventory)} "
          f"tables={len(tables)} figures={len(figures)} -> {out_path.name}")
    return result


def _assign_table_numbers(tables: list[dict], content_list: list[dict]) -> None:
    """就地给 tables 补 table_no：按页取该页 caption_table 块里的 "Table N"。"""
    import re as _re
    cap_by_page: dict[int, list[int]] = {}
    for blk in content_list:
        if blk.get("type") != "caption_table":
            continue
        m = _re.search(r"Table\s*(\d+)", blk.get("text") or "")
        if m:
            cap_by_page.setdefault(blk["page"], []).append(int(m.group(1)))
    used: dict[int, int] = {}
    for t in tables:
        pno = t.get("page")
        nums = cap_by_page.get(pno) or []
        i = used.get(pno, 0)
        t["table_no"] = nums[i] if i < len(nums) else None
        used[pno] = i + 1


def plain_text(parsed: dict) -> str:
    return "\n\n".join(p["text"] for p in parsed["pages"])


def quality_report(parsed: dict) -> tuple[bool, str]:
    """解析质量体检：页数/每页字数/图数/表数是否像样。

    命中缓存后跑一次——缓存里的旧解析如果本身质量差，应该能被发现并重解析，
    而不是一直复用它。
    """
    pages = parsed.get("pages") or []
    n_pages = len(pages)
    chars = sum(len(p.get("text") or "") for p in pages)
    per_page = chars / n_pages if n_pages else 0
    n_fig = len(parsed.get("figures") or [])
    n_tab = len(parsed.get("tables") or [])
    ok = n_pages > 0 and per_page >= 200 and (n_fig + n_tab) >= 1
    msg = (f"页数={n_pages} 每页均字数={per_page:.0f} 图={n_fig} 表={n_tab}")
    if not n_pages:
        msg += "（无页面文本，可能是扫描件）"
    elif per_page < 200:
        msg += "（每页字数偏低，文本层可能不完整）"
    return ok, msg


def parse_pdf_cached(pdf_path: Path, out_path: Path = PARSED_JSON,
                     *, use_cache: bool = True, refresh: bool = False,
                     force: bool = False) -> tuple[dict, bool]:
    """带 Redis 缓存的解析入口，返回 (parsed, 是否命中缓存)。

    缓存键 = PDF 内容 sha256 + PARSER_VERSION：换论文或改解析逻辑都会自然失效。
    用内容哈希而不是 mtime/size —— Windows 上复制/同步会改 mtime 但内容没变，
    那会造成无谓的重解析并把下游 LLM 全部重跑一遍。
    """
    from . import cache

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sha = None
    if use_cache or not refresh:
        try:
            sha = cache.sha256_file(pdf_path)
        except OSError:
            sha = None

    ck = cache.paper_key(sha, PARSER_VERSION) if sha else None
    if ck and use_cache and not refresh and not force:
        hit = cache.cache_get(ck)
        if hit:
            ok, msg = quality_report(hit)
            if ok:
                print(f"[cache] 解析命中缓存 {ck[-12:]}（{msg}）")
                save_json(hit, out_path)      # 写回工作区，工具层其余代码零改动
                return hit, True
            print(f"[cache] 缓存解析质量不达标（{msg}），重新解析并更新缓存")

    parsed = parse_pdf(pdf_path, out_path)
    if ck:
        cache.cache_set(ck, parsed, cache.TTL_PAPER)
        cache.register(sha, [ck])
    return parsed, False
