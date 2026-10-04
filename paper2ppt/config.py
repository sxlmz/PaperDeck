# -*- coding: utf-8 -*-
"""全局配置：路径、主题（配色/字体）、幻灯片尺寸。

2026-10-03 起转换层为 dom-to-pptx（浏览器直出），不再使用 python-pptx：
RGBColor/Inches 等 pptx 专用类型已移除；幻灯片尺寸保留数值口径
（HTML 画布 1280x720px -> 16:9，英寸 10"x5.625" 由转换脚本负责）。
"""
from pathlib import Path

# ---------- 路径 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPER_DIR = PROJECT_ROOT / "paper"
WORKSPACE = PROJECT_ROOT / "workspace"
ASSETS_DIR = WORKSPACE / "assets"          # 从论文提取的图
CHARTS_DIR = WORKSPACE / "charts"          # 生成的图表
OUTPUT_DIR = PROJECT_ROOT / "output"
PREVIEW_DIR = WORKSPACE / "preview"        # PDF 预览/截图

for _d in (WORKSPACE, ASSETS_DIR, CHARTS_DIR, OUTPUT_DIR, PREVIEW_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PARSED_JSON = WORKSPACE / "parsed_paper.json"
OUTLINE_JSON = WORKSPACE / "outline.json"
DESIGN_JSON = WORKSPACE / "design.json"
DEFAULT_OUT = OUTPUT_DIR / "deck.pptx"
FULLTEXT_TXT = WORKSPACE / "paper_fulltext.txt"

# ---------- 幻灯片 ----------
# HTML 真源画布 1280x720（模板统一），dom-to-pptx 转 10"x5.625" 16:9
SLIDE_W = 1280
SLIDE_H = 720

# ---------- 主题（学术蓝 + 橙色强调；HTML/CSS 层用 hex 字符串） ----------
PRIMARY_HEX = "#1F3864"      # 深蓝（标题/封面）
CHART_BLUE = "#1F3864"
CHART_MID = "#2E75B6"
CHART_ORANGE = "#ED7D31"
CHART_GRAY = "#8FAADC"

FONT_NAME = "微软雅黑"
FONT_LATIN = "Microsoft YaHei"

# matplotlib 中文字体路径（Windows）
CJK_FONT_PATHS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]

# 封面元信息兜底：论文元信息缺失时用它填坑，避免 planner 少给一个键就崩首屏。
# 真正的值来自 outline["paper"]（由 Planner Agent 从论文里抽取）。
DECK_FALLBACK = {
    "title": "论文汇报",
    "short": "",
    "authors": "",
    "affiliation": "",
    "venue": "",
    "arxiv": "",
}

# ---------- Agent 系统配置（环境变量，均为可选） ----------
# LLM 优先级：P2P_LLM_* > LangGraph 单模型(SINGLE_*) > DEEPSEEK_* > OPENAI_*
# 未配置任何 Key 时自动进入 offline 确定性 Agent 模式（不联网）。
DEFAULT_ENV_FILES = [
    PROJECT_ROOT / ".env",   # 本项目配置（优先，含 MinerU/视觉/缓存）
]


def mineru_config() -> dict:
    """MinerU 高保真解析后端配置（可选）。

    刻意做成**函数**而不是模块级常量：.env 是在 import config 之后才加载的，
    在 import 期读环境变量会拿到空值。
    """
    import os
    try:
        page_limit = int(os.environ.get("P2P_MINERU_PAGE_LIMIT", "200"))
    except ValueError:
        page_limit = 200
    return {
        "api_key": (os.environ.get("P2P_MINERU_API_KEY")
                    or os.environ.get("MINERU_API_KEY", "")),
        "base_url": os.environ.get("P2P_MINERU_BASE_URL", "https://mineru.net/api/v4"),
        "model_version": os.environ.get("P2P_MINERU_MODEL_VERSION", "vlm"),
        "enabled": os.environ.get("P2P_MINERU_ENABLED", "0") == "1",
        "page_limit": page_limit,
    }

