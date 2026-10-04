# -*- coding: utf-8 -*-
r"""论文 -> PPT 智能体系统入口。

用法：
    python run_agent.py                          # 自动模式（有 LLM Key 用 LLM Agent，否则 offline）
    python run_agent.py --llm                    # 强制 LLM Agent（无 Key 会报错退出）
    python run_agent.py --offline                # 强制 offline 规则 Agent
    python run_agent.py --pdf paper/xxx.pdf      # 指定论文
    python run_agent.py --out output/xxx.pptx    # 指定输出
    python run_agent.py --max-critic 3           # Critic 最多审查轮数
    python run_agent.py --scene defense          # 叙事骨架：academic_group/conference_talk/defense
    python run_agent.py --env-file D:\...\.env   # 额外加载环境变量文件

只跑一部分（配合工作区里已有的中间态 JSON，重渲染从分钟级降到秒级）：
    python run_agent.py --from html_author --offline   # 只重做大纲后的 HTML 编排
    python run_agent.py --from html_renderer --llm     # 只重渲染（HTML -> PPTX）
    python run_agent.py --from html_critic --llm       # 只重跑审查

缓存（Redis db2 / p2p: 前缀，与其它业务缓存隔离）：
    python run_agent.py --no-cache                  # 本次不使用缓存
    python run_agent.py --refresh-cache llm         # 丢弃 LLM 缓存后重跑
    python run_agent.py --cache-stats               # 查看缓存统计后退出
    python run_agent.py --reparse                   # 无视解析缓存，重新解析并更新缓存

流程图（LangGraph StateGraph，HTML 真源唯一路线）：
    parser -> understand -> planner -> faithfulness -> coherence
    -> html_author -> html_renderer -> html_critic -> router
                                   ^                        |
                                   └────(needs_render)──────┘

转换层：html2pptx.py 内部走 npm dom-to-pptx（浏览器直出）+ zip 级合并，
无 python-pptx（表格/图片按 CSS 直转，不经过默认主题样式）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from paper2ppt.config import (DEFAULT_ENV_FILES, DEFAULT_OUT, DESIGN_JSON,
                              OUTLINE_JSON, PARSED_JSON)
from paper2ppt.llm import get_llm, load_dotenv_files

#: --from 的取值（与 agents.graph.STAGES 对应，HTML 真源唯一路线）
_STAGES = ["parser", "understand", "planner", "faithfulness",
           "coherence", "html_author", "html_renderer", "html_critic"]

#: --from <阶段> 需要预先具备的工作区产物
_SEED_FILES = {
    "understand": [PARSED_JSON],
    "planner": [PARSED_JSON],
    "faithfulness": [PARSED_JSON, OUTLINE_JSON],
    "html_author": [PARSED_JSON, OUTLINE_JSON],
    "html_renderer": [PARSED_JSON, OUTLINE_JSON],
    "html_critic": [PARSED_JSON, OUTLINE_JSON],
}
_KEY_OF = {str(PARSED_JSON): "parsed", str(OUTLINE_JSON): "outline",
           str(DESIGN_JSON): "design"}


def _seed_state(stage: str, out_pptx: Path) -> dict:
    """--from：把该阶段之前的产物从工作区回填进 state。"""
    from paper2ppt.models import load_json

    seed: dict = {}
    files = _SEED_FILES.get(stage, [])
    if not files and stage in _STAGES:
        # HTML 模式的中间阶段都复用同一批上游产物（parsed + outline）。
        files = _SEED_FILES["html_author"]
    for path in files:
        p = Path(path)
        if not p.exists():
            sys.exit(f"[entry] --from {stage} 需要 {p.name}，但工作区里没有；"
                     f"请先完整跑一次")
        seed[_KEY_OF[str(path)]] = load_json(p)
        print(f"[entry] 回填 {p.name}")
    return seed


def main() -> int:
    ap = argparse.ArgumentParser(description="论文转 PPT 智能体系统")
    ap.add_argument("--pdf", default="", help="论文 PDF 路径（缺省自动取 paper/ 下第一份）")
    ap.add_argument("--out", default="", help="输出 .pptx 路径")
    ap.add_argument("--llm", action="store_true", help="强制使用 LLM Agent")
    ap.add_argument("--offline", action="store_true", help="强制使用 offline 规则 Agent")
    ap.add_argument("--max-critic", type=int, default=2, help="Critic 最大审查轮数（默认 2）")
    ap.add_argument("--no-critic", action="store_true",
                    help="完全跳过 VLM/确定性审查，渲染后直接出终稿（先看模板效果）")
    ap.add_argument("--scene", default="academic_group",
                    choices=["academic_group", "conference_talk", "defense"],
                    help="叙事骨架（默认学术组会）")
    ap.add_argument("--from", dest="from_stage", default="parser",
                    help="从哪一步开始跑（回填之前的产物；HTML 模式可选 "
                         "html_author/html_renderer/html_critic）")
    ap.add_argument("--env-file", action="append", default=[],
                    help="额外 .env 文件（可多次），默认自动加载项目 .env（P2P_LLM_* / SINGLE_* / DEEPSEEK_* 优先级）")
    ap.add_argument("--no-cache", action="store_true", help="本次不使用 Redis 缓存")
    ap.add_argument("--refresh-cache", default="",
                    choices=["", "all", "parser", "llm", "vlm"],
                    help="先失效指定缓存再运行")
    ap.add_argument("--cache-stats", action="store_true", help="打印缓存统计后退出")
    ap.add_argument("--no-vlm", action="store_true", help="跳过关键页 VLM 解析")
    ap.add_argument("--reparse", action="store_true", help="无视解析缓存，重新解析论文")
    args = ap.parse_args()

    if args.cache_stats:
        from paper2ppt import cache
        import json as _json
        print(_json.dumps(cache.stats(), ensure_ascii=False, indent=2))
        return 0

    if args.refresh_cache:
        from paper2ppt import cache
        kind = args.refresh_cache
        n = cache.invalidate(drop_llm=kind in ("all", "llm"),
                             drop_vlm=kind in ("all", "vlm"))
        print(f"[entry] 已失效 {kind} 缓存（{n} 个键）")

    env_files = [Path(f) for f in args.env_file] + DEFAULT_ENV_FILES
    load_dotenv_files(env_files)

    llm = None if args.offline else get_llm(env_files)
    if llm is not None:
        print(f"[entry] LLM Agent 模式：{llm.cfg.model} @ {llm.cfg.base_url or '默认'}"
              + (f"；视觉 {llm.cfg.vision_model}" if llm.cfg.vision_model else "；无视觉模型"))
    elif args.llm:
        print("[entry] 错误：--llm 要求配置 API Key（P2P_LLM_*/SINGLE_*/DEEPSEEK_API_KEY/OPENAI_API_KEY）")
        return 2
    elif args.offline:
        print("[entry] 已按 --offline 使用规则 Agent（不调用任何 LLM）")
    else:
        print("[entry] 未检测到 LLM Key，切换到 offline 规则 Agent 模式"
              "（配置 .env 里的 P2P_LLM_API_KEY 即可启用 LLM Agent）")

    out_pptx = Path(args.out or DEFAULT_OUT)

    # HTML 真源唯一路线：跳过审查则 P2P_SKIP_CRITIC=1
    if args.no_critic:
        os.environ["P2P_SKIP_CRITIC"] = "1"
        print("[entry] --no-critic：跳过全部审查，渲染后直接出终稿")
    if args.from_stage not in _STAGES:
        print(f"[entry] 错误：--from {args.from_stage} 不是合法阶段，可选：{_STAGES}")
        return 2

    from paper2ppt.agents import build_graph

    graph = build_graph(args.from_stage)
    initial = {
        "pdf_path": args.pdf,
        "out_pptx": str(out_pptx),
        "max_critic_rounds": args.max_critic,
        "scene": args.scene,
        "use_vlm": not args.no_vlm,
        "reparse": args.reparse,
        "use_cache": not args.no_cache,
        "refresh": bool(args.refresh_cache),
        "_llm": llm,
    }
    if args.from_stage != "parser":
        initial.update(_seed_state(args.from_stage, out_pptx))

    try:
        result = graph.invoke(initial, config={"recursion_limit": 32})
    except Exception as e:  # noqa: BLE001
        # 节点主动抛出的失败（结构非法、路径 B 无 Key…）不该以裸 traceback 收场：
        # 打印可读原因并以非零码退出，脚本/CI 才能判定这次运行是失败的。
        print(f"\n[entry] 流水线失败：{type(e).__name__}: {e}")
        return 1

    print("\n========== 执行日志 ==========")
    for line in result.get("logs", []):
        print(line)

    # 内容覆盖自检：任何截断都必须显式可见（见 paper2ppt/coverage.py）
    from paper2ppt.coverage import Coverage
    cov = Coverage(result.get("coverage") or [])
    print("\n========== 内容覆盖自检 ==========")
    print(cov.report())
    if os.environ.get("P2P_STRICT") == "1":
        cov.raise_if_violated()

    print("\n========== Critic 审查报告 ==========")
    print(result.get("critic_report", "(未执行审查)"))

    # 收尾断言：图跑完却没有 pptx，就是失败——不能"成功退出但什么都没产出"
    # （路径 B 少一个节点短路就会走到这里，见 html_author 的失败要响）
    produced = str(result.get("pptx_path") or "").strip()
    if not produced or not Path(produced).exists():
        print(f"\n[entry] 错误：流水线结束但未产出 .pptx"
              f"（pptx_path={produced or '空'}）。请检查上方日志里的失败节点。")
        return 1
    print("\n产物:", produced)
    return 0


if __name__ == "__main__":
    sys.exit(main())
