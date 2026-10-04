# -*- coding: utf-8 -*-
"""Planner Agent 节点：学术资产 -> 演讲大纲 outline.json。

模式：
  * LLM 模式（默认，配置了 Key 时）：调用大模型把论文转为结构化大纲 JSON，
    结构非法时携带报错回炉一次；生成后执行 verify_facts 数字锚定核对。
  * offline 模式（无 Key / --offline）：规则 + 已审定大纲复用（offline_planner）。
LLM 输出的硬约束（写在 system prompt 里，代码侧再用 schema 校验兜底）：
  * 输出必须是合法 JSON，slide_type 取自白名单；
  * 所有数字必须来自论文原文，不得编造；不确定就写定性描述。
"""
from __future__ import annotations

import re

from ..config import FULLTEXT_TXT, OUTLINE_JSON
from ..coverage import Coverage
from ..llm import LLMClient
from ..models import (OutlineError, save_json,
                      normalize_slide_data, validate_outline)
from .. import offline as offline_mod
from .. import planner as planner_mod
from .state import AgentState

_MAX_RETRY = 2                    # 结构非法/空响应时的回炉次数
_DEFAULT_INPUT_BUDGET = 150000    # 喂给规划模型的论文正文预算（字符）


def _input_budget() -> int:
    """规划输入预算。刻意做成函数：.env 在 import 之后才加载。"""
    import os
    try:
        return int(os.environ.get("P2P_PLANNER_MAX_CHARS",
                                  str(_DEFAULT_INPUT_BUDGET)))
    except ValueError:
        return _DEFAULT_INPUT_BUDGET

#: 四大块**固定**（用户明确要求，不得增删改顺序）：背景 / 方法 / 实验 / 展望和挑战。
#: LLM 只负责为每块生成 2~3 个小标题（内容页）；块名与顺序由 _validate_skeleton
#: 强校验（4 块、按序包含关键词），任何自由划分都会被打回。
_FIXED_BLOCKS: list[str] = ["背景", "方法", "实验", "展望和挑战"]

_BLOCKS: dict[str, list[str]] = {
    "academic_group": list(_FIXED_BLOCKS),
    "conference_talk": list(_FIXED_BLOCKS),
    "defense": list(_FIXED_BLOCKS),
}

_SCENES = {
    "academic_group": "学术组会汇报：固定四块 背景 -> 方法 -> 实验 -> 展望和挑战。",
    "conference_talk": "顶会演讲：固定四块 背景 -> 方法 -> 实验 -> 展望和挑战。",
    "defense": "毕业答辩：固定四块 背景 -> 方法 -> 实验 -> 展望和挑战。",
}


def scene_blocks(scene: str) -> list[str]:
    return list(_BLOCKS.get(scene) or _BLOCKS["academic_group"])


# ---- 第一步：先定小标题（不写内容）----
_SKELETON_SYSTEM = """你是论文转 PPT 流水线的 Planner Agent。现在做**第一步：定结构**。

任务：**大纲固定为四大块**：背景 / 方法 / 实验 / 展望和挑战（顺序固定、
名称固定，不得增删改）。你只需为**每一块**挑选 2~3 个**小标题**（内容页标题，
必须是一句**可判真假的断言**，而不是主题标签）。PPT 就围绕这四块与小标题展开。

**顺序纪律（最重要）**：必须先决定"这一页要证明什么"（小标题），再去找支持它的证据。
**严禁**先看到一段有意思的数据、再倒推一个标题——「标题说『代价』、正文却摆『收益』」
就是顺序颠倒的典型后果，会被评审直接判为内容不成立。

输出 JSON（不要任何多余文字）：
{"paper": {"title": "...", "short": "模型/方法简称", "authors": "...",
           "affiliation": "...", "venue": "会议/期刊", "arxiv": "arXiv 号"},
 "blocks": [{"section": "背景|方法|实验|展望和挑战（按固定四块填）",
             "desc": "本块一句话说明要回答什么",
             "pages": [{"subtitle": "...", "claim": "...", "purpose": "..."}]}]}

硬约束：
1. **恰好 4 块**，section 依次为：背景、方法、实验、展望和挑战（块名可加
   限定词如「方法设计」，但必须含对应关键词且顺序不变）；不要多分章、
   不要合并、不要改成别的四块名称。
2. 每块 2~3 个小标题（内容页）。注意：『实验』块的小标题必须是
   **可判真假的实验结果断言**（如「PatchTST 监督预测 MSE 降 21%」）；
   『方法』块的小标题是**机制断言**（如「Patching 把 token 数从 L 降到约 L/S」）。
3. subtitle ≤ 28 字，中英文混排，保留模型名/数据集名。
   反例（标签）：「Patching 机制」；正例（断言）：「Patching 把 token 数从 L 降到约 L/S」。
4. claim：与 subtitle 同义的可判定表述（可与 subtitle 完全相同）。
5. purpose：Audience move——听众听完这页会改变什么认知（一句话）。
6. **同一块内的小标题不得讲同一件事**（会渲染成两页重复）。
7. 这一步**不要**写 bullets / 表格 / 图，那是第二步的事。"""

# ---- 第二步：围绕已定小标题填内容 ----
_PAGES_SYSTEM = """你是论文转 PPT 流水线的 Planner Agent。现在做**第二步：按已定小标题填内容**。

小标题**已经定好，不得修改、不得增删**。你只为每一个小标题找证据、写内容，
并且所有内容都必须**直接支撑该小标题**——支撑不了的一律不写。

输出 JSON（不要任何多余文字）：
{"pages": [{"subtitle": "<原样抄回>", "claim": "<原样抄回>", "purpose": "<原样抄回>",
            "slide_type": "cover|agenda|bullets|two_col|figure|chart|table|keycards|formula",
            "content_kind": "motivation|concept|mechanism|architecture|data_comparison|data_analysis|table|result_stats|conclusion",
            "bullets": [{"text": "要点", "page": 5}],
            "takeaway": "（可选）一句话总结",
            "table": {"header": [...], "rows": [[...]]},
            "figure": {"kind": "paper_figure", "src": "workspace/assets/xxx.png", "caption": "图注"},
            "formula": "N = ⌊(L − P) / S⌋ + 2",
            "note": "3-5 句可直接照读的中文讲稿"}]}

硬约束：
1. pages 的**数量与小标题数量一致、顺序一致**，subtitle/claim/purpose 原样抄回。
2. **每一条 bullet 都必须能回答「它如何支撑本页小标题」**；回答不了就删掉。
   一页只讲一个论点——不要把两个不相干的论点塞进同一页。
3. 所有数字必须来自我给的论文原文，禁止编造；引用时保留表号/图号（如「（表 3）」）。
4. 写「（表 N）」就必须给 `table` 字段（数据照抄论文）；写「（图 N）」就必须给
   `figure` 的 src。只提编号却不展示，评审会判为「指代不明」。
5. bullets 每条 ≤ 45 字、每页 ≤ 5 条；每条必须带 `page` 字段（论文第几页）。
6. note：口语化、可直接照读，说明"这页要传达什么"。"""


def _normalize_bullets(outline: dict) -> None:
    """统一 bullet 形状：字符串 -> {"text":..,"page":None}；dict 补 page 键。"""
    for s in outline.get("slides", []):
        norm = []
        for b in s.get("bullets", []) or []:
            if isinstance(b, str):
                norm.append({"text": b, "page": None})
            elif isinstance(b, dict):
                page = b.get("page")
                try:
                    page = int(page) if page is not None else None
                except (TypeError, ValueError):
                    page = None
                norm.append({"text": str(b.get("text") or b.get("head", "")),
                             "head": b.get("head", ""), "page": page})
        s["bullets"] = norm


def _fit_pages(pages: list[dict], budget: int) -> tuple[str, dict]:
    """把分页正文压进预算：超预算时**按页等比裁剪**，绝不整页丢弃。

    旧实现是「取头部 39000 字 + 取尾部 6000 字、丢掉中间」，论文中段
    （方法/实验核心）整段消失且日志里只字未提——成品里大量「（表 N）」
    指代却无对应表格，根因就在这里。现在任何一页都保留代表内容，
    并把丢弃量交给 coverage 自检。
    """
    blocks = [f"[第{p.get('page_no')}页]\n{p.get('text', '')}" for p in pages]
    total = sum(len(b) for b in blocks)
    if total <= budget or not blocks:
        return "\n\n".join(blocks), {"total": total, "sent": total, "dropped": []}

    ratio = budget / total
    kept, dropped = [], []
    for page, block in zip(pages, blocks):
        cut = max(300, int(len(block) * ratio))      # 每页保底 300 字
        if cut >= len(block):
            kept.append(block)
            continue
        kept.append(block[:cut] + f"\n…（本页另省略 {len(block) - cut} 字）")
        dropped.append(f"第{page.get('page_no')}页省略{len(block) - cut}字")
    text = "\n\n".join(kept)
    return text, {"total": total, "sent": len(text), "dropped": dropped}


def _build_input(parsed: dict, cov=None) -> str:
    pages = parsed.get("pages", [])
    text, cover = _fit_pages(pages, _input_budget())
    if cov is not None:
        cov.record("planner/论文全文", total=cover["total"], sent=cover["sent"],
                   detail=f"共 {len(pages)} 页；超预算时按页等比裁剪，不整页丢弃",
                   dropped_items=cover["dropped"])
    meta = {
        "title": parsed.get("title", ""),
        "author": parsed.get("author", ""),
        "metadata": parsed.get("metadata", {}),
        "pages": parsed.get("total_pages", 0),
    }
    fig_lines = []
    for f in parsed.get("figures", []):
        fig_lines.append(f"  - Fig{f['fig_no']}（第{f['page']}页）{f['caption'][:80]} -> src={f['path']}")
    fig_block = "\n可用论文原图（直接引用 src 路径，不要再让 Designer 自己重画图表）：\n" + "\n".join(fig_lines)

    # 图表/公式的结构化理解放在全文**之前**：_MAX_INPUT_CHARS 截断的是尾部，
    # 而这是全篇信息密度最高、最该被模型看到的段落。
    vlm_block = ""
    try:
        from ..vlm_parse import digest_for_planner
        digest = digest_for_planner(parsed.get("vlm") or {})
        if digest:
            vlm_block = ("\n\n【图表结构化理解（已核实，可直接引用这里的数值）】\n"
                         + digest
                         + "\n（表内文字数值优先于图内读数；"
                           "图内读数置信度低于 0.8 的不要写进 bullets）")
    except Exception:  # noqa: BLE001
        vlm_block = ""

    return ("论文元信息：\n" + str(meta) + "\n" + fig_block + vlm_block
            + "\n\n========== 论文全文（分页）==========\n\n" + text)


def _json_with_retry(llm: LLMClient, system: str, user: str, *,
                     max_tokens: int = 8192, validate=None, label: str = ""):
    """调 LLM 拿 JSON，失败/校验不过就带着报错回炉。

    validate(obj) 抛异常即视为非法，异常文本回喂给模型让它改。
    实测坑：本供应商 temperature=0.0 会返回空响应，必须用 0.3。
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    last_err = ""
    for _ in range(_MAX_RETRY + 1):
        try:
            result = llm.chat_json(messages, temperature=0.3, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            result = None
        if result is None:
            last_err = last_err or "LLM 未返回合法 JSON"
            messages.append({"role": "user",
                             "content": "输出为空或不是 JSON，请直接输出完整 JSON，不要解释。"})
            continue
        if validate is None:
            return result
        try:
            validate(result)
            return result
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            messages.append({"role": "assistant", "content": str(result)})
            messages.append({"role": "user",
                             "content": f"{label}不合法：{last_err}。请修正后重新输出完整 JSON。"})
    raise RuntimeError(f"Planner Agent：{label or '生成'}失败（{last_err}）")


def _validate_skeleton(result: dict, blocks: list[str]) -> None:
    # 四大块固定强校验：恰好 4 块、顺序固定、块名含对应关键词
    got = [str(b.get("section") or "").strip() for b in (result.get("blocks") or [])]
    if len(got) != 4:
        raise OutlineError(
            f"大纲必须恰好 4 块（背景/方法/实验/展望和挑战），实际 {len(got)} 个：{got}")
    _KEYWORDS = ["背景", "方法", "实验", "展望"]  # 第 4 块含「展望」或「挑战」
    for i, sec in enumerate(got):
        if i == 3:
            ok = ("展望" in sec) or ("挑战" in sec)
        else:
            ok = _KEYWORDS[i] in sec
        if not ok:
            raise OutlineError(
                f"第 {i + 1} 块章节名「{sec}」与固定四大块不符，应为『{_FIXED_BLOCKS[i]}』"
                "（顺序固定：背景→方法→实验→展望和挑战）")
    seen: set[str] = set()
    for b in result["blocks"]:
        sec = str(b.get("section") or "").strip()
        if not sec:
            raise OutlineError("有 block 缺 section")
        if sec in seen:
            raise OutlineError(f"章节名重复：{sec}")
        seen.add(sec)
        pages = b.get("pages") or []
        if not 2 <= len(pages) <= 3:
            raise OutlineError(
                f"章节「{sec}」有 {len(pages)} 个小标题，要求每章 2~3 页")
        for p in pages:
            if not str(p.get("subtitle") or "").strip():
                raise OutlineError(f"章节「{b.get('section')}」有页面缺 subtitle")
            if not str(p.get("purpose") or "").strip():
                raise OutlineError(f"「{p.get('subtitle')}」缺 purpose")


def _plan_skeleton(llm: LLMClient, parsed: dict, scene: str, cov=None) -> dict:
    """第一步：LLM 按论文自主划分 3-5 章 + 每章 2~3 个小标题（不写任何内容）。"""
    blocks = scene_blocks(scene)
    user = (_build_input(parsed, cov=cov)
            + f"\n\n大纲四大块固定（不得增删改顺序）：{blocks}"
            + f"\n叙事参考：{_SCENES.get(scene, _SCENES['academic_group'])}"
            + "\n\n请输出第一步的 JSON：paper + 恰好 4 块（背景/方法/实验/展望和挑战），"
            "每块 2-3 个小标题（只给 subtitle/claim/purpose）。")
    return _json_with_retry(llm, _SKELETON_SYSTEM, user, max_tokens=8192,
                            validate=lambda r: _validate_skeleton(r, blocks),
                            label="结构骨架")


def _plan_block_pages(llm: LLMClient, parsed: dict, block: dict, blocks: list[str],
                      scene: str, cov=None) -> list[dict]:
    """第二步：为**某一块**已定的小标题填内容（块间可并行）。"""
    subs = [{"subtitle": p.get("subtitle"), "claim": p.get("claim"),
             "purpose": p.get("purpose")} for p in (block.get("pages") or [])]

    def _norm(r):
        pages = r.get("pages") or []
        if len(pages) != len(subs):
            raise OutlineError(
                f"pages 数量 {len(pages)} 与小标题数量 {len(subs)} 不一致")
        for got, want in zip(pages, subs):
            if str(got.get("subtitle") or "").strip() != str(want["subtitle"]).strip():
                raise OutlineError(
                    f"subtitle 被改动了：给了「{want['subtitle']}」，"
                    f"返回「{got.get('subtitle')}」。小标题不得修改。")

    user = (_build_input(parsed, cov=cov)
            + f"\n\n所属章节：{block.get('section')}"
            + f"\n章节目标：{block.get('desc', '')}"
            + "\n本块已定的小标题（不得修改/增删，按序填内容）："
            + "\n".join(f"  {i + 1}. {s['subtitle']}  （claim：{s['claim']}）"
                        for i, s in enumerate(subs))
            + "\n\n请输出本块的 pages JSON。")
    res = _json_with_retry(llm, _PAGES_SYSTEM, user, max_tokens=16384,
                           validate=_norm, label=f"章节「{block.get('section')}」内容")
    # 以骨架为准回填，杜绝模型改写小标题
    out = []
    for got, want in zip(res.get("pages") or [], subs):
        page = dict(got)
        page.update(want)
        out.append(page)
    return out


def _plan_with_llm(llm: LLMClient, parsed: dict, env_files=None,
                   scene: str = "academic_group", cov=None) -> dict:
    """两段式规划：先定小标题，再围绕小标题填内容。

    为什么分两步（而不是一次出全）：一次调用里模型会边写内容边改标题，
    于是"标题说代价、正文摆收益"这种错配能一路通过。先把小标题钉死，
    第二步被明确要求"所有内容必须支撑该小标题"，错配在生成时就被挡住——
    这比事后让审查 agent 去改可靠得多（实测事后改会越改越差）。
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    blocks = scene_blocks(scene)
    skeleton = _plan_skeleton(llm, parsed, scene, cov=cov)
    sk_blocks = skeleton.get("blocks") or []
    print(f"[PlannerAgent(LLM)] 结构已定：{len(sk_blocks)} 块 / "
          f"{sum(len(b.get('pages') or []) for b in sk_blocks)} 页小标题")

    # 第二步按块并行（每块一次调用，互不依赖）
    try:
        workers = max(1, int(os.environ.get("P2P_PLANNER_WORKERS", "4")))
    except ValueError:
        workers = 4
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages_by_block = list(ex.map(
            lambda b: _plan_block_pages(llm, parsed, b, blocks, scene, cov=cov),
            sk_blocks))

    outline = {"paper": skeleton.get("paper") or {}, "blocks": []}
    for b, pages in zip(sk_blocks, pages_by_block):
        outline["blocks"].append({"section": b.get("section"),
                                  "desc": b.get("desc", ""), "pages": pages})

    # 展平成 slides（下游零改动），并做结构校验
    from ..models import normalize_outline
    normalize_outline(outline)
    for _s in outline.get("slides") or []:
        if isinstance(_s, dict):
            _s.update(normalize_slide_data(_s))
    _normalize_bullets(outline)
    validate_outline(outline)

    from .. import structure_check
    # 四大块固定，恢复闭集校验：结构性检查（连续性/每块页数/块名/claim/purpose）。
    s_issues = structure_check.structural_issues(outline, blocks)
    structure_check.raise_if_violated(s_issues)

    issues = planner_mod.verify_facts(outline, FULLTEXT_TXT)

    if _rag_enabled():
        from ..rag import fetch_references
        citations = set(re.findall(r"([A-Z][A-Za-z]+(?:\s+et\s+al\.)?,\s*\d{4})",
                                   str(outline)))
        outline["reference_notes"] = fetch_references(
            str(outline["paper"].get("title", "paper"))[:60], list(citations)[:5])
    outline["_blocks"] = blocks
    save_json(outline, OUTLINE_JSON)
    print(f"[PlannerAgent(LLM)] 大纲生成：{len(outline['slides'])} 页 / "
          f"{len(blocks)} 章；事实核对 {len(issues)} 条未命中（需复核）")
    return outline


def _rag_enabled() -> bool:
    import os
    return os.environ.get("P2P_RAG_ENABLED", "0") == "1"


def planner_node(state: AgentState) -> dict:
    parsed = state["parsed"]
    llm = state.get("_llm")  # 由 graph 注入
    logs = list(state.get("logs") or [])

    if llm is not None:
        scene = state.get("scene", "academic_group")
        cov = Coverage(state.get("coverage"))
        outline = _plan_with_llm(llm, parsed, scene=scene, cov=cov)
        logs.append(f"[PlannerAgent(LLM)] 大纲 {len(outline['slides'])} 页（场景={scene}）")
        for r in cov.violations():
            logs.append(f"[Coverage] ⚠ {r['stage']} 丢弃 {r['ratio']:.1%}"
                        f"（{r['sent']}/{r['total']} 字）")
        new_cov = cov.records
    else:
        outline = offline_mod.offline_planner(parsed)
        logs.append(f"[PlannerAgent(offline)] 大纲 {len(outline['slides'])} 页")
        new_cov = state.get("coverage") or []

    return {"outline": outline, "logs": logs, "coverage": new_cov}
