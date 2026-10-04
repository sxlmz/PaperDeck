# -*- coding: utf-8 -*-
"""rag_search.py：论文内部 RAG 在线检索（审查反馈 -> query -> 混合检索）。

与 RAG-test 的检索侧同构（rag/nlp/query.py 的 query 改写 + Infinity 的
BM25/余弦混合），但全部本地实现、不 import RAG-test：
  1. query 改写：全角转半角、繁转简、剥离特殊字符、去除客套词
     （rag_tokenizer.strQ2B / tradi2simp / rmWWW 同款语义）；
  2. 双通道检索：BM25（rank_bm25）+ 余弦（numpy 内积，bge-m3 向量）；
  3. 融合：weighted_sum("0.001,1") —— BM25 分归一化后乘 0.001、余弦原值，
     与 RAG-test 的融合权重一致；
  4. 精排：融合分排序 + 去重（同页同 kind 只留最高分）+ 相关性阈值过滤，
     再按页码邻域聚簇归并（同一页多个 chunk 合并成一条证据，避免 PPT 修复
     时只拿到半张表/半段方法）。

对外入口：search(query, top_k) -> [evidence, ...]
    evidence: {chunk_id, page, kind, score, text, html?, fig_path?, caption?}
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from .rag_index import (RAG_INDEX_DIR, _bm25_tokenize, build_bm25,
                        embed_texts, load_chunks, load_meta, load_vectors)

#: BM25 与余弦的融合权重（同 RAG-test weighted_sum "0.001,1"）
W_BM25 = 0.001
W_COS = 1.0

#: 相关性阈值（余弦）：低于此值的证据丢弃。bge-m3 对同文档召回通常 >0.4
COS_THRESHOLD = 0.30

#: 客套/提问前缀剥离（RAG-test rmWWW 的缩减版，够用即可）
_POLITENESS = re.compile(
    r"(请问|想问下|想知道|帮我|麻烦|请告诉我|告诉我|请给我|给我|"
    r"查一下|看看|查下|能否|可否|能不能|可以告诉我|可以给我|"
    r"谢谢|感谢|多谢)[，。,.!！?？\s]*")

#: 待剥离的特殊字符（Infinity search lexer 的可转义字符集）
_SPECIAL = re.compile(r'[ :|\r\n\t,，。？?/`!！&^%()\[\]{}<>*~"\'\\]+')


# ---------------------------------------------------------------- query 改写
def _str_q2b(text: str) -> str:
    """全角 -> 半角（strQ2B 同款）。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _tradi2simp(text: str) -> str:
    """繁转简：只做公共区映射（不引入 opencc 依赖）。

    论文多为英文 + 简体；繁体块极少。够用即可，缺失映射不影响检索。
    """
    table = {
        "認": "认", "識": "识", "圖": "图", "表": "表", "據": "据",
        "數": "数", "據": "据", "實": "实", "驗": "验", "證": "证",
        "樣": "样", "體": "体", "機": "机", "製": "制", "準": "准",
        "標": "标", "結": "结", "論": "论", "導": "导", "網": "网",
        "緒": "绪", "儀": "仪", "壓": "压", "雙": "双", "對": "对",
        "應": "应", "碼": "码", "區": "区", "級": "级", "層": "层",
        "維": "维", "統": "统", "計": "计", "號": "号", "經": "经",
        "過": "过", "進": "进", "遠": "远", "運": "运", "隨": "随",
        "関": "关", "確": "确", "權": "权", "資": "资", "質": "质",
        "測": "测", "評": "评", "讓": "让", "讀": "读", "變": "变",
        "顯": "显", "風": "风", "驗證": "验证",
    }
    return "".join(table.get(ch, ch) for ch in text)


def rewrite_query(query: str) -> str:
    """审查反馈 -> 检索 query（RAG-test FulltextQueryer.question 同款流程）。"""
    original = (query or "").strip()
    t = _SPECIAL.sub(" ", _tradi2simp(_str_q2b(original.lower()))).strip()
    t = _POLITENESS.sub("", t)
    if not t:
        t = original
    return t.strip()


# ---------------------------------------------------------------- 检索
def _cosine_scores(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """query_vec(1,d) x matrix(n,d) -> 余弦相似度(n,)。"""
    if matrix.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    qn = np.linalg.norm(query_vec)
    if qn == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    return (matrix @ query_vec) / (norms * qn)


def _fusion(bm25_scores: np.ndarray, cos_scores: np.ndarray) -> np.ndarray:
    """加权融合（同 RAG-test weighted_sum("0.001,1")）。

    BM25 原始分先做 min-max 归一化到 [0,1]（Infinity 的 BM25 分数是引擎侧
    绝对值，不同查询量纲不同；本地实现归一化后融合更稳）。
    """
    b = np.asarray(bm25_scores, dtype=np.float64)
    if b.size and b.max() > b.min():
        b = (b - b.min()) / (b.max() - b.min())
    else:
        b = np.zeros_like(b, dtype=np.float64)
    return W_BM25 * b + W_COS * np.asarray(cos_scores, dtype=np.float64)


def _rerank(evidences: list[dict]) -> list[dict]:
    """精排：去重（同页同 kind 取最高分）+ 页码邻域聚簇 + 降序。

    聚簇：按 page 排序后把连续同页（±1 页）的证据合并成一条，text 拼接，
    保证「缺表 3 的 MAE 列」这类反馈能拿到整页相关上下文而非半截 chunk。
    """
    # 1) 去重：同 (page, kind) 只留分数最高的
    best: dict[tuple, dict] = {}
    for ev in evidences:
        k = (ev.get("page"), ev.get("kind"))
        if k not in best or ev.get("score", 0) > best[k].get("score", 0):
            best[k] = ev
    # 2) 按 page 聚簇
    ordered = sorted(best.values(), key=lambda e: (e.get("page") or 0,
                                                   -(e.get("score") or 0)))
    merged: list[dict] = []
    for ev in ordered:
        if merged and abs((ev.get("page") or 0) - (merged[-1].get("page") or 0)) <= 1 \
                and ev.get("kind") == merged[-1].get("kind"):
            # 同页邻域同类：合并文本（HTML 用分号连接）
            cur = merged[-1]
            cur["text"] = (cur.get("text") or "") + "\n\n" + (ev.get("text") or "")
            if cur.get("html") and ev.get("html"):
                cur["html"] += ev["html"]
            cur["score"] = max(cur.get("score", 0), ev.get("score", 0))
            continue
        merged.append(dict(ev))
    merged.sort(key=lambda e: -(e.get("score") or 0))
    return merged


def search(query: str, top_k: int = 5, *,
           index_dir: Path = RAG_INDEX_DIR,
           use_cache: bool = True) -> list[dict]:
    """混合检索入口。

    top_k 是**去重聚簇后**的证据条数；内部双通道各取 top_k*3 再融合，
    避免某一通道漏召回。
    """
    chunks = load_chunks(index_dir)
    vectors = load_vectors(index_dir)
    if not chunks or vectors is None:
        raise RuntimeError(
            f"RAG 索引不存在或不完整（{index_dir}）。请先运行 "
            "python -m paper2ppt.rag_index")
    if len(chunks) != vectors.shape[0]:
        raise RuntimeError(f"索引不一致：chunks={len(chunks)} 但 vectors={vectors.shape[0]}")

    q = rewrite_query(query)
    if not q:
        return []

    model = (load_meta(index_dir) or {}).get("model") or os.environ.get(
        "EMBEDDING_MODEL", "BAAI/bge-m3")
    query_vec = embed_texts([q], model)[0]
    cos = _cosine_scores(query_vec, vectors)

    bm25 = build_bm25(chunks)
    bm25_scores = np.asarray(bm25.get_scores(_bm25_tokenize(q)),
                             dtype=np.float64)

    fused = _fusion(bm25_scores, cos)

    # 阈值过滤（余弦为主闸门；BM25 高分但余弦过低视为词汇巧合）
    keep = fused > (W_COS * COS_THRESHOLD)
    if not keep.any():
        # 阈值过严时回退：取余弦 top_k*3 中最高的，避免「反馈了却拿不到证据」
        idxs = np.argsort(-cos)[: max(1, top_k * 3)]
    else:
        idxs = np.where(keep)[0]
    idxs = idxs[np.argsort(-fused[idxs])][: top_k * 3]

    evidences = []
    for i in idxs:
        c = chunks[i]
        evidences.append({
            "chunk_id": c.get("id"),
            "page": c.get("page"),
            "kind": c.get("kind"),
            "score": round(float(fused[i]), 4),
            "text": c.get("text", ""),
            "html": c.get("html"),
            "fig_path": c.get("fig_path"),
            "caption": c.get("caption"),
            "table_no": c.get("table_no"),
        })
    return _rerank(evidences)[:top_k]


def format_evidence(evidences: list[dict], max_chars: int = 4000) -> str:
    """证据 -> 给 PPT Agent 的可读文本（含页码溯源）。"""
    if not evidences:
        return "（RAG 检索无结果）"
    lines = []
    for ev in evidences:
        pno = ev.get("page")
        kind = ev.get("kind")
        head = f"【第{pno}页 · {kind}】"
        if ev.get("table_no") is not None:
            head = f"【第{pno}页 · 表{ev['table_no']}】"
        if ev.get("fig_no") is not None:
            head = f"【第{pno}页 · 图{ev['fig_no']}】"
        body = ev.get("html") or ev.get("text") or ""
        if ev.get("fig_path"):
            body += f"\n（原图：{ev['fig_path']}）"
        lines.append(f"{head} 相关性 {ev.get('score', 0):.3f}\n{body}")
    text = "\n\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n…（另有 {len(text) - max_chars} 字被省略）"
    return text


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "PatchTST 的自注意力机制如何处理时序通道"
    evs = search(q, top_k=5)
    print(format_evidence(evs))
    print(f"\n[{len(evs)} 条证据]")
