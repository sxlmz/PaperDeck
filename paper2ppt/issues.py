# -*- coding: utf-8 -*-
"""跨轮次问题追踪：判断「改完之后，上一轮的问题到底有没有被修掉」。

为什么必须逐条比对而不是数个数：
    38 -> 39 -> 38 这个序列有两种完全相反的解释——
      (a) 修好了 30 条、又新增 31 条（改动剧烈但方向不对）
      (b) 一条都没修掉，原地打转
    两者对策截然不同（换动作 vs 换目标），只看总数分不出来。

匹配规则（**保守优先**：宁可判成 persisting，也不要虚报 fixed）：
    同一页 + 同一类型，且满足其一：
      * 描述文本相似度 >= TEXT_SIM（0.55）
      * 位置区域重叠率  >= REGION_IOU（0.45）
    两条都不满足，才认为是「新问题」。
"""
from __future__ import annotations

import difflib
import re

TEXT_SIM = 0.55
REGION_IOU = 0.45

#: 坐标后缀（"（位置 x=0.0, y=1.2, ...）"）不参与文本比对
_COORD_RE = re.compile(r"（位置[^）]*）")
_NORM_RE = re.compile(r"[\s，。、；：（）()\[\]{}「」【】,.;:!?\"'“”‘’]+")


def normalize_desc(desc: str) -> str:
    d = _COORD_RE.sub("", str(desc or ""))
    return _NORM_RE.sub("", d).lower()


def text_similarity(a: str, b: str) -> float:
    na, nb = normalize_desc(a), normalize_desc(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def region_overlap(box_a, box_b) -> float:
    """两个 [x,y,w,h] 的交集 / 较小者面积（与 deck_ops 口径一致）。"""
    if not (isinstance(box_a, (list, tuple)) and isinstance(box_b, (list, tuple))):
        return 0.0
    if len(box_a) != 4 or len(box_b) != 4:
        return 0.0
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    if ix <= 0 or iy <= 0:
        return 0.0
    small = min(aw * ah, bw * bh)
    return (ix * iy) / small if small > 0 else 0.0


def _bucket(issue: dict) -> tuple:
    """粗分桶：同页同类型才可能匹配，避免跨类型误判。"""
    t = str(issue.get("type") or "")
    # vlm:xxx 与 xxx 视为不同来源，不互相匹配（判据不同）
    return (issue.get("slide"), t)


def _same(prev: dict, cur: dict) -> bool:
    if _bucket(prev) != _bucket(cur):
        return False
    if text_similarity(prev.get("desc", ""), cur.get("desc", "")) >= TEXT_SIM:
        return True
    if region_overlap(prev.get("box_in"), cur.get("box_in")) >= REGION_IOU:
        return True
    return False


def diff_rounds(prev: list[dict], cur: list[dict]) -> dict:
    """比较相邻两轮的问题集合。

    返回 {"fixed": [...], "persisting": [...], "new": [...]}。
    persisting 里带上它在上一轮的原文，便于人工核对是不是同一件事。
    """
    prev = list(prev or [])
    cur = list(cur or [])
    used_prev: set[int] = set()
    persisting, new = [], []

    for c in cur:
        hit = None
        for i, p in enumerate(prev):
            if i in used_prev:
                continue
            if _same(p, c):
                hit = i
                break
        if hit is None:
            new.append(c)
        else:
            used_prev.add(hit)
            persisting.append({**c, "_prev_desc": prev[hit].get("desc", "")})
    fixed = [p for i, p in enumerate(prev) if i not in used_prev]
    return {"fixed": fixed, "persisting": persisting, "new": new}


def summarize(prev: list[dict], cur: list[dict], round_no: int = 0) -> str:
    """一行结论 + 明细，直接进日志/报告。"""
    d = diff_rounds(prev, cur)
    n_p, n_f, n_n = len(d["persisting"]), len(d["fixed"]), len(d["new"])
    verdict = ("全部是上轮遗留，无新增" if n_n == 0 and n_p else
               "上一轮问题已全部消失" if n_p == 0 and n_f else
               "仍有上轮问题未解决" if n_p else "全部为新问题")
    head = (f"[问题追踪] 第{round_no}轮：{len(cur)} 条 = "
            f"遗留 {n_p} + 新增 {n_n}（上轮 {len(prev)} 条中修掉 {n_f}）—— {verdict}")
    lines = [head]
    if n_p:
        lines.append(f"   遗留 {n_p} 条（改了但没修掉）：")
        for x in d["persisting"][:8]:
            lines.append(f"     · 第{x.get('slide')}页 [{x.get('type')}] "
                         f"{str(x.get('desc'))[:70]}")
    if n_n:
        lines.append(f"   新增 {n_n} 条：")
        for x in d["new"][:8]:
            lines.append(f"     · 第{x.get('slide')}页 [{x.get('type')}] "
                         f"{str(x.get('desc'))[:70]}")
    return "\n".join(lines)


def composition(issues: list[dict]) -> dict:
    """按「确定性缺陷 vs VLM 主观判断」拆开统计。

    这个拆分很重要：确定性缺陷有客观标准、能收敛到 0；而 ugly/clutter 这类
    主观项几乎每页都会有，**它们构成的问题数下限是永远压不到 0 的**，
    拿总数当验收信号会被这类项长期卡住。
    """
    det = [i for i in issues if not str(i.get("type", "")).startswith("vlm:")]
    subj = [i for i in issues if str(i.get("type", "")).startswith("vlm:")]
    kinds: dict[str, int] = {}
    for i in subj:
        k = str(i["type"]).removeprefix("vlm:")
        kinds[k] = kinds.get(k, 0) + 1
    return {"deterministic": len(det), "subjective": len(subj),
            "subjective_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}
