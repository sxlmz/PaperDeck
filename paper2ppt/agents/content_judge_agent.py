# -*- coding: utf-8 -*-
"""content_judge_agent.py：内容质量 Judge —— 只审 LLM 生成的内容（Markdown），
不审渲染后的整页布局（布局归 layout_checker / VLM 那套确定性检查）。

职责（用户拍板，2026-10-04）：
  judge agent 不再对整页 PPT 审核，只审核 LLM 生成的内容，三维度：
  1. 充实度：非标题正文描述总内容必须在 40~400 词之间
     （约束搬自 SlideGen gen_slides_raw_content_v2.txt 的
     "content >= 40 且 <= 400 words"，防止空页/敷衍页，也防止文字溢出页）；
     禁止 1~2 句敷衍式描述。
  2. 书面化：所有描述必须是完整书面语句，禁止口语化 / 电报式碎片 /
     占位式空话（"详见上图"、"这部分很重要"之类）。
  3. 相关性：正文必须兑现页面标题与 purpose 的承诺，不得跑题；
     标题承诺什么，正文必须展开什么。

闭环（接在 html_author._gen_one 里）：
  LLM 生成 Markdown -> judge_content 审查 -> 不合格带具体 issue 反馈
  -> Author 重写（上限 2 次，防死循环）-> 通过后才 render_markdown 渲染模板。

程序硬闸门 word_count()：不依赖 LLM 自评的确定性计数（防自评作弊）——
  CJK 每字符计 1 词 + 连续英文/数字串每串计 1 词（SlideGen 英文词数的中文近似）；
  judge 返回的 LLM 词数仅作参考，最终以程序计数为准。
"""
from __future__ import annotations

import re
from typing import Any

#: 密度约束（SlideGen 搬入）：非标题正文总词数下限/上限
MIN_WORDS = 40
MAX_WORDS = 400

#: 正文提取时要剥掉的内容模式
_TITLE_RE = re.compile(r"^\s*#{1,6}\s+.*$", re.M)         # 标题行
_FRONT_RE = re.compile(r"^---\s*$.*?^---\s*$", re.S | re.M)  # front-matter
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")              # 图片引用
_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)            # 表格行（管道表）
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)            # 代码块
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z0-9]+")

_JUDGE_SYSTEM = """你是学术 PPT 内容质量审查官。你只审查作者为**一页幻灯片**生成的
内容 Markdown（尚未渲染成页面），**只评判内容质量，不评判布局/视觉/排版**。

评判标准（三项全部满足才算通过）：
1. 充实度：非标题正文描述的总词数必须在 40~400 词之间（中文按"每个汉字 1 词"
   近似，英文按单词计）。一页正文若只有 1~2 句话、或明显敷衍（如"详见原文"、
   "该部分内容重要"这类空话），判不通过；若超过 400 词则信息过载，判不通过。
2. 书面化：所有描述必须是完整、通顺的书面语句（学术书面语），禁止口语化、
   电报式短语、碎片化罗列、占位式空话。
3. 相关性：正文内容必须兑现"页面标题"与"本页 purpose"的承诺，不得跑题；
   标题承诺什么，正文必须展开什么。要点与标题无关、答非所问判不通过。

输出 JSON（不要输出任何其他文字）：
{"pass": true|false, "word_count": 估算词数, "issues": ["...", "..."]}
- pass=true 时 issues 为空数组；
- pass=false 时 issues 必须给 1~3 条**具体、可执行**的修改意见，
  指明是哪一条内容不合格、为什么、怎么改（例如"第 2 条要点只有 8 个字，
  请展开为完整的书面语句说明 Patch 划分的具体做法"）。"""


def _strip_for_count(md: str) -> str:
    """把 Markdown 剥成"纯正文文本"（去掉标题/图/表/代码/front-matter）。"""
    t = str(md or "")
    t = _CODE_FENCE_RE.sub(" ", t)
    t = _FRONT_RE.sub(" ", t)
    t = _TITLE_RE.sub(" ", t)
    t = _IMG_RE.sub(" ", t)
    t = _TABLE_RE.sub(" ", t)
    # 去掉 Markdown 强调符号、行首列表符（保留文字本身）
    t = re.sub(r"[*_>`~]", "", t)
    t = re.sub(r"^\s*[-•]\s*", "", t, flags=re.M)
    return t


def word_count(md: str) -> int:
    """正文词数（确定性硬闸门）：CJK 每字符 1 词 + 连续英文/数字串每串 1 词。

    注意这是"正文描述"的词数：标题、图引用、表格单元格都不计入（表格是
    数据展示而非描述）。要点、lead、说明文字计入。
    """
    t = _strip_for_count(md)
    cjk = len(_CJK_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    return cjk + latin


def _judge_prompt(title: str, section: str, purpose: str, md: str) -> list[dict]:
    user = (
        f"页面标题：{title or '（无）'}\n"
        f"所属章节：{section or '（无）'}\n"
        f"本页 purpose（必须兑现）：{purpose or '（无）'}\n"
        f"作者生成的内容 Markdown（待审查）：\n"
        f"-----\n{md}\n-----\n\n"
        f"按三条标准审查并输出 JSON。"
    )
    return [{"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user}]


def judge_content(llm, title: str, section: str, purpose: str, md: str,
                  *, use_cache: bool = True) -> dict[str, Any]:
    """LLM 审查 + 程序词数硬闸门，返回统一判定。

    返回 {"pass": bool, "word_count": int, "issues": list[str],
          "judge_note": str}。
    pass=False 时 issues 一定非空（LLM 给的具体意见；程序闸门不过时给程序意见）。
    """
    wc = word_count(md)
    issues: list[str] = []
    # 程序硬闸门先行：LLM 自评可能"粉饰太平"，词数必须由确定性计数把关。
    if wc < MIN_WORDS:
        issues.append(
            f"正文词数 {wc} < 下限 {MIN_WORDS}：内容太简略。请把本页要点/说明"
            f"扩写为完整的书面语句，讲清方法与结论（目标 {MIN_WORDS}~{MAX_WORDS} 词）。")
    elif wc > MAX_WORDS:
        issues.append(
            f"正文词数 {wc} > 上限 {MAX_WORDS}：信息过载会溢出卡片。请删减次要细节，"
            f"把正文收敛到 {MIN_WORDS}~{MAX_WORDS} 词。")
    # LLM 审查（书面化/相关性/质感）——LLM 为空（offline）时跳过
    llm_issues: list[str] = []
    judge_note = ""
    if llm is not None:
        try:
            # 2026-10-04 用户要求：judge 采用 qwen3.7-flash（走 llm.judge_json，
            # 复用 DashScope 兼容端点的评估客户端，与 VLM 审查同模型同端点），
            # 不再用主 LLM（deepseek）。未实现时回退 chat_json 主模型。
            jcall = getattr(llm, "judge_json", None)
            if callable(jcall):
                res = jcall(_judge_prompt(title, section, purpose, md),
                            temperature=0.1, max_tokens=1024,
                            use_cache=use_cache)
            else:
                res = llm.chat_json(_judge_prompt(title, section, purpose, md),
                                    temperature=0.1, max_tokens=1024,
                                    use_cache=use_cache)
            if isinstance(res, dict):
                if not res.get("pass", False):
                    for it in res.get("issues") or []:
                        if isinstance(it, str) and it.strip():
                            llm_issues.append(it.strip())
                judge_note = (f"LLM 判定 {'通过' if res.get('pass') else '不通过'}"
                              f"（自评词数 {res.get('word_count', '?')}）")
            else:
                judge_note = "LLM 审查返回非 dict，忽略"
        except Exception as e:  # noqa: BLE001  审查失败不应阻断生成，降级为仅程序闸门
            judge_note = f"LLM 审查失败降级（{e}），仅按程序词数判定"
    else:
        judge_note = "offline 模式：仅按程序词数判定"

    issues.extend(llm_issues)
    passed = wc >= MIN_WORDS and wc <= MAX_WORDS and not llm_issues
    return {"pass": passed, "word_count": wc,
            "issues": issues, "judge_note": judge_note}
