# -*- coding: utf-8 -*-
"""Coherence Agent：大纲级内容一致性闸门（**只报不改**）。

为什么是 deck 级一次调用、而不是逐页：
「这一页的内容是否属于它所在的章节」必须能看见**相邻页**才能判断——
逐页并发调用看不到上下文，正是旧版 `narrative_check` 判断力弱的原因之一。

为什么只报不改（用户决策）：
内容必须在生成时紧扣小标题；交给审查 agent 事后改，实测"越改越差"
（v24 就是加了闸门反而更难读）。所以这里只产出 verdict + 审计报告，
不合格就阻断，让人或重跑 Planner 解决。

实测坑：本供应商 `temperature=0.0` 会返回**空响应**（旧 narrative_check
的注释里记着这件事），必须用 0.3 并在失败时 refresh 重试一次。
"""
from __future__ import annotations

from .. import coherence as coh_mod
from ..config import OUTLINE_JSON
from ..llm import LLMClient
from ..models import save_json
from .state import AgentState

_SYSTEM = """你是演示文稿的**内容一致性审查员**。给你整份大纲（每页有所属章节
section、小标题 title/claim、Audience move purpose、要点 bullets，可能还有表格/图/公式）。

纪律（照 ppt-master 的 Page Message Discipline / Claim Discipline）：
- **标题即结论**：一页只讲一个可判真假的断言，正文只提供支撑它的证据与限定；
- 凡是正文讲的东西与该页 claim 不是同一件事，就是不一致；
- 凡是内容不属于它所在章节的，就是错位；
- **判定要严**，宁可报出来让人复核，也不要放过明显的脱节。

只输出 JSON：
{"verdicts":[{"slide_id":"s05",
  "title_supported": true,     // 正文/表格/图是否直接支撑本页 claim
  "block_fit": true,           // 内容是否属于它所在的章节
  "single_argument": true,     // 是否只讲了一个论点
  "reason": "一句话理由（说清脱节在哪，或与相邻页的关系）"}]}

硬性要求：**每一页都要给 verdict，不得遗漏**。不要给修改建议，只做判定。"""


def _norm_outline_for_prompt(outline: dict) -> str:
    lines = []
    for s in (outline.get("slides") or []):
        st = str(s.get("slide_type") or "")
        if st in ("cover", "agenda", "toc", "closing"):
            continue
        bl = "；".join(str(b.get("text") if isinstance(b, dict) else b)[:40]
                       for b in (s.get("bullets") or [])[:4])
        tb = s.get("table") or {}
        tbl = ""
        if tb.get("header"):
            tbl = " | 表格表头：" + " / ".join(str(h) for h in tb["header"][:8])
        fig = " | 有图" if s.get("figure") else ""
        lines.append(
            f"- {s.get('slide_id')} [{s.get('section')}] 小标题：{s.get('title')}"
            f"\n    本页断言(claim)：{s.get('claim')}"
            f"\n    认知目标(purpose)：{s.get('purpose')}"
            f"\n    正文：{bl}{tbl}{fig}")
    return "\n".join(lines)


def coherence_node(state: AgentState) -> dict:
    llm: LLMClient | None = state.get("_llm")
    outline = state.get("outline") or {}
    parsed = state.get("parsed") or {}
    logs = list(state.get("logs") or [])
    if not outline.get("slides"):
        return {}

    # ① 确定性部分（无 Key 也能跑）
    det = coh_mod.deterministic_issues(outline, parsed)
    print(coh_mod.report(det))

    # ② LLM 部分：deck 级一次判定
    verdicts: list[dict] = []
    if llm is not None:
        msgs = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content":
                 "大纲（按页序）：\n" + _norm_outline_for_prompt(outline)}]
        res = None
        for attempt in range(2):
            try:
                res = llm.chat_json(msgs, temperature=0.3, max_tokens=4000,
                                    refresh=bool(attempt))
            except Exception as e:  # noqa: BLE001
                print(f"[Coherence] 判定调用失败：{e}")
                res = None
            if isinstance(res, dict) and res.get("verdicts"):
                break
        if isinstance(res, dict):
            verdicts = [v for v in (res.get("verdicts") or []) if isinstance(v, dict)]
        if not verdicts:
            # 失败要响：旧实现这里是 `print + return`（静默放过）。
            # 未判定的页按 hard 记，P2P_STRICT=1 时直接失败。
            det.append({"slide_id": "", "class": "coherence_gate_unavailable",
                        "severity": "hard",
                        "desc": "一致性判定未返回结果（模型两次都失败），"
                                "这些页从未被检查过"})

    bad = [v for v in verdicts
           if not (v.get("title_supported", True) and v.get("block_fit", True)
                   and v.get("single_argument", True))]
    if bad:
        for v in bad:
            reasons = []
            if not v.get("title_supported", True):
                reasons.append("正文不支撑标题断言")
            if not v.get("block_fit", True):
                reasons.append("内容不属于该章节")
            if not v.get("single_argument", True):
                reasons.append("一页讲了多个论点")
            det.append({"slide_id": v.get("slide_id", ""),
                        "class": "incoherent_page", "severity": "hard",
                        "desc": "；".join(reasons) + "——" + str(v.get("reason", ""))[:80]})

    # 审计落盘：只报告，不改任何内容
    save_json({"verdicts": verdicts, "issues": det}, OUTLINE_JSON.parent / "coherence_report.json")
    logs.append(f"[Coherence] 判定 {len(verdicts)} 页，其中 {len(bad)} 页与标题/章节不符；"
                f"确定性问题 {len([i for i in det if i.get('severity') == 'hard'])} 条")
    print(f"[Coherence] {logs[-1]}")

    coh_mod.raise_if_violated(det)
    return {"outline": outline, "coherence_issues": det, "logs": logs}
