# -*- coding: utf-8 -*-
"""LLM 客户端抽象（OpenAI 兼容）。

Agent 的所有 LLM 能力（Planner 大纲 / Designer 视觉决策 / Critic 截图审查）
都通过本模块调用；未配置 Key 时返回 None，由上层切换到 offline 确定性 Agent。

配置优先级（环境变量）：
    P2P_LLM_API_KEY / P2P_LLM_BASE_URL / P2P_LLM_MODEL   ← 本项目专用
    SINGLE_KEY / SINGLE_BASE / SINGLE_MODEL               ← LangGraph 单模型模式
    DEEPSEEK_API_KEY（base=https://api.deepseek.com, model=deepseek-chat）
    OPENAI_API_KEY / OPENAI_API_BASE / MAIN_MODEL
视觉（Critic 用，可选）：
    P2P_VISION_MODEL + 上面的 Key（需供应商支持图像输入，如 Qwen-VL / GLM-4V）
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import DEFAULT_ENV_FILES

_NO_PROXY = {"NO_PROXY": "*", "no_proxy": "*"}


def load_dotenv_files(files=None) -> None:
    """加载 .env（可选）。不做任何输出，避免泄露密钥。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for f in (files or DEFAULT_ENV_FILES):
        if f and os.path.exists(f):
            load_dotenv(f, override=False)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    vision_model: str = ""
    vision_api_key: str = ""
    vision_base_url: str = ""
    timeout: float = 120.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)


def resolve_llm_config() -> LLMConfig:
    """按优先级解析 LLM 配置。"""
    cfg = LLMConfig(
        api_key=_env("P2P_LLM_API_KEY") or _env("SINGLE_KEY") or _env("DEEPSEEK_API_KEY")
        or _env("OPENAI_API_KEY"),
        base_url=_env("P2P_LLM_BASE_URL") or _env("SINGLE_BASE")
        or ("" if _env("DEEPSEEK_API_KEY") else _env("OPENAI_API_BASE")),
        model=_env("P2P_LLM_MODEL") or _env("SINGLE_MODEL") or _env("MAIN_MODEL")
        or ("deepseek-chat" if _env("DEEPSEEK_API_KEY") else _env("OPENAI_API_MODEL", "gpt-4o-mini")),
        vision_api_key=(_env("P2P_VISION_API_KEY") or _env("DASHSCOPE_API_KEY")
                        or _env("SINGLE_KEY")),
        vision_base_url=_env("P2P_VISION_BASE_URL") or _env("QWEN_EVAL_BASE"),
        vision_model=(_env("P2P_VISION_MODEL") or _env("QWEN_EVAL_MODEL")),
    )
    return cfg


def _extract_json(text: str):
    """从模型输出中稳健提取 JSON（dict 或 list）。

    容忍 markdown 代码围栏、前后杂质；优先按数组/对象整体截取。
    """
    if not text:
        return None
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    # 数组整体
    if t.startswith("[") and t.rfind("]") > 0:
        try:
            return json.loads(t[:t.rfind("]") + 1])
        except Exception:  # noqa: BLE001
            pass
    # 对象：找第一个 { 到最后一个 }
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except Exception:  # noqa: BLE001
            pass
    return None


class LLMClient:
    """OpenAI 兼容 chat / vision 客户端（轻量封装，不引入 langchain 重量依赖）。"""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        from openai import OpenAI
        kwargs = {"api_key": cfg.api_key, "timeout": cfg.timeout}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = OpenAI(**kwargs)
        # 视觉模型可能与文本模型不同端点（如 Qwen-VL @ DashScope），独立建客户端
        self._vision_client = self.client
        if cfg.vision_model and (cfg.vision_base_url or cfg.vision_api_key):
            vk = cfg.vision_api_key or cfg.api_key
            self._vision_client = OpenAI(api_key=vk, timeout=cfg.timeout,
                                         base_url=cfg.vision_base_url)

    def chat_json(self, messages, temperature: float = 0.3,
                  max_tokens: int = 8192, *, refresh: bool = False,
                  use_cache: bool = True, schema_version=None):
        """调用 chat 并解析 JSON（dict 或 list）。

        优先使用 json_object 响应格式（提高结构化输出成功率）；
        供应商不支持该参数时自动降级为普通调用。失败返回 None。

        结果按 (模型, 温度, messages, schema 版本) 哈希缓存：messages 里含
        system prompt，所以**改 prompt 会自动换键、自动失效**，不会吃到旧结果。
        命中会打印短键便于排查（否则缓存会让坏结果逐字节复现，看起来像确定性 bug）。
        """
        from . import cache
        from .models import SCHEMA_VERSION

        ck = None
        if use_cache and not refresh:
            ck = cache.llm_key(self.cfg.model, temperature, messages,
                               schema_version or SCHEMA_VERSION)
            hit = cache.cache_get(ck)
            if hit is not None:
                print(f"[cache] llm hit {ck[-8:]}")
                return hit

        base = dict(model=self.cfg.model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens)
        result = None
        for use_fmt in (True, False):
            try:
                kwargs = dict(base)
                if use_fmt:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = self.client.chat.completions.create(**kwargs)
                parsed = _extract_json(resp.choices[0].message.content or "")
                if parsed is not None:
                    result = parsed
                    break
            except Exception:  # noqa: BLE001  不支持 json_object 等，降级重试
                if not use_fmt:
                    raise
        # 只缓存成功解析的结果（失败/空响应不写，避免把一次网络抖动固化下来）
        if result is not None and use_cache:
            ck = ck or cache.llm_key(self.cfg.model, temperature, messages,
                                     schema_version or SCHEMA_VERSION)
            cache.cache_set(ck, result, cache.TTL_LLM)
        return result

    def chat_text(self, messages, temperature: float = 0.3,
                  max_tokens: int = 4096) -> str:
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    def judge_json(self, messages, temperature: float = 0.1,
                   max_tokens: int = 1024, *, refresh: bool = False,
                   use_cache: bool = True):
        """内容 Judge 专用：用评估模型（qwen3.7-flash，复用 _vision_client /
        DashScope 兼容端点）调文本 chat 并解析 JSON。

        VLM 审查与 judge 同模型同端点，直接复用 _vision_client；未配置
        vision 模型时回退主客户端。缓存键含 (模型, 温度, messages)，
        模型换成 qwen 自动用新键，与主 LLM 缓存互不串扰。
        """
        from . import cache
        client = self._vision_client if self.cfg.vision_model else self.client
        model = self.cfg.vision_model or self.cfg.model

        ck = None
        if use_cache and not refresh:
            ck = cache.llm_key(model, temperature, messages, "judge-qwen")
            hit = cache.cache_get(ck)
            if hit is not None:
                print(f"[cache] judge hit {ck[-8:]}")
                return hit

        base = dict(model=model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens)
        result = None
        for use_fmt in (True, False):
            try:
                kwargs = dict(base)
                if use_fmt:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
                parsed = _extract_json(resp.choices[0].message.content or "")
                if parsed is not None:
                    result = parsed
                    break
            except Exception:  # noqa: BLE001  不支持 json_object 等，降级重试
                if not use_fmt:
                    raise
        if result is not None and use_cache:
            ck = ck or cache.llm_key(model, temperature, messages, "judge-qwen")
            cache.cache_set(ck, result, cache.TTL_LLM)
        return result

    def vision_json(self, image, instruction: str, *, task: str,
                    prompt_version: str | int = 1, system: str = "",
                    temperature: float = 0.1, max_tokens: int = 1024,
                    refresh: bool = False, use_cache: bool = True):
        """把图片发给 VLM 并解析 JSON 结果（按图片内容哈希缓存）。

        image 可以是路径或 bytes。task/prompt_version 参与缓存键：
        prompt 升级时显式改 prompt_version 即可让旧结果失效。
        """
        from . import cache
        if not self.cfg.vision_model:
            raise RuntimeError("未配置视觉模型（P2P_VISION_MODEL / QWEN_EVAL_MODEL）")

        data = image if isinstance(image, (bytes, bytearray)) else Path(image).read_bytes()
        ck = None
        if use_cache and not refresh:
            ck = cache.vlm_key(self.cfg.vision_model, prompt_version, task, bytes(data))
            hit = cache.cache_get(ck)
            if hit is not None:
                print(f"[cache] vlm hit {ck[-8:]}")
                return hit

        b64 = base64.b64encode(bytes(data)).decode()
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]})
        resp = self._vision_client.chat.completions.create(
            model=self.cfg.vision_model, messages=msgs,
            temperature=temperature, max_tokens=max_tokens)
        parsed = _extract_json(resp.choices[0].message.content or "")
        if use_cache and parsed is not None:
            ck = ck or cache.vlm_key(self.cfg.vision_model, prompt_version,
                                     task, bytes(data))
            cache.cache_set(ck, parsed, cache.TTL_VLM)
        return parsed

    def vision_review(self, image_path, instruction: str) -> str:
        """把截图发给 VLM（OpenAI 兼容 image_url），返回审查文本。

        轻量文本接口，不做缓存（需要结构化+缓存请用 vision_json）。
        """
        if not self.cfg.vision_model:
            raise RuntimeError("未配置 P2P_VISION_MODEL，无法进行视觉审查")
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        mime = "image/png"
        resp = self._vision_client.chat.completions.create(
            model=self.cfg.vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            temperature=0.1,
            max_tokens=1024,
        )
        return resp.choices[0].message.content or ""


_SINGLETON: Optional[LLMClient] = None
_SINGLETON_CFG: Optional[LLMConfig] = None


def get_llm(env_files=None) -> Optional[LLMClient]:
    """获取全局 LLM 客户端（首次按环境变量解析；未配置返回 None）。"""
    global _SINGLETON, _SINGLETON_CFG
    load_dotenv_files(env_files)
    cfg = resolve_llm_config()
    if not cfg.enabled:
        return None
    if _SINGLETON is not None and _SINGLETON_CFG == cfg:
        return _SINGLETON
    _SINGLETON_CFG = cfg
    _SINGLETON = LLMClient(cfg)
    return _SINGLETON
