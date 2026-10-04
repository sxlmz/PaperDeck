# -*- coding: utf-8 -*-
"""rag_index.py：论文内部 RAG 索引构建（离线）。

模仿 RAG-test 的 DeepDOC 链路（parse -> chunk -> embed -> index），但只依赖
本项目已有的解析产物（parsed_paper.json），不 import RAG-test 项目：
  1. 切片：text/title/formula 块按段落边界累积到 CHUNK_TOKEN_SIZE；
            table 整块不切并转 HTML（construct_table 同款）；figure 整块
           （图注 + 图片路径）——保证「审查反馈缺数据/缺图」时能整块召回。
  2. 向量化：bge-m3 embedding（EMBEDDING_API_BASE/KEY/MODEL，SiliconFlow）。
  3. 索引：BM25（rank_bm25 + jieba 分词）+ 余弦（numpy 内积）双通道，
           与 RAG-test 的 Infinity HNSW cosine + rag-coarse/rag-fine 同构。
  4. 存储：本地 JSONL（chunks + vectors npy + meta.json），embedding 结果
           走 Redis 缓存（p2p:ragemb: 前缀）避免重复付费调用。

产物（workspace/rag_index/）：
    chunks.jsonl   每行一个 chunk：{id, page, kind, text, html?, fig_path?, bbox?}
    vectors.npy    所有 chunk 的向量矩阵（行序 = chunks.jsonl 行序）
    meta.json      {model, dim, chunk_count, pdf_sha, built_at}
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import numpy as np

from . import cache
from .config import PARSED_JSON, WORKSPACE

#: 索引目录与版本（解析策略变了就升版本，旧索引自然失效）
RAG_INDEX_DIR = WORKSPACE / "rag_index"
RAG_VERSION = 1

CHUNK_TOKEN_SIZE = 512          # 与 RAG-test TokenChunker 一致
EMBED_BATCH = 32                # embedding 批大小
EMBED_TIMEOUT = 60

#: token 数粗估：CJK 每字符 1 token，其余按 4 字符 1 token
_CJK = re.compile(r"[\u4e00-\u9fff]")


def num_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(re.sub(r"[\u4e00-\u9fff\s]+", "", text))
    return cjk + int(other / 4) + 1


# ---------------------------------------------------------------- 表格转 HTML
def table_to_html(table: dict) -> str:
    """rows -> HTML 表格文本（DeepDOC construct_table 同款语义）。

    保留行列结构与表注；embedding 前会 strip 标签，入库保留 HTML 原文
    （与 RAG-test tokenize_chunks 一致）。
    """
    rows = table.get("rows") or []
    if not rows:
        return ""
    n_cols = max(len(r) for r in rows)
    parts = ["<table>"]
    # 首行视为表头
    head = [str(c or "") for c in (rows[0] or [])]
    parts.append("<thead><tr>")
    for ci in range(n_cols):
        parts.append(f"<th>{head[ci] if ci < len(head) else ''}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows[1:]:
        parts.append("<tr>")
        for ci in range(n_cols):
            parts.append(f"<td>{row[ci] if ci < len(row) else ''}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _strip_html(text: str) -> str:
    """embedding 用的纯文本：剥掉表格标签、合并空白。"""
    t = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- 切片
def _cut_paragraphs(text: str, page: int, kind: str = "text") -> list[dict]:
    """把一段文本按段落边界切成 ≤ CHUNK_TOKEN_SIZE 的 chunks。

    段落边界 = 空行。单段超长时再按句子边界兜底切（不硬切单词）。
    """
    out = []
    buf, buf_tokens = [], 0
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        n = num_tokens(para)
        if buf and buf_tokens + n > CHUNK_TOKEN_SIZE:
            out.append({"kind": kind, "page": page, "text": "\n\n".join(buf)})
            buf, buf_tokens = [], 0
        # 单段就超预算：按句子边界硬切
        while n > CHUNK_TOKEN_SIZE:
            sents = re.split(r"(?<=[。！？!?.;;])", para)
            piece, piece_tokens = [], 0
            for s in sents:
                sn = num_tokens(s)
                if piece and piece_tokens + sn > CHUNK_TOKEN_SIZE:
                    out.append({"kind": kind, "page": page, "text": "".join(piece)})
                    piece, piece_tokens = [], 0
                piece.append(s)
                piece_tokens += sn
            if piece:
                out.append({"kind": kind, "page": page, "text": "".join(piece)})
            para, n = "", 0
        if para:
            buf.append(para)
            buf_tokens += n
    if buf:
        out.append({"kind": kind, "page": page, "text": "\n\n".join(buf)})
    return out


def build_chunks(parsed: dict) -> list[dict]:
    """parsed_paper.json -> chunk 列表（有序）。

    chunk schema：
      {id, page, kind(text|table|figure|formula), text, html?, fig_path?,
       caption?, bbox?}
    table 用 html 字段存 HTML 原文（text = strip 后纯文本）；
    figure 用 fig_path 指向 PDF 原始图，caption 存图注。
    """
    chunks: list[dict] = []
    next_id = 1

    def add(c: dict) -> None:
        nonlocal next_id
        c["id"] = next_id
        next_id += 1
        chunks.append(c)

    # ---- 1. 结构块：text / title / formula（content_list 带 page + bbox）----
    # 按页累积文本块；formula 单独成块（保留 LaTeX 语义）
    blocks = parsed.get("content_list") or []
    text_buf: list[str] = []
    text_page: int | None = None
    text_tokens = 0

    def flush_text() -> None:
        nonlocal text_buf, text_page, text_tokens
        if text_buf:
            txt = "\n\n".join(text_buf)
            for c in _cut_paragraphs(txt, text_page or 1, "text"):
                add(c)
        text_buf, text_page, text_tokens = [], None, 0

    for b in blocks:
        typ = b.get("type")
        t = (b.get("text") or "").strip()
        if not t:
            continue
        pno = b.get("page") or 1
        if typ == "formula":
            flush_text()
            add({"kind": "formula", "page": pno, "text": t,
                 "bbox": b.get("bbox")})
        elif typ in ("text", "title"):
            n = num_tokens(t)
            if text_page is not None and pno != text_page:
                flush_text()
            if text_page is None:
                text_page = pno
            text_buf.append(t)
            text_tokens += n
            if text_tokens >= CHUNK_TOKEN_SIZE:
                flush_text()
    flush_text()

    # ---- 2. 表格：整块不切，转 HTML ----
    for t in parsed.get("tables") or []:
        html = table_to_html(t)
        if not html:
            continue
        txt = _strip_html(html)
        if not txt:
            continue
        add({"kind": "table", "page": t.get("page") or 1, "text": txt,
             "html": html, "table_no": t.get("table_no"), "bbox": t.get("bbox")})

    # ---- 3. 图：图注 + 原图路径，整块 ----
    for f in parsed.get("figures") or []:
        cap = (f.get("caption") or "").strip()
        if not cap and not f.get("path"):
            continue
        add({"kind": "figure", "page": f.get("page") or 1,
             "text": cap or f"Figure {f.get('fig_no')}",
             "fig_no": f.get("fig_no"), "fig_path": f.get("path"),
             "caption": cap})

    return chunks


# ---------------------------------------------------------------- embedding
def _embed_client():
    from openai import OpenAI
    key = (os.environ.get("EMBEDDING_API_KEY") or "").strip()
    base = (os.environ.get("EMBEDDING_API_BASE") or "").strip()
    if not key:
        raise RuntimeError("缺少 EMBEDDING_API_KEY，无法构建 RAG 向量索引")
    return OpenAI(api_key=key, base_url=base or None, timeout=EMBED_TIMEOUT)


def embed_texts(texts: list[str], model: str = "") -> np.ndarray:
    """批量 embedding（带 Redis 缓存：按文本哈希，避免重复付费调用）。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    model = model or os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
    client = _embed_client()
    all_batches: list[np.ndarray] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        keys = []
        fresh = []
        fresh_idx = []
        vecs: dict[int, np.ndarray] = {}
        for j, t in enumerate(batch):
            ck = cache.key("ragemb", model, cache.sha256_bytes(t.encode("utf-8"))[:32])
            hit = cache.cache_get(ck)
            if hit is not None:
                vecs[j] = np.asarray(hit, dtype=np.float32)
            else:
                keys.append(ck)
                fresh.append(t)
                fresh_idx.append(j)
        if fresh:
            resp = client.embeddings.create(model=model, input=fresh)
            data = sorted(resp.data, key=lambda d: d.index)
            for j, emb in zip(fresh_idx, data):
                v = np.asarray(emb.embedding, dtype=np.float32)
                cache.cache_set(keys[j], v.tolist(), cache.TTL_INDEX)
                vecs[j] = v
        batch_vecs = np.stack([vecs[j] for j in range(len(batch))])
        all_batches.append(batch_vecs)
    return np.concatenate(all_batches)


# ---------------------------------------------------------------- BM25
def _bm25_tokenize(text: str) -> list[str]:
    """分词：英文按小写词，中文按 jieba。"""
    import jieba
    tokens = []
    for word in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", (text or "").lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            tokens.extend(jieba.lcut(word))
        else:
            tokens.append(word)
    return [t for t in tokens if len(t) > 1]


def build_bm25(chunks: list[dict]):
    from rank_bm25 import BM25Okapi
    corpus = [_bm25_tokenize(c.get("text") or "") for c in chunks]
    return BM25Okapi(corpus)


# ---------------------------------------------------------------- 索引落盘
def build_index(parsed_path: Path = PARSED_JSON,
                out_dir: Path = RAG_INDEX_DIR) -> dict:
    """parsed_paper.json -> chunks + vectors + BM25 -> 落盘。

    返回 {chunk_count, dim, index_dir}；可重复运行（幂等重建）。
    """
    from .models import load_json
    from . import cache as _cache

    parsed = load_json(parsed_path)
    chunks = build_chunks(parsed)
    if not chunks:
        raise RuntimeError("parsed_paper.json 无可索引内容（切片为空）")

    texts = [c.get("text") or "" for c in chunks]
    model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
    print(f"[rag_index] embedding {len(texts)} chunks（{model}，batch={EMBED_BATCH}）...")
    vectors = embed_texts(texts, model)

    bm25 = build_bm25(chunks)
    bm25_docs = [[c.get("text") or ""] for c in chunks]  # 保留原文供 BM25 检索侧使用

    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = out_dir / "chunks.jsonl"
    with open(chunk_path, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    np.save(out_dir / "vectors.npy", vectors)
    meta = {
        "rag_version": RAG_VERSION,
        "model": model,
        "dim": int(vectors.shape[1]) if vectors.ndim == 2 else 0,
        "chunk_count": len(chunks),
        "pdf_sha": _cache.sha256_file(parsed_path),
        "built_at": __import__("time").time(),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[rag_index] 完成：{len(chunks)} chunks，dim={meta['dim']} -> {out_dir}")
    return meta


def load_chunks(out_dir: Path = RAG_INDEX_DIR) -> list[dict]:
    path = out_dir / "chunks.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_vectors(out_dir: Path = RAG_INDEX_DIR) -> np.ndarray | None:
    path = out_dir / "vectors.npy"
    if not path.exists():
        return None
    return np.load(path)


def load_meta(out_dir: Path = RAG_INDEX_DIR) -> dict:
    path = out_dir / "meta.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    args = sys.argv[1:]
    print(json.dumps(build_index(
        Path(args[0]) if args and Path(args[0]).exists() else PARSED_JSON),
        ensure_ascii=False, indent=2))
