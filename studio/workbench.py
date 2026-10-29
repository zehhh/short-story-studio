# -*- coding: utf-8 -*-
"""控制台读写层：config.yaml / Agent 提示词 / 热点来源 / 文稿 的读取与保存。

所有写操作先做 .bak 备份；config 用 yaml 全量重写（会丢失注释，备份可回滚）。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import yaml

from .config import ROOT, data_path

AGENT_META = {
    # task -> (中文名, 所属角色, 是否可在 UI 开关, 开关对应的 config 键)
    "mine": ("提梗策划", "planner", False, ""),
    "pitch": ("书名方案", "planner", False, ""),
    "lead": ("导语写手", "planner", False, ""),
    "outline": ("大纲官", "planner", False, ""),
    "bible": ("故事圣经", "planner", False, ""),
    "draft": ("执笔人", "writer", False, ""),
    "rewrite": ("重写写手", "writer", False, ""),
    "review": ("毒舌主编", "reviewer", False, ""),
    "reader_score": ("读者评分", "panel", False, ""),
    "emotion": ("情绪审查官", "reviewer", True, "review.emotion_enabled"),
    "kb_distill": ("知识蒸馏", "light", True, "knowledge.auto_collect"),
    "polish_fix": ("全稿体检", "reviewer", False, ""),
    "polish_final": ("终稿润色", "writer", False, ""),
    "material_from_hot": ("热点转换员", "light", False, ""),
    "material_curate": ("素材整理员", "light", True, "materials.auto_curate"),
    "decon_section": ("拆书·逐段", "planner", False, ""),
    "decon_book": ("拆书·全书归纳", "planner", False, ""),
}
GATE_TOGGLES = {
    "语言质检门": "review.langqa_enabled",
    "主编审查": "review.enabled",
    "读者评分团": "review.reader_enabled",
    "情绪量化门": "review.emotion_enabled",
    "题材知识库": "knowledge.auto_collect",
    "素材自动整理": "materials.auto_curate",
}
ROLES = ["planner", "writer", "reviewer", "panel", "light"]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def config_path(config_file: str | None) -> Path:
    return Path(config_file) if config_file else ROOT / "config.yaml"


def load_config_dict(config_file: str | None = None) -> dict:
    p = config_path(config_file)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_config_dict(patch: dict, config_file: str | None = None) -> dict:
    """深合并 patch 到现有 config.yaml 并保存（先备份）。"""
    p = config_path(config_file)
    cur = load_config_dict(config_file)
    _deep_merge(cur, patch)
    # 以 `_` 开头的键是运行时私有键（如 `_config_file`），绝不写回磁盘配置
    cur = {k: v for k, v in cur.items() if not str(k).startswith("_")}
    if p.exists():
        shutil.copy2(p, p.with_suffix(".yaml.bak"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump(cur, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return cur


def _deep_merge(base: dict, patch: dict):
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def get_nested(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_nested(d: dict, path: str, value):
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


# ---------------------------------------------------------------------------
# 热点来源
# ---------------------------------------------------------------------------

AVAILABLE_SOURCES = [
    "weibo", "zhihu", "baidu", "douyin", "toutiao", "bilibili", "tieba",
    "dailyhot", "mock", "custom",
]


def sources_state(cfg: dict) -> dict:
    scfg = cfg.get("studio", {})
    return {
        "available": AVAILABLE_SOURCES,
        "enabled": scfg.get("sources", []),
        "dailyhot_base": scfg.get("dailyhot_base", ""),
    }


def save_sources(cfg: dict, patch: dict, config_file: str | None = None) -> dict:
    studio = patch.get("studio", {})
    p = {"studio": {}}
    if "sources" in studio:
        p["studio"]["sources"] = [s.strip() for s in studio["sources"] if s.strip()]
    if "dailyhot_base" in studio:
        p["studio"]["dailyhot_base"] = studio["dailyhot_base"]
    save_config_dict(p, config_file)
    return sources_state({**cfg, "studio": {**cfg.get("studio", {}), **p["studio"]}})


# ---------------------------------------------------------------------------
# Agent 提示词
# ---------------------------------------------------------------------------

def list_agents() -> list[dict]:
    """已知流水线 agent + prompts/ 目录里的自定义模板。"""
    out = []
    for task, (name, role, toggleable, toggle_key) in AGENT_META.items():
        p = ROOT / "prompts" / f"{task}.md"
        out.append(
            {
                "task": task,
                "name": name,
                "role": role,
                "file": f"prompts/{task}.md",
                "exists": p.exists(),
                "toggleable": toggleable,
                "toggle_key": toggle_key,
                "custom": False,
            }
        )
    for p in sorted((ROOT / "prompts").glob("*.md")):
        task = p.stem
        if task not in AGENT_META:
            out.append(
                {
                    "task": task,
                    "name": task + "（自定义）",
                    "role": "custom",
                    "file": f"prompts/{task}.md",
                    "exists": True,
                    "toggleable": False,
                    "toggle_key": "",
                    "custom": True,
                }
            )
    return out


def read_agent(task: str) -> dict | None:
    if not re.fullmatch(r"[\w\-]+", task or ""):
        return None
    p = ROOT / "prompts" / f"{task}.md"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    system, user = "", ""
    if "===SYSTEM===" in text and "===USER===" in text:
        _, rest = text.split("===SYSTEM===", 1)
        system, user = rest.split("===USER===", 1)
    return {"task": task, "raw": text, "system": system.strip(), "user": user.strip()}


def save_agent(task: str, raw: str, create: bool = False) -> dict:
    if not re.fullmatch(r"[\w\-]{1,40}", task or ""):
        return {"ok": False, "error": "非法任务名"}
    if task in AGENT_META and not raw.lstrip().startswith(f"<!--TASK:{task}"):
        pass  # 已知任务允许改内容，标记行可保留也可缺失，但建议保留
    if "===USER===" not in raw:
        return {"ok": False, "error": "缺少 ===USER=== 分隔符"}
    if "===SYSTEM===" not in raw:
        return {"ok": False, "error": "缺少 ===SYSTEM=== 分隔符（模板必须有 SYSTEM 与 USER 两段）"}
    if raw.find("===SYSTEM===") > raw.find("===USER==="):
        return {"ok": False, "error": "分隔符顺序错误：===SYSTEM=== 必须在 ===USER=== 之前"}
    p = ROOT / "prompts" / f"{task}.md"
    if p.exists():
        shutil.copy2(p, p.with_suffix(".md.bak"))
    elif not create and task not in AGENT_META:
        return {"ok": False, "error": "任务不存在（新建请用 create=true）"}
    p.write_text(raw, encoding="utf-8")
    return {"ok": True, "task": task, "file": str(p)}


def delete_agent(task: str) -> dict:
    """删除自定义提示词（内置 AGENT_META 任务不可删，流水线依赖）。删除前自动备份 .md.bak。"""
    if not re.fullmatch(r"[\w\-]{1,40}", task or ""):
        return {"ok": False, "error": "非法任务名"}
    if task in AGENT_META:
        return {"ok": False, "error": "内置 agent 不可删除（流水线依赖）"}
    p = ROOT / "prompts" / f"{task}.md"
    if not p.exists():
        return {"ok": False, "error": "任务不存在"}
    shutil.copy2(p, p.with_suffix(".md.bak"))
    p.unlink()
    return {"ok": True, "task": task, "backup": str(p.with_suffix(".md.bak"))}


# ---------------------------------------------------------------------------
# 文稿
# ---------------------------------------------------------------------------

def _safe_manuscript_rel(project_root: Path, rel: str) -> Path | None:
    rel = (rel or "").replace("\\", "/").strip("/")
    if rel.endswith("/"):
        return None
    p = (project_root / rel).resolve()
    try:
        p.relative_to(project_root.resolve())
    except ValueError:
        return None
    allowed = (
        rel == "final/成稿.md"
        or rel == "final/成稿.txt"
        or rel == "final/improvement.md"
        or re.fullmatch(r"chapters/ch\d+\.md", rel)
    )
    return p if allowed else None


def list_manuscript(project_root: Path) -> list[dict]:
    files = []
    for rel in ["final/成稿.md", "final/成稿.txt", "final/improvement.md"]:
        p = project_root / rel
        if p.exists():
            files.append(
                {"rel": rel, "size": p.stat().st_size, "updated": p.stat().st_mtime}
            )
    chdir = project_root / "chapters"
    if chdir.exists():
        for p in sorted(chdir.glob("ch*.md")):
            files.append(
                {"rel": f"chapters/{p.name}", "size": p.stat().st_size, "updated": p.stat().st_mtime}
            )
    return files


def read_manuscript(project_root: Path, rel: str) -> dict | None:
    p = _safe_manuscript_rel(project_root, rel)
    if p is None or not p.exists():
        return None
    return {"rel": rel, "content": p.read_text(encoding="utf-8")}


def save_manuscript(project_root: Path, rel: str, content: str) -> dict:
    p = _safe_manuscript_rel(project_root, rel)
    if p is None:
        return {"ok": False, "error": "不允许的文件路径"}
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "rel": rel, "size": len(content)}


# ---------------------------------------------------------------------------
# 项目删除（连带 analytics 数据）
# ---------------------------------------------------------------------------

def _projects_base(cfg: dict) -> Path:
    return Path(cfg["studio"]["workspace_abs"]) / "projects"


def list_projects_brief(cfg: dict) -> list[dict]:
    base = _projects_base(cfg)
    out = []
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir():
                out.append({"name": d.name, "has_report": (d / "report.json").exists()})
    return out


def delete_project(cfg: dict, name: str) -> dict:
    """彻底删除项目目录及其 analytics 数据。name 必须与目录名精确匹配。"""
    base = _projects_base(cfg)
    root = None
    if base.exists():
        for d in base.iterdir():
            if d.is_dir() and d.name == name:
                root = d
                break
    if root is None:
        return {"ok": False, "error": "项目不存在"}
    shutil.rmtree(root, ignore_errors=True)
    adir = Path(cfg["studio"]["workspace_abs"]) / "analytics" / name
    if adir.exists():
        shutil.rmtree(adir, ignore_errors=True)
    return {"ok": True, "deleted": name}
