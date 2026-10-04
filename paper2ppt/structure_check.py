# -*- coding: utf-8 -*-
"""大纲**结构**校验：硬失败，不调 LLM。

为什么需要它（真实故障）：
`html_author_agent` 的章节页是这么生成的——

    if section and section != prev_section:
        chapter_no += 1          # section 一变就插一个章节分隔页

只要大纲的 `section` 不连续（A,B,A），就会插出两个 A 章节页。
实测 `output/html_v24_p1p5_fixed.pptx` 就因此变成 **31 页 12 个章节**，
「实验设置」「分析与消融」「总结」各出现两次，顺序错乱，
用户的原话是「完全不知道这个 PPT 在讲什么」。

那个问题**不是 LLM 写得不好**，而是结构可以被写成非法的。本模块负责让
非法结构在渲染前就失败：章节白名单、章节连续、每块页数、小标题齐备且不重复。

全部判为 hard —— 不合格就是 `RuntimeError`，不做任何"自动修好"。
按用户决策：内容要在**生成时**就紧扣小标题，不能靠事后审查 agent 去改
（实测越改越差）。
"""
from __future__ import annotations

import os
import re

#: 每个块的页数区间（含端点）
MIN_PAGES_PER_BLOCK = 2
MAX_PAGES_PER_BLOCK = 3

#: 块内两个小标题字符 3-gram 相似度超过它就视为重复
DUP_SUBTITLE_SIM = 0.72

_STOP = set("的了和与及或其这那为是在对从到把被并且以用")


def _ngrams(text: str, n: int = 3) -> set[str]:
    s = re.sub(r"[\s，。、；：（）()\[\]【】「」,.;:!?\"'“”‘’·—\-]+", "", text or "")
    s = "".join(ch for ch in s if ch not in _STOP)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def subtitle_similarity(a: str, b: str) -> float:
    """字符 3-gram Jaccard 相似度（0~1）。"""
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _content_pages(outline: dict) -> list[dict]:
    """展平后的内容页（排除封面/目录/结尾这类固定页）。"""
    fixed = {"cover", "agenda", "toc", "closing"}
    return [s for s in (outline.get("slides") or [])
            if str(s.get("slide_type") or "") not in fixed]


def structural_issues(outline: dict, blocks: list[str] | None = None) -> list[dict]:
    """返回结构性问题的清单；每项都是 hard。

    blocks: 期望的章节白名单与顺序（例如四块）。为 None 时只做连续性检查。
    """
    issues: list[dict] = []
    slides = outline.get("slides") or []
    pages = _content_pages(outline)

    def add(cls: str, desc: str, slide_id: str = ""):
        issues.append({"slide_id": slide_id, "class": cls,
                       "severity": "hard", "desc": desc})

    if not slides:
        add("empty_outline", "大纲没有任何内容页")
        return issues

    # ① section 必须在白名单内
    if blocks:
        for s in pages:
            sec = str(s.get("section") or "").strip()
            if sec and sec not in blocks:
                add("section_not_in_blocks",
                    f"章节「{sec}」不在骨架白名单 {blocks} 内", s.get("slide_id", ""))

    # ② section 必须连续：一旦某个 section 出现后又中断再出现，就是碎片化
    seen_done: set[str] = set()
    prev = None
    for s in pages:
        sec = str(s.get("section") or "").strip()
        if not sec:
            continue
        if sec != prev:
            if sec in seen_done:
                add("section_not_contiguous",
                    f"章节「{sec}」被打断后再次出现——会渲染成重复的章节分隔页"
                    f"（v24 出现 12 个章节的根因）", s.get("slide_id", ""))
            seen_done.add(sec)
            prev = sec

    # ③ 每个块的页数
    counts: dict[str, int] = {}
    for s in pages:
        sec = str(s.get("section") or "").strip()
        if sec:
            counts[sec] = counts.get(sec, 0) + 1
    for sec, n in counts.items():
        if n < MIN_PAGES_PER_BLOCK:
            add("block_too_few_pages",
                f"章节「{sec}」只有 {n} 页（要求 {MIN_PAGES_PER_BLOCK}~"
                f"{MAX_PAGES_PER_BLOCK} 页）")
        elif n > MAX_PAGES_PER_BLOCK:
            add("block_too_many_pages",
                f"章节「{sec}」有 {n} 页（要求 {MIN_PAGES_PER_BLOCK}~"
                f"{MAX_PAGES_PER_BLOCK} 页）")
    if blocks:
        for sec in blocks:
            if sec not in counts:
                add("block_missing", f"骨架里的章节「{sec}」没有任何页面")

    # ④ 每页必须有 subtitle/claim/purpose —— 这是"内容围绕小标题生成"的落点
    for s in pages:
        sid = s.get("slide_id", "")
        if not str(s.get("subtitle") or s.get("title") or "").strip():
            add("missing_subtitle", "内容页缺少小标题（subtitle/title）", sid)
        if not str(s.get("claim") or "").strip():
            add("missing_claim", "内容页缺少 claim（本页唯一可判真假的断言）", sid)
        if not str(s.get("purpose") or "").strip():
            add("missing_purpose",
                "内容页缺少 purpose（Audience move：听众听完改变什么认知）", sid)

    # ⑤ 块内小标题不得语义重复（防止一个块里两页讲同一件事）
    for sec in counts:
        subs = [(str(s.get("subtitle") or s.get("title") or ""), s.get("slide_id", ""))
                for s in pages if str(s.get("section") or "").strip() == sec]
        for i in range(len(subs)):
            for j in range(i + 1, len(subs)):
                sim = subtitle_similarity(subs[i][0], subs[j][0])
                if sim >= DUP_SUBTITLE_SIM:
                    add("duplicate_subtitle",
                        f"章节「{sec}」的「{subs[i][0]}」与「{subs[j][0]}」"
                        f"高度重复（相似度 {sim:.2f}）", subs[j][1])

    return issues


def raise_if_violated(issues: list[dict], strict: bool | None = None) -> None:
    """结构非法即失败。与 coverage.Coverage.raise_if_violated 同构，
    但**没有容忍阈值**——结构问题只有"合法/非法"两种。"""
    if not issues:
        return
    strict = (os.environ.get("P2P_STRICT", "0") == "1") if strict is None else strict
    detail = "；".join(f"{i['class']}({i.get('slide_id') or '-'})" for i in issues[:6])
    if strict:
        raise RuntimeError(f"大纲结构校验未通过（{len(issues)} 项）：{detail}")
    print(f"[structure] ⚠ 大纲结构有 {len(issues)} 处问题（P2P_STRICT=1 时会直接失败）：{detail}")


def report(issues: list[dict]) -> str:
    if not issues:
        return "[structure] ✓ 结构合法：章节连续、每块页数达标、每页有 claim/purpose"
    lines = [f"[structure] ✗ {len(issues)} 处结构问题："]
    for i in issues:
        where = f"（{i['slide_id']}）" if i.get("slide_id") else ""
        lines.append(f"    · [{i['class']}]{where} {i['desc']}")
    return "\n".join(lines)
