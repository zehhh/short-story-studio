# -*- coding: utf-8 -*-
"""配置加载：config.yaml > 环境变量 > 内置默认值。

支持任意 OpenAI 兼容接口（DeepSeek / GLM / Kimi / Qwen / OpenAI / Ollama / LM Studio 等）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = {
    "llm": {
        "provider": "auto",            # auto / openai_compat / mock
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.85,
        "max_tokens": 8192,
        "timeout": 300,
        "retries": 3,
        "qps": 0,                      # 两次调用最小间隔秒数，0 不限
        "stream": "auto",              # 流式输出：auto=本地引擎自动开启 / true / false
    },
    "studio": {
        "workspace": "workspace",      # 相对本目录根
        "platform": "zhihu",           # 默认目标平台: zhihu / fanqie / uc / qimao
        "target_words": 9000,          # 全稿目标字数（成稿须落在 word_floor~word_ceiling）
        "chapters": 0,                 # 0 = 按目标字数自动定章节数
        "hot_top": 30,                 # 每个平台抓多少条热点
        "concept_candidates": 6,       # 一次提几张梗卡
        "dailyhot_base": "",           # 可选：自建热榜聚合服务地址，如 http://127.0.0.1:6688
        "sources": ["weibo", "zhihu", "baidu", "douyin", "toutiao", "bilibili"],
        "novel_sources": True,        # 抓热点时同时聚合小说平台/故事向源（七猫榜/知乎故事/贴吧）
        "weibo_cookie": "",            # 可选：浏览器里的 SUB=... cookie，提高微博热搜成功率
    },
    "review": {
        "enabled": True,               # 总开关：写作-审查双门禁
        "langqa_enabled": True,        # 语言质检门（确定性正则/统计，零 token 成本）
        "langqa_max_rounds": 2,        # 语言质检打回重写上限
        "lang_polish_enabled": True,   # 全稿语言打磨（定向改写碎句/欧化/冗余/详略，花 token）
        "lang_polish_rounds": 2,       # 语言打磨轮数（每轮最多改 4 节）
        "max_review_rounds": 3,        # 主编审查打回重写上限（达到上限按现稿放行并标记）
        "reader_enabled": True,        # 读者评分团开关
        "reader_panel": 3,             # 参与评分的读者画像数（按题材亲和力挑选）
        "score_threshold": 7.5,        # 读者团均分阈值（0-10），低于即打回
        "max_reader_rounds": 2,        # 读者评分打回重写上限
        "emotion_enabled": True,       # 情绪量化门（情绪分/钩子强度/虐爽比/毒点）
        "emotion_threshold": 7.0,      # 情绪总分阈值
        "hook_threshold": 7.0,         # 章尾钩子强度阈值
        "word_floor": 8000,            # 成稿字数下限：不足自动打回扩写
        "word_ceiling": 12000,         # 成稿字数上限：超标自动打回压缩
        "word_gate_rounds": 5,         # 字数门打回重做上限
        "stage_gate": True,            # 导语/大纲/圣经阶段达标门（不达标打回重做）
        "stage_gate_rounds": 2,        # 阶段门打回重做上限
        "stage_gate_threshold": 8.0,   # 阶段门达标分（0-10）
    },
    "knowledge": {
        "auto_collect": True,          # 写作前自动搜集题材知识
        "max_pages": 8,                # 每个题材最多抓取的网页数
        "page_chars": 1500,            # 每页提取正文字符上限
        "digest_budget": 2200,         # 注入 prompt 的知识卡摘要字符预算
        "allow_llm_fallback": True,    # 搜索全部失败时降级为 LLM 内化生成
    },
    "publish": {
        "platform": "fanqie",         # fanqie / zhihu / uc / qimao
        "mode": "manual",             # manual(投稿包) / custom(自配接口) / mock
        "cookie": "",                 # 作家后台登录 cookie（custom 模式用）
        "endpoints": {"publish": "", "stats": ""},  # 自定义接口模板（抓包获得，publish 支持 {title} 占位）
        "fields": {},                 # stats 返回 JSON 的指标映射，如 {"reads": "data.reads"}
    },
    "analytics": {
        "source": "mock",             # mock / custom
    },
    "logging": {
        "enabled": True,              # 每次运行自动生成运行日志（workspace/logs/）
    },
    "rag": {
        "max_chars": 620,             # 注入 draft 的检索素材字符预算
    },
    "decon": {
        "max_sections": 24,           # 单本最多拆多少段
        "per_call": 2,                # 每次 LLM 调用拆几段
    },
    "materials": {
        # 默认关：成稿自动沉淀 = 把机器生成的稿子回灌进素材库/知识库，
        # 下一篇再检索回来 → 自回声（echo chamber），越写越像自己、越写越薄。
        # 需要反哺时显式开启（config.yaml 或 `run --deposit`）；沉淀端点 `material deposit` 始终可用。
        "auto_deposit": False,
        "convert_hotspots": True,     # 热点事件必须转成小说素材才能入库（需 LLM）
        "auto_curate": True,          # 沉淀后自动接入整理 Agent（质量分级+删低质）
        "curate_batch": 15,           # 整理 Agent 每批评估条数
    },
    # 角色路由：给不同任务配不同模型（本地小模型/云端 API 混合）。
    # 未配置的角色沿用主 llm。provider=local 表示本地 OpenAI 兼容引擎。
    # 示例见 config.example.yaml 或运行 `python -m studio local --snippet`
    "llm_roles": {},
}

ENV_OVERRIDES = {
    # 环境变量 -> (节, 键)
    "STUDIO_LLM_BASE_URL": ("llm", "base_url"),
    "STUDIO_LLM_API_KEY": ("llm", "api_key"),
    "STUDIO_LLM_MODEL": ("llm", "model"),
    "STUDIO_LLM_PROVIDER": ("llm", "provider"),
    "STUDIO_PLATFORM": ("studio", "platform"),
    "STUDIO_TARGET_WORDS": ("studio", "target_words"),
    "STUDIO_WORKSPACE": ("studio", "workspace"),
    "STUDIO_DAILYHOT": ("studio", "dailyhot_base"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path: str | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = Path(config_path) if config_path else (ROOT / "config.yaml")
    # 记录实际使用的配置文件：调度器拉起子进程时必须透传同一份配置，
    # 否则自定义 --config 下定时任务会回落默认 config.yaml（真实 API/真实 workspace）。
    # 在这里记录而不是只靠 CLI，看板/测试/调度器直接 load_config 的路径也一并生效。
    cfg["_config_file"] = str(path)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            import yaml

            user_cfg = yaml.safe_load(text) or {}
        except ImportError:
            try:
                user_cfg = json.loads(text)
            except json.JSONDecodeError as e:
                raise SystemExit(
                    f"配置文件 {path} 解析失败：未安装 pyyaml 且不是合法 JSON。{e}"
                )
        cfg = _deep_merge(cfg, user_cfg)
    # 环境变量覆盖
    for env_key, (section, key) in ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            node = cfg[section]
            if isinstance(node.get(key), int) and not isinstance(node.get(key), bool):
                val = int(val)
            node[key] = val
    # 相对路径转绝对
    ws = Path(cfg["studio"]["workspace"])
    if not ws.is_absolute():
        ws = ROOT / ws
    cfg["studio"]["workspace_abs"] = str(ws)
    return cfg


def data_path(*parts: str) -> Path:
    return ROOT.joinpath("data", *parts)


def prompt_path(name: str) -> Path:
    return ROOT.joinpath("prompts", name + ".md")
