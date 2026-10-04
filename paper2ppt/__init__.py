# -*- coding: utf-8 -*-
"""
paper2ppt —— 论文转 PPT 多阶段 Agent 流水线（HTML 真源唯一路线）。

阶段（2026-10-03 起，python-pptx 路径 A 已删除）：
  Parser      : PDF/LaTeX -> 结构化学术资产 (parsed_paper.json)
  Understand  : 论文理解 -> 覆盖率基线与要点
  Planner     : 学术资产   -> 演讲大纲     (outline.json)
  Faithfulness: 大纲逐条证据校验（可溯源）
  Coherence   : 叙事链一致性检查
  HtmlAuthor  : 大纲 -> 按模板填内容生成每页 HTML 源码 (html_source/)
  HtmlRenderer: HTML -> PPTX（npm dom-to-pptx 浏览器直出 + zip 级合并）
  HtmlCritic  : 确定性布局检查 + VLM 并行看图 -> issues
  Router      : 按问题桶分发（布局->Fixer 改 HTML / 缺数据->RAG / 事实->Faithfulness）

设计要点：
  * HTML 为真源、模板中心化：LLM 只选模板填内容，布局由模板 CSS 保证。
  * 审查闭环：确定性闸门（零 token）+ VLM 看图 + 单调守卫回滚，最多 N 轮。
"""

__version__ = "0.1.0"
