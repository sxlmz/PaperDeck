# -*- coding: utf-8 -*-
"""LangGraph 图组装：多 Agent 流水线 + 审查闭环条件边。

HTML 真源模式（唯一路线，2026-10-03 起 python-pptx 路径 A 已删除）：
    parser -> understand -> planner -> faithfulness -> coherence
    -> html_author -> html_renderer -> html_critic -> router ->(重渲染回环 | END)

Router 三桶分发（确定性规则，见 paper2ppt/router.py）：
  missing_data -> RAG 检索论文原文 chunk -> HTML Fixer 落进页面
  layout       -> HTML Fixer 直接改 HTML 源码（自由 CSS）
  factuality   -> 交给 Faithfulness 裁决（不阻断本轮渲染）

审查闭环纪律：max_critic_rounds 上限 + 单调守卫（问题数不低于历史最优
则回滚最优版本），防止 VLM 迭代"越改越差"。

转换层：html_renderer 节点调用 html2pptx.convert_html_to_pptx，
内部走 npm dom-to-pptx（浏览器直出）+ zip 级合并，无 python-pptx。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .coherence_agent import coherence_node
from .faithfulness_agent import faithfulness_node
from .html_author_agent import html_author_node
from .html_critic_agent import html_critic_node
from .html_renderer_agent import html_renderer_node
from .parser_agent import parser_node
from .planner_agent import planner_node
from .router_agent import router_node
from .state import AgentState
from .understand_agent import understand_node

#: HTML 真源流水线：线性段 + 审查环（可选起始节点 --from，顺序即流水线顺序）
STAGES = ["parser", "understand", "planner", "faithfulness",
          "coherence", "html_author", "html_renderer", "html_critic"]


def _route_after_html_critic(state: AgentState) -> str:
    return "router" if state.get("needs_render") else "end"


def _route_after_router(state: AgentState) -> str:
    # Router 分发后：有页面被修复则重渲染重审，否则直接出终稿（避免死循环）
    return "html_renderer" if state.get("needs_render") else "end"


def build_graph(start_node: str = "parser") -> StateGraph:
    """组装 HTML 真源流水线图。"""
    if start_node not in STAGES:
        raise ValueError(f"未知起始节点 {start_node!r}，可选：{STAGES}")

    g = StateGraph(AgentState)
    g.add_node("parser", parser_node)
    g.add_node("understand", understand_node)
    g.add_node("planner", planner_node)
    g.add_node("faithfulness", faithfulness_node)
    g.add_node("coherence", coherence_node)
    g.add_node("html_author", html_author_node)
    g.add_node("html_renderer", html_renderer_node)
    g.add_node("html_critic", html_critic_node)
    g.add_node("router", router_node)

    g.add_edge(START, start_node)
    linear = [("parser", "understand"), ("understand", "planner"),
              ("planner", "faithfulness"),
              ("faithfulness", "coherence"),
              ("coherence", "html_author"),
              ("html_author", "html_renderer")]
    started = False
    for src, dst in linear:
        if src == start_node:
            started = True
        if started:
            g.add_edge(src, dst)

    # 审查闭环的回路必须**无条件**加上：它属于循环，不属于线性序列。
    # html_renderer -> html_critic 也在循环里：--from html_critic 时 linear 循环
    # 因 src 没有 html_critic 而 started 恒 False，这条边会丢，导致 Router 修复
    # 后重渲染直接 END、不复查（2026-10-04 实测第 2 轮缺失）。
    g.add_edge("html_renderer", "html_critic")
    g.add_edge("router", "html_renderer")
    g.add_conditional_edges("html_critic", _route_after_html_critic,
                            {"router": "router", "end": END})
    g.add_conditional_edges("router", _route_after_router,
                            {"html_renderer": "html_renderer", "end": END})
    return g.compile()
