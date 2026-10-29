# -*- coding: utf-8 -*-
"""项目存储：每个故事一个目录，JSON/Markdown 落盘，支持断点续跑。

所有写操作走 `util.atomic_write_text/json`（临时文件 → fsync → replace）：
长任务被强杀时，留下的是"完整的旧版"而不是半截 JSON/半截章节，
断点续跑不会读到坏文件（借鉴 openwrite 的原子写纪律）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .util import atomic_write_json, atomic_write_text, date_stamp, now_ts, slugify

STAGES = ["hotspots", "concepts", "concept", "pitch", "lead", "outline", "bible", "chapters", "final"]

# project.json 的读-改-写必须串行：atomic_write_json 只保证"不写出半个文件"，
# 不保证"不丢更新"。同一章内会多次 save_meta（换衔接状态、落 reviews、改 status），
# 章级门禁又是 ThreadPoolExecutor 并行，两个线程各自 load → 改 → 整份覆盖，
# 后者会把前者的 chapters_state / reviews / status 整份回退（typical lost update）。
# 跨进程由 lock.project_lock 负责，这里管进程内的线程。
_META_LOCK = threading.RLock()


class Project:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "chapters").mkdir(exist_ok=True)
        (self.root / "final").mkdir(exist_ok=True)

    # -- 元信息 ------------------------------------------------------------
    @property
    def meta(self) -> dict:
        """项目元信息快照。注意：**这是每次访问都重新读盘的 property**，
        改它必须走 `update_meta()` —— 写过 `project.meta["k"]=v; save_meta(project.meta)`
        的人都会踩坑：后半句又读了一份干净 dict，改动凭空消失。
        """
        m = self.load_json("project.json")
        if m is not None:
            return m
        return {"name": self.root.name, "created_at": now_ts(), "status": {}}

    def update_meta(self, **kv) -> dict:
        """读-改-写一次的原子元信息更新（替代 `project.meta[k]=v` 那套写法）。

        持 `_META_LOCK`：见模块头注释——并行门禁下这是 lost update 的唯一防线。
        """
        with _META_LOCK:
            meta = self.meta
            meta.update(kv)
            self.save_meta(meta)
        return meta

    def save_meta(self, meta: dict):
        meta.setdefault("name", self.root.name)
        meta.setdefault("created_at", now_ts())
        meta.setdefault("status", {})
        atomic_write_json(self.root / "project.json", meta)

    def set_stage(self, stage: str, detail: str = "done"):
        # 必须持锁包住"读 → 改 → 写"，否则与同进程内另一个线程的 save_meta 交错
        with _META_LOCK:
            meta = self.meta
            meta.setdefault("status", {})[stage] = detail
            self.save_meta(meta)

    def merge_meta(self, patch_fn) -> dict:
        """锁内读-改-写：`patch_fn(meta)` 原地改，返回值无用。

        并行阶段写 meta 的正确姿势（`update_meta(**kv)` 只做顶层浅更新，
        改 `reviews["3"]` / `chapters_state` 这类嵌套键必须走这里）。
        """
        with _META_LOCK:
            meta = self.meta
            patch_fn(meta)
            self.save_meta(meta)
        return meta

    def stage_status(self) -> dict:
        return self.meta.get("status", {})

    # -- 文件读写 ------------------------------------------------------------
    def save_json(self, name: str, obj, subdir: str = ""):
        p = self._path(name, subdir)
        atomic_write_json(p, obj)
        return p

    def load_json(self, name: str, subdir: str = ""):
        p = self._path(name, subdir)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # 半截 JSON = 上次写入被强杀；不静默当"不存在"（那会重跑整段），
            # 明确报错并提示文件位置，便于人工决定删除重跑还是修复。
            raise SystemExit(
                f"项目文件损坏（JSON 解析失败）：{p}\n"
                f"  原因：{e}\n"
                f"  多半是上次运行被强杀留下半截文件。可删除该文件后重跑（对应阶段会重新生成）。"
            ) from e

    def save_text(self, name: str, text: str, subdir: str = ""):
        p = self._path(name, subdir)
        atomic_write_text(p, text)
        return p

    def read_text(self, name: str, subdir: str = "") -> str | None:
        p = self._path(name, subdir)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def has(self, name: str, subdir: str = "") -> bool:
        return self._path(name, subdir).exists()

    def _path(self, name: str, subdir: str) -> Path:
        p = self.root / subdir / name if subdir else self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def chapter_file(self, no: int) -> Path:
        return self.root / "chapters" / f"ch{no:02d}.md"

    def read_chapters(self) -> list[dict]:
        out = []
        for f in sorted((self.root / "chapters").glob("ch*.md")):
            no = int(f.stem[2:])
            out.append({"no": no, "text": f.read_text(encoding="utf-8")})
        return out


def create_project(workspace: Path, title: str) -> Project:
    name = f"{date_stamp()}_{slugify(title)}"
    root = workspace / "projects" / name
    # 防撞：同名目录已存在（同日同 idea 重跑）则加序号，避免混入上一轮的半成品状态
    n = 0
    while root.exists() and any(root.iterdir()):
        n += 1
        root = workspace / "projects" / f"{name}_{n}"
    return Project(root)


def latest_project(workspace: Path) -> Project | None:
    base = Path(workspace) / "projects"
    if not base.exists():
        return None
    dirs = sorted([d for d in base.iterdir() if d.is_dir()])
    return Project(dirs[-1]) if dirs else None


def find_project(workspace: Path, keyword: str) -> Project | None:
    base = Path(workspace) / "projects"
    if not base.exists():
        return None
    dirs = sorted([d for d in base.iterdir() if d.is_dir()])
    hits = [d for d in dirs if keyword in d.name]
    if not hits:
        raise SystemExit(f"找不到匹配的项目：{keyword}（现有 {[d.name for d in dirs]}）")
    return Project(hits[-1])
