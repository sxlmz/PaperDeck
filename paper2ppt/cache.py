# -*- coding: utf-8 -*-
"""Redis 缓存层：论文解析结果 / LLM 结果 / VLM 结果。

隔离约定（三重要求，必须严格遵守）：
  * **独立 db**：默认 2，可用 `P2P_REDIS_DB` 覆盖；
  * **键前缀**：所有键以 `p2p:` 开头，可用 `P2P_REDIS_PREFIX` 覆盖；
  * **写路径自检**：任何写操作都断言键在命名空间内，越界直接 ValueError。

本机同一个 Redis 里还有别的业务缓存（db0 的 `order:sn:*`/`auth:*`、
db1 的 4674 个 `agn:emb:*`/`agn:rrk:*`、db3 的 `ragtest:*`），本模块
**绝不触碰**——清理只按前缀 SCAN，绝不使用 FLUSHDB。

Redis 不可用时整体降级为直通（只警告一次），流水线照常跑完。

键 schema：
    p2p:paper:{pdf_sha256[:32]}:{PARSER_VERSION}   整份 parsed 结果     90d
    p2p:paperindex:{pdf_sha256[:32]}               该论文产出的键集合   90d
    p2p:llm:{sha1(model|temp|messages|schema)}     chat_json 结果       30d
    p2p:vlm:{sha1(model|prompt_ver|task|img)}      VLM 结构化结果       90d
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

# ---------- 配置 ----------
DEFAULT_DB = 2
DEFAULT_PREFIX = "p2p:"

TTL_PAPER = 90 * 86400
TTL_INDEX = 90 * 86400
TTL_LLM = 30 * 86400
TTL_VLM = 90 * 86400

_DISABLED_REASON: str | None = None
_CLIENT = None
_WARNED = False


def _db() -> int:
    """Redis 库号。只认 P2P_REDIS_DB，避免被外部全局 REDIS_DB 之类的变量带偏。"""
    raw = os.environ.get("P2P_REDIS_DB")
    if not raw:
        return DEFAULT_DB
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DB


def _prefix() -> str:
    p = (os.environ.get("P2P_REDIS_PREFIX") or DEFAULT_PREFIX).strip()
    return p if p.endswith(":") else p + ":"


def _host_port() -> tuple[str, int]:
    host = os.environ.get("P2P_REDIS_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("P2P_REDIS_PORT", "6379"))
    except ValueError:
        port = 6379
    return host, port


def get_client():
    """返回 Redis 客户端；不可用返回 None（只警告一次）。"""
    global _CLIENT, _DISABLED_REASON, _WARNED
    if _CLIENT is not None or _DISABLED_REASON is not None:
        return _CLIENT
    if os.environ.get("P2P_NO_CACHE") == "1":
        _DISABLED_REASON = "P2P_NO_CACHE=1"
        return None
    try:
        import redis
        host, port = _host_port()
        client = redis.Redis(host=host, port=port, db=_db(),
                             decode_responses=True,
                             socket_connect_timeout=2,
                             socket_timeout=4)
        client.ping()
        _CLIENT = client
    except Exception as e:  # noqa: BLE001
        _DISABLED_REASON = f"{type(e).__name__}: {e}"
        _CLIENT = None
        if not _WARNED:
            host, port = _host_port()
            print(f"[cache] Redis 不可达（{host}:{port}/db{_db()}）：{e}"
                  f" —— 缓存降级为直通，流水线继续")
            _WARNED = True
    return _CLIENT


def enabled() -> bool:
    return get_client() is not None


def key(*parts: str) -> str:
    """拼一个带命名空间的键。"""
    body = ":".join(str(p) for p in parts if p is not None and str(p) != "")
    return _prefix() + body


def _guard(k: str) -> None:
    if not k.startswith(_prefix()):
        raise ValueError(f"拒绝写入 p2p 命名空间之外的键: {k!r}")


# ---------- 基础读写 ----------
def cache_get(k: str) -> Any | None:
    client = get_client()
    if client is None:
        return None
    try:
        raw = client.get(k)
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def cache_set(k: str, value: Any, ttl: int | None = None) -> bool:
    client = get_client()
    if client is None:
        return False
    _guard(k)
    try:
        client.set(k, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
        return True
    except Exception:  # noqa: BLE001
        return False


def cache_delete(*keys: str) -> int:
    client = get_client()
    if client is None or not keys:
        return 0
    try:
        return int(client.delete(*keys))
    except Exception:  # noqa: BLE001
        return 0


def _scan(prefix_key: str, limit: int = 100000) -> list[str]:
    client = get_client()
    if client is None:
        return []
    out, cur = [], 0
    while True:
        cur, batch = client.scan(cur, match=prefix_key + "*", count=500)
        out.extend(batch)
        if cur == 0 or len(out) >= limit:
            break
    return out


# ---------- 键构造 ----------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest(obj) -> str:
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
        .encode("utf-8")).hexdigest()


def paper_key(pdf_sha: str, parser_version: str | int) -> str:
    return key("paper", pdf_sha[:32], parser_version)


def paper_index_key(pdf_sha: str) -> str:
    return key("paperindex", pdf_sha[:32])


def llm_key(model: str, temperature: float, messages, schema_version: str | int) -> str:
    return key("llm", _digest({"m": model, "t": temperature,
                               "msg": messages, "v": schema_version}))


def vlm_key(model: str, prompt_version: str | int, task: str, image_bytes: bytes) -> str:
    return key("vlm", _digest({"m": model, "p": prompt_version, "task": task,
                               "img": sha256_bytes(image_bytes)}))


# ---------- 论文级注册与失效 ----------
def register(pdf_sha: str, keys: list[str]) -> None:
    """把该论文产出的键登记进索引集合，便于按论文精确失效。"""
    client = get_client()
    if client is None or not keys:
        return
    idx = paper_index_key(pdf_sha)
    _guard(idx)
    try:
        client.sadd(idx, *keys)
        client.expire(idx, TTL_INDEX)
    except Exception:  # noqa: BLE001
        pass


def invalidate(pdf_sha: str | None = None, drop_llm: bool = False,
               drop_vlm: bool = False) -> int:
    """失效缓存。

    给了 pdf_sha 就只删该论文登记的键；否则按前缀全清（仍只动 p2p: 命名空间）。
    """
    targets: list[str] = []
    if pdf_sha:
        idx = paper_index_key(pdf_sha)
        client = get_client()
        if client is not None:
            try:
                targets = list(client.smembers(idx))
            except Exception:  # noqa: BLE001
                targets = []
            targets.append(idx)
    else:
        if drop_llm:
            targets += _scan(key("llm") + ":")
        if drop_vlm:
            targets += _scan(key("vlm") + ":")
        if not drop_llm and not drop_vlm:
            targets += _scan(_prefix())
    return cache_delete(*targets) if targets else 0


def stats() -> dict:
    client = get_client()
    info = {"enabled": client is not None, "db": _db(), "prefix": _prefix(),
            "reason": _DISABLED_REASON}
    if client is None:
        return info
    try:
        info["db_size"] = client.dbsize()
        # 用 "p2p:paper:" 而不是 "p2p:paper" 做前缀，否则会把 paperindex 也算进来
        for name in ("paper", "llm", "vlm", "paperindex"):
            info[name] = len(_scan(key(name) + ":"))
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    return info


# ---------- CLI ----------
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "stats"
    if cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif cmd == "flush":
        kinds = argv[2:]
        n = invalidate(drop_llm="llm" in kinds, drop_vlm="vlm" in kinds)
        print(f"[cache] 已删除 {n} 个键（命名空间 {_prefix()}，db{_db()}）")
    elif cmd == "invalidate":
        from pathlib import Path
        pdf = argv[2]
        sha = sha256_file(Path(pdf))
        n = invalidate(pdf_sha=sha)
        print(f"[cache] 已失效 {pdf} 的缓存：{n} 个键")
    else:
        print("用法: python -m paper2ppt.cache stats|flush [llm|vlm]|invalidate <pdf>")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
