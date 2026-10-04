# -*- coding: utf-8 -*-
"""Planner 阶段：学术资产 -> 演讲大纲 outline.json。

当前实现：
  * 若 workspace/outline.json 已由"规划 Agent（LLM）"审定生成，则加载并校验；
  * verify_facts：将大纲中的关键数字/声明与论文全文做基于文本的核对
    （事实性校验的轻量版，后续可替换为 RAG 检索）。
未来扩展：plan_with_llm() 接入外部 LLM API，按金字塔原理自动生成大纲。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import FULLTEXT_TXT, OUTLINE_JSON
from .models import load_json, validate_outline


def load_outline(path: Path = OUTLINE_JSON) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"outline.json 不存在: {path}")
    outline = load_json(path)
    validate_outline(outline)
    return outline


def _norm(s: str) -> str:
    s = re.sub(r"[\s\u00a0]+", "", str(s))
    s = re.sub(r"[-_−–—→←↑↓·/\\,，。：、；！？（）()\[\]{}<>《》\"'“”‘’]", "", s)
    return s.lower()


def verify_facts(outline: dict, fulltext_path: Path = FULLTEXT_TXT,
                 verbose: bool = True) -> list[str]:
    """把大纲中出现的数字/关键词与论文全文核对，返回未命中的声明列表。

    这是"内容准确性检查"的最小实现：只做文本命中，不做语义判断；
    用于拦截明显脱离论文的编造数字。
    """
    if not fulltext_path.exists():
        return [f"全文文件缺失: {fulltext_path}"]
    corpus = _norm(fulltext_path.read_text(encoding="utf-8"))

    misses = []
    claims = []

    def add_claim(text: str):
        if text:
            claims.append(text)

    for s in outline["slides"]:
        if s.get("slide_type") == "cover":
            continue
        add_claim(s.get("title", ""))
        for b in s.get("bullets", []) or []:
            if isinstance(b, str):
                add_claim(b)
            elif isinstance(b, dict):
                add_claim(b.get("text", ""))
                add_claim(b.get("head", ""))
        add_claim(s.get("takeaway", ""))
        ch = s.get("chart")
        if isinstance(ch, dict):
            add_claim(ch.get("caption", ""))
        fig = s.get("figure")
        if isinstance(fig, dict):
            add_claim(fig.get("caption", ""))

    for c in claims:
        key = _norm(c)
        # 数字锚定：提取声明中的数字（含小数/百分比/×N），任一数字在论文全文中出现即算命中
        nums = re.findall(r"\d+(?:\.\d+)?%?", c)
        if nums:
            hit = any(n in corpus for n in nums)
            missing_nums = [n for n in nums if n not in corpus]
        else:
            hit = False
            missing_nums = []
        if not hit:
            # 无数字的声明退化为 8 字窗口命中
            window = 8
            for i in range(0, len(key) - window + 1, 3):
                if key[i:i + window] in corpus:
                    hit = True
                    break
        if not hit:
            misses.append(c)
            if verbose:
                detail = f"（未命中数字: {missing_nums}）" if missing_nums else "（无数字声明未命中）"
                print(f"[planner] 事实核对未命中: {c} {detail}")

    if verbose:
        if misses:
            print(f"[planner] 事实核对: {len(claims)} 条声明，{len(misses)} 条未命中（需人工复核）")
        else:
            print(f"[planner] 事实核对: {len(claims)} 条声明全部命中论文全文")
    return misses


def plan_with_llm(parsed, api_config=None) -> dict:
    """预留：接入 LLM 自动生成大纲。当前抛错提示未实现，避免静默降级。"""
    raise NotImplementedError(
        "plan_with_llm 尚未接入外部 LLM API；当前请使用人工审定版 outline.json"
    )
