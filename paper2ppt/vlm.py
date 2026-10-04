# -*- coding: utf-8 -*-
"""vlm.py：VLM 视觉审查独立模块（HTML 真源路线专用，无 python-pptx 依赖）。

从旧路径 A 的 critic_agent.py 迁移而来：看一页截图 -> 结构化审查 JSON。
审查纪律与模板约束 prompt 保持原样（用户多次校准过的口径）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .llm import LLMClient

#: VLM 审查系统 prompt（2026-10-04 收窄：只查文字溢出；必须定位溢出源文字）
_VLM_SYSTEM = """你是 PPT 视觉审查专家（VLM）。像资深设计审稿人一样看这张幻灯片截图。

**本阶段只检查一类问题：严重文字溢出** —— 文字多到溢出所在容器/卡片、
被裁剪、换行错乱、与相邻元素明显挤压、内容不可读。**其他一律不检查、
不报告**：配色、版面平衡、美观度、图表大小、字体大小观感、模板匹配、
页脚页码、内容与论文是否一致等，全都不要提。

**严重溢出的判定标准**：只有文字真的溢出容器边界（被裁掉、看不清、
压到其他元素、超出卡片/画布）才算。**轻微情况（差一两像素、接近边界
但内容完整可读）一律不算、不要报告**。

**报告要求**：
- issues 只允许 type=overflow，且 severity 只允许 "high"（严重才报）；
- 每条必须指明是哪一段文字导致的溢出：desc 里截取该段文字原文（前 80 字），
  并给处理建议——「压缩该段文字」或「拆分为两条要点」，二选一；
- region 用归一化坐标（整页左上角为 0,0，右下角为 1,1；x/y 是区域左上角，
  w/h 是宽高），尽量贴近溢出的那块区域；
- 没有严重溢出时 ok=true、issues=[]。

只输出 JSON：
{"ok": true|false,
 "template": "本页当前使用的模板 ID（判断不出就给 content）",
 "issues": [{"type":"overflow",
             "desc":"指出哪段文字（截取原文前 80 字）严重溢出、在哪个位置、
                    建议：压缩该段文字或拆分为两条要点",
             "region":{"x":0.0,"y":0.0,"w":0.0,"h":0.0},
             "severity":"high"}],
 "advice": "一句具体可执行的修改建议"}"""
#: 画布尺寸（英寸），把 VLM 的归一化 region 换算成精确坐标用
_SLIDE_W_IN, _SLIDE_H_IN = 13.333, 7.5


def _region_to_inches(region) -> list[float] | None:
    """把 VLM 给的归一化 region 换算成英寸 [x, y, w, h]（确定性换算）。"""
    if not isinstance(region, dict):
        return None
    try:
        x = float(region.get("x", 0)); y = float(region.get("y", 0))
        w = float(region.get("w", 0)); h = float(region.get("h", 0))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    # 容忍模型给出越界值
    x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
    w, h = max(0.0, min(1.0 - x, w)), max(0.0, min(1.0 - y, h))
    return [round(x * _SLIDE_W_IN, 2), round(y * _SLIDE_H_IN, 2),
            round(w * _SLIDE_W_IN, 2), round(h * _SLIDE_H_IN, 2)]


def _run_vlm_slide(llm: LLMClient, png: Path, context: str = "") -> dict:
    """看一页截图并输出结构化审查结果（走 vision_json，结果按图缓存）。"""
    instr = "这是某页幻灯片截图，请审查并输出 JSON。"
    if context:
        instr += ("\n\n本页**应当**讲的内容（论文原文摘录，用来判断图文是否相符）：\n"
                  + context)
    got = llm.vision_json(png, instr, task="review", prompt_version=6,
                          system=_VLM_SYSTEM, max_tokens=900)
    return got if isinstance(got, dict) else {"ok": True, "issues": []}


def _run_one_png(args):
    llm, png, ctx = args
    try:
        return png, _run_vlm_slide(llm, png, ctx)
    except Exception as e:  # noqa: BLE001
        print(f"[VLM] 审查失败（跳过）{png.name}: {e}")
        return png, None


def _run_vlm(llm, pngs: list[Path], issues: list[dict], ctx: dict[int, str] | None = None):
    """并行审查多页截图（用户明确要求每页并行，不串行）。"""
    out = list(issues)
    advices = []
    ctx = ctx or {}
    targets = pngs
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_run_one_png,
                              [(llm, p, ctx.get(int(p.stem.split("_")[-1]), ""))
                               for p in targets]))
    for png, res in results:
        if res is None:
            continue
        slide_no = int(png.stem.split("_")[-1])
        for it in (res.get("issues") or []):
            if isinstance(it, dict) and it.get("desc"):
                entry = {"slide": slide_no,
                         "type": "vlm:" + str(it.get("type", "review")),
                         "desc": str(it["desc"]),
                         "severity": str(it.get("severity", ""))}
                box = _region_to_inches(it.get("region"))
                if box:
                    # 模型给的是归一化区域，这里换算成精确英寸坐标，
                    # 下游修订就能拿到「在哪儿、多大」而不是只有一句话
                    entry["box_in"] = box
                    entry["desc"] += (f"（位置 x={box[0]}, y={box[1]}, "
                                      f"宽 {box[2]}, 高 {box[3]} 英寸）")
                out.append(entry)
        adv = str(res.get("advice", "")).strip()
        if adv and adv.lower() not in ("ok", "none"):
            advices.append(f"第{slide_no}页: {adv}")
    return out, advices
