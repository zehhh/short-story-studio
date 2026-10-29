# -*- coding: utf-8 -*-
"""Prompt 模板加载与渲染。

模板文件位于 prompts/*.md，格式：
    <!--TASK:任务名-->
    ===SYSTEM===
    系统提示词
    ===USER===
    用户提示词，支持 {{占位符}}
"""
from __future__ import annotations

from .config import prompt_path

_CACHE: dict[str, tuple[str, str]] = {}


def load_prompt(task: str) -> tuple[str, str]:
    if task in _CACHE:
        return _CACHE[task]
    path = prompt_path(task)
    if not path.exists():
        raise SystemExit(
            f"提示词模板不存在：{path}\n"
            f"  改法：确认 prompts/{task}.md 存在；或用 `python -m studio check` 体检资产完整性"
        )
    text = path.read_text(encoding="utf-8")
    if "===SYSTEM===" not in text or "===USER===" not in text:
        raise SystemExit(
            f"提示词模板格式错误：{path}\n"
            f"  原因：缺少 ===SYSTEM=== 或 ===USER=== 分隔符（模板三段结构被改坏）\n"
            f"  改法：恢复为 `<!--TASK:{task}-->` + `===SYSTEM===` + `===USER===` 三段，"
            f"或从同目录 {task}.md.bak 备份恢复"
        )
    header, rest = text.split("===SYSTEM===", 1)
    system, user = rest.split("===USER===", 1)
    # 任务标记保留进 system，供日志与 MockLLM 识别阶段
    system = header.strip() + "\n\n" + system.strip()
    _CACHE[task] = (system, user.strip())
    return _CACHE[task]


def _safe_val(v) -> str:
    """占位值消毒：内容里混入的 {{xxx}} 模板记号（抓取的网页/知识卡文本常见）
    会被下面的"未提供占位符"检查误判为缺失取值而崩渲染，统一替换成全角括号。"""
    return str(v).replace("{{", "〔").replace("}}", "〕")


def render_prompt(task: str, **kw) -> tuple[str, str]:
    system, user = load_prompt(task)
    for k, v in kw.items():
        # kw 显式传入的占位值（含 GENRE_GUIDE/MARKET_GUIDE 选择结果）优先，system/user 一并替换
        user = user.replace("{{" + k + "}}", _safe_val(v))
        system = system.replace("{{" + k + "}}", _safe_val(v))
    # 自动注入参考指南：{{MARKET_GUIDE}}/{{GENRE_GUIDE}} 从对应模板的 SYSTEM 段读取，
    # 使下沉市场/题材指导既能被写手提示词引用，又能在看板 Agent 管理中直接编辑。
    # 若调用方已在 kw 传入对应占位值（如按题材选择的文件内容），此处占位已消失、不会重复注入。
    for ph, guide in (("MARKET_GUIDE", "market_guide"), ("GENRE_GUIDE", "genre_guide")):
        if "{{" + ph + "}}" in system or "{{" + ph + "}}" in user:
            try:
                guide_sys, _ = load_prompt(guide)
                system = system.replace("{{" + ph + "}}", guide_sys)
                user = user.replace("{{" + ph + "}}", guide_sys)
            except Exception:  # noqa: BLE001
                system = system.replace("{{" + ph + "}}", "（指南缺失）")
                user = user.replace("{{" + ph + "}}", "（指南缺失）")
    # 未提供的占位符显式留空，避免漏看
    import re

    missing = re.findall(r"\{\{(\w+)\}\}", user)
    if missing:
        raise KeyError(f"prompt [{task}] 缺少占位符取值: {missing}")
    return system, user
