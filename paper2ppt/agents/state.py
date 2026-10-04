# -*- coding: utf-8 -*-
"""LangGraph State：四个 Agent 之间传递的结构化中间态。

State 即流水线的"工作台"：每个节点只增改自己负责的字段，
后续节点依赖前面节点产出的 JSON 资产（内容与表现分离的载体）。
"""
from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # 输入
    pdf_path: str
    out_pptx: str
    env_files: list[str]
    force_llm: bool          # True=必须用 LLM；False=自动回退 offline
    max_critic_rounds: int
    scene: str               # 叙事骨架：academic_group/conference_talk/defense
    _llm: Any                # 由入口注入的 LLMClient（不参与状态语义）
    # Parser Agent 产出
    parsed: dict[str, Any]
    # Planner Agent 产出
    outline: dict[str, Any]
    outline_issues: list[str]
    # Designer Agent 产出
    decisions: dict[str, dict[str, str]]
    design: dict[str, Any]
    # Renderer Agent 产出
    pptx_path: str
    # Critic Agent 产出（审查闭环）
    critic_rounds: int
    max_critic_rounds: int
    issues: list[dict[str, Any]]
    critic_report: str
    needs_render: bool
    logs: list[str]
    # 审查闭环的单调守卫：记录目前问题数最少的那一版 design，
    # 一旦某轮改完问题反而变多，就回滚到它并停止
    _best_design: dict[str, Any]
    _best_issue_count: int
    _finalized: bool         # 已回滚，本次渲染后直接出终稿（不再修订）
    _prev_issues: list[dict[str, Any]]   # 上一轮的问题清单（逐条比对用）
    # 大纲级内容一致性判定（coherence_agent，只报不改）
    coherence_issues: list[dict[str, Any]]
    # 内容覆盖自检（见 paper2ppt/coverage.py）：记录每处「应送入 vs 实际送入」，
    # 任何截断都必须留痕，不允许静默发生
    coverage: list[dict[str, Any]]
    # 入口开关
    use_vlm: bool            # False = 跳过关键页 VLM 解析
    reparse: bool            # True = 无视缓存重新解析论文
    use_cache: bool          # False = 关闭 Redis 缓存（--no-cache）
    refresh: bool            # True = 跳过缓存读取但仍写回（--refresh-cache）
    from_stage: str          # --from：从哪一步开始（回填前面步骤的产物）
    # HTML 真源模式（完整版方案，P2P_HTML_SOURCE=1）
    _html_dir: str           # html_author 输出目录（html_source/）
    _rag_search: Any         # 注入的 rag_search 函数（Router 缺数据分支用）
    _html_fixed: list[int]   # Router 本轮改过的页号
    _rag_evidence: dict[int, str]  # 每页附上的 RAG 证据（审计用）
    factuality_issues: list[dict]  # 待 faithfulness 裁决的 issue
