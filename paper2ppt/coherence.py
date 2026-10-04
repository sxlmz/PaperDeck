# -*- coding: utf-8 -*-
"""内容一致性：**只看指标，不做修复**。

用户决策：内容必须在**生成时**就紧扣小标题（见 planner 的两段式规划）。
事后让审查 agent 去改，实测越改越差——所以这里只判定、只报告、只阻断，
**绝不自动改写标题或内容**。

与相邻模块的分工：
  * `structure_check.py` 管**结构**（章节连续、每块页数、小标题齐备）——硬失败。
  * `faithfulness_agent` 管**事实**（bullet 能否由论文原文推出）——删无依据的要点。
  * 本模块管**逻辑**（claim 与证据是否同一件事）——报告 + 阻断。

确定性部分（本文件）不需要 LLM：
  * claim 里出现的数字，必须能在本页正文/表格/图注/已核实的 VLM 读数里找到；
  * 方法页若给了公式，该公式必须真的来自论文。
"""
from __future__ import annotations

import os
import re

#: claim 与证据的相似度低于它就提示"可能不是同一件事"（仅提示，不阻断）
LOW_OVERLAP = 0.12

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _norm_num(s: str) -> str:
    """数字归一：去千分位、百分号、正负号；.518 与 0.518 等价。"""
    s = s.replace(",", "").replace("％", "%").lstrip("+-")
    if s.startswith("."):
        s = "0" + s
    if s.endswith("%"):
        s = s[:-1]
    return s.rstrip("0").rstrip(".") if "." in s else s


def _numbers(text: str) -> set[str]:
    return {_norm_num(m) for m in _NUM_RE.findall(str(text or ""))}


def _grounding_corpus(parsed: dict, slide: dict) -> str:
    """本页可用来印证数字的语料：表格、图注、VLM 读数、正文要点、论文全文。"""
    parts: list[str] = []
    for b in (slide.get("bullets") or []):
        parts.append(str(b.get("text") if isinstance(b, dict) else b))
    if slide.get("takeaway"):
        parts.append(str(slide["takeaway"]))
    tb = slide.get("table") or {}
    parts += [str(c) for c in (tb.get("header") or [])]
    parts += [str(c) for r in (tb.get("rows") or []) for c in (r or [])]
    fig = slide.get("figure") or {}
    parts.append(str(fig.get("caption") or ""))
    vlm = parsed.get("vlm") or {}
    for chunk in (vlm.get("figures") or {}).values():
        parts.append(str(chunk.get("takeaway") or ""))
        for kv in (chunk.get("key_values") or []):
            parts.append(str(kv.get("value") if isinstance(kv, dict) else kv))
    for chunk in (vlm.get("tables") or {}).values():
        parts += [str(x) for x in (chunk.get("header") or [])]
        parts += [str(x) for r in (chunk.get("rows") or []) for x in (r or [])]
    # 兜底：论文全文（归一后比对，避免 claim 的数字其实来自某页正文）
    parts += [str(p.get("text") or "") for p in (parsed.get("pages") or [])]
    return "\n".join(parts)


def deterministic_issues(outline: dict, parsed: dict) -> list[dict]:
    """确定性一致性问题（无需 LLM）。severity=hard 表示应阻断。"""
    issues: list[dict] = []
    corpus_nums = _numbers(_grounding_corpus(parsed, {}))
    formulas = set()
    for chunk in ((parsed.get("vlm") or {}).get("formulas") or {}).values():
        formulas.add(re.sub(r"\s+", "", str(chunk.get("latex") or "")))
    for blk in (parsed.get("content_list") or []):
        if blk.get("type") == "formula":
            formulas.add(re.sub(r"\s+", "", str(blk.get("text") or "")))

    for s in (outline.get("slides") or []):
        sid = s.get("slide_id", "")
        claim = str(s.get("claim") or s.get("title") or "")
        # ① claim 里的数字必须有出处
        local = _numbers(_grounding_corpus(parsed, s)) | corpus_nums
        for n in _numbers(claim):
            if n not in local:
                issues.append({
                    "slide_id": sid, "class": "claim_number_ungrounded",
                    "severity": "hard",
                    "desc": f"claim「{claim[:32]}」里的数字 {n} 在论文与本页证据中都找不到",
                })
        # ② 公式必须来自论文
        f = re.sub(r"\s+", "", str(s.get("formula") or ""))
        if f and formulas and f not in formulas:
            issues.append({
                "slide_id": sid, "class": "formula_not_in_paper",
                "severity": "hard",
                "desc": f"公式「{f[:36]}」不在论文的公式清单里（可能是编造的）",
            })
    return issues


def raise_if_violated(issues: list[dict], strict: bool | None = None) -> None:
    """与 coverage / structure_check 同构：hard 问题未修复即失败。"""
    hard = [i for i in issues if i.get("severity") == "hard"]
    if not hard:
        return
    strict = (os.environ.get("P2P_STRICT", "0") == "1") if strict is None else strict
    detail = "；".join(f"{i['class']}({i.get('slide_id') or '-'})" for i in hard[:6])
    if strict:
        raise RuntimeError(f"内容一致性校验未通过（{len(hard)} 项）：{detail}")
    print(f"[coherence] ⚠ {len(hard)} 处内容一致性问题"
          f"（P2P_STRICT=1 时会直接失败）：{detail}")


def report(issues: list[dict]) -> str:
    if not issues:
        return "[coherence] ✓ 未发现确定性一致性问题"
    lines = [f"[coherence] ✗ {len(issues)} 处一致性问题："]
    for i in issues:
        lines.append(f"    · [{i['class']}]（{i.get('slide_id') or '-'}）{i['desc']}")
    return "\n".join(lines)
