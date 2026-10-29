# -*- coding: utf-8 -*-
"""本地小模型执行方案：引擎探测、模型基准、按硬件档位给出落地方案。

三大主流本地引擎都提供 OpenAI 兼容端点，本项目的 OpenAICompatLLM 可直连：
  - Ollama      http://127.0.0.1:11434/v1   （模型管理最省心）
  - LM Studio   http://127.0.0.1:1234/v1    （GUI 友好）
  - llama.cpp   http://127.0.0.1:8080/v1    （llama-server，最可控）

已知坑：Ollama 的 OpenAI 兼容层传不了 num_ctx，默认上下文仅 4K，
中文长稿会被截断。解法：设环境变量 OLLAMA_CONTEXT_LENGTH=16384，
或用 Modelfile 里 `PARAMETER num_ctx 16384` 自建模型。
"""
from __future__ import annotations

import json
import time

import requests

from .llm import OpenAICompatLLM
from .util import extract_json

ENGINES = [
    {
        "name": "ollama",
        "label": "Ollama",
        "probe": "http://127.0.0.1:11434/api/tags",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": lambda j: [m["name"] for m in j.get("models", [])],
    },
    {
        "name": "lmstudio",
        "label": "LM Studio",
        "probe": "http://127.0.0.1:1234/v1/models",
        "base_url": "http://127.0.0.1:1234/v1",
        "models": lambda j: [m["id"] for m in j.get("data", [])],
    },
    {
        "name": "llamacpp",
        "label": "llama.cpp (llama-server)",
        "probe": "http://127.0.0.1:8080/v1/models",
        "base_url": "http://127.0.0.1:8080/v1",
        "models": lambda j: [m["id"] for m in j.get("data", [])],
    },
]

_UA = {"User-Agent": "short-story-studio/0.1"}


def detect_engines(timeout: float = 1.5) -> list[dict]:
    """探测本机已运行的本地推理引擎。"""
    found = []
    for eng in ENGINES:
        try:
            r = requests.get(eng["probe"], headers=_UA, timeout=timeout)
            if r.status_code != 200:
                continue
            models = eng["models"](r.json())
            found.append(
                {
                    "engine": eng["name"],
                    "label": eng["label"],
                    "base_url": eng["base_url"],
                    "models": models,
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return found


def pick_model(models, prefer: list[str] | None = None) -> str | None:
    """从引擎模型列表里挑一个：优先命中 prefer 关键词，否则第一个。"""
    if isinstance(models, dict):
        models = models.get("models") or []
    models = models or []
    for kw in prefer or []:
        for m in models:
            if kw.lower() in m.lower():
                return m
    # 避开 embedding / rerank / vision-only 模型
    for m in models:
        if not any(k in m.lower() for k in ("embed", "rerank", "bge", "clip", "whisper")):
            return m
    return models[0]


def bench_model(base_url: str, model: str, api_key: str = "", timeout: int = 120) -> dict:
    """对某模型跑两项测试：中文生成速度 + JSON 指令遵循（3 次取成功数）。"""
    llm = OpenAICompatLLM(
        {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "temperature": 0.7,
            "max_tokens": 512,
            "timeout": timeout,
            "retries": 1,
        }
    )
    out = {"model": model, "base_url": base_url}

    # 1) 生成速度
    t0 = time.time()
    try:
        text = llm.chat(
            "你是小说写手。", "用下沉市场文风写一段80字左右的开头：婆婆把儿媳的陪嫁金镯子给了小姑子。"
        )
        dt = time.time() - t0
        chars = len(text.strip())
        out["speed_ok"] = True
        out["chars"] = chars
        out["seconds"] = round(dt, 1)
        out["chars_per_sec"] = round(chars / dt, 1) if dt > 0 else 0
        out["sample"] = text.strip()[:80]
    except Exception as e:  # noqa: BLE001
        out["speed_ok"] = False
        out["error"] = str(e)[:160]
        return out

    # 2) JSON 可靠性（审查/评分任务的关键能力）
    ok = 0
    for i in range(3):
        try:
            raw = llm.chat(
                "只输出 JSON。",
                '审稿：开头是"腊月二十八，我攥着三千八的退彩礼判决书站在婆家门口"。'
                '严格输出 JSON：{"hook_strong": true或false, "score": 0到10}',
                json_mode=True,
            )
            extract_json(raw)
            ok += 1
        except Exception:  # noqa: BLE001
            continue
    out["json_ok"] = f"{ok}/3"
    return out


# 硬件档位 → 推荐方案（模型名以 Ollama 拉取名为准，llama.cpp/LM Studio 换 GGUF 文件）
TIERS = [
    {
        "tier": "纯 CPU / 6GB 显存",
        "models": "qwen3:4b（写）+ qwen3:4b（审）",
        "expect": "生成约 3-10 字/秒（CPU 慢，适合过夜批量跑）",
        "note": "Qwen3-4B-Instruct-2507 Q4 约 2.5GB，建议上下文 16K",
    },
    {
        "tier": "8GB 显存（RTX 3060Ti/4060）",
        "models": "qwen3:8b 全角色",
        "expect": "生成约 15-30 字/秒",
        "note": "8B Q4 约 5GB + KV 缓存，上下文给到 16K",
    },
    {
        "tier": "12GB 显存（3060 12G/4070）",
        "models": "qwen3:14b（写）+ qwen3:8b（审/评）",
        "expect": "写作 8-20 字/秒，审查近实时",
        "note": "14B Q4 约 9GB；审评用 8B 小模型分工性价比最高",
    },
    {
        "tier": "16-24GB 显存（4080/4090）",
        "models": "qwen3:14b / glm4:9b（写）+ qwen3:8b（审）",
        "expect": "写作 20-50 字/秒，可开 32K 上下文跑全稿润色",
        "note": "GLM-4-9B-Chat-1M 变体适合超长上下文场景",
    },
    {
        "tier": "混合模式（推荐，任意机器）",
        "models": "云端 API（写）+ 本地 qwen3:4b/8b（审/评/知识卡）",
        "expect": "质量不降，审查/评分零成本，总调用量省 60%+",
        "note": "llm.writer 走 API，llm_roles.reviewer/panel/light 走本地",
    },
]

ROLE_TABLE = [
    ("writer", "写手：章节扩写/重写、终稿改写", "用最强模型（本地最大档或云端 API）"),
    ("reviewer", "主编审查、全稿体检（高频 JSON）", "本地 4B/8B 足够"),
    ("panel", "读者团评分（每章 3 画像 × 轮次）", "本地 4B/8B 足够，调用量最大"),
    ("planner", "提梗/书名/大纲/故事圣经", "中档；混合模式下建议走 API"),
    ("light", "知识卡蒸馏（长文本→结构化）", "本地 8B+，需长上下文"),
]


def setup_text() -> str:
    lines = ["【本地小模型落地方案（按硬件档位）】", ""]
    for i, t in enumerate(TIERS, 1):
        lines.append(f"{i}. {t['tier']}")
        lines.append(f"   模型分工：{t['models']}")
        lines.append(f"   预期速度：{t['expect']}")
        lines.append(f"   说明：{t['note']}")
        lines.append("")
    lines.append("【快速安装（Ollama 路线，最省心）】")
    lines.append("  1. 安装 Ollama：https://ollama.com/download（Windows 一键安装）")
    lines.append("  2. 拉模型：  ollama pull qwen3:8b")
    lines.append("  3. 设上下文（关键！默认 4K 会截断长稿）：")
    lines.append("     setx OLLAMA_CONTEXT_LENGTH 16384   然后重启 Ollama 服务")
    lines.append("  4. python -m studio local   # 确认探测到引擎与模型")
    lines.append("  5. python -m studio local --bench qwen3:8b   # 测速度与 JSON 可靠性")
    lines.append("  6. 把 config.yaml 的 llm 段改为 Ollama（用 --snippet 生成配置片段）")
    lines.append("")
    lines.append("【角色分工配置】（llm_roles，未配置的角色沿用主 llm）")
    for role, desc, sug in ROLE_TABLE:
        lines.append(f"  {role:<9} {desc}  ← {sug}")
    return "\n".join(lines)


def snippet_text(base_url: str, model_main: str, model_small: str, hybrid: bool = True) -> str:
    """生成可粘贴的 config.yaml 片段。hybrid=True 时 writer 走云端 API。"""
    if hybrid:
        return f"""llm:
  provider: api
  base_url: https://api.deepseek.com/v1   # 写作/大纲走云端，保质量
  api_key: sk-你的key
  model: {model_main}

llm_roles:
  reviewer:                                # 主编审查 + 全稿体检
    provider: local
    base_url: {base_url}
    model: {model_small}
  panel:                                   # 读者评分团（调用量最大）
    provider: local
    base_url: {base_url}
    model: {model_small}
  light:                                   # 知识卡蒸馏
    provider: local
    base_url: {base_url}
    model: {model_small}
"""
    return f"""llm:
  provider: local
  base_url: {base_url}
  api_key: ""                              # 本地引擎不校验
  model: {model_main}

llm_roles:
  reviewer:
    provider: local
    base_url: {base_url}
    model: {model_small}
  panel:
    provider: local
    base_url: {base_url}
    model: {model_small}
"""
