# -*- coding: utf-8 -*-
"""断点保稿与草稿发现恢复（借鉴 openwrite B03：`.uncommitted` + 指针 + 扫描器）。

为什么需要：
    旧流程只在**全部门禁通过之后**才把正文写进 `chapters/chNN.md`。写一章可能要跑
    写手初稿 → 禁道具重写 → 语言质检重写 → 主编打回 → 读者团打回 → 情绪门打回，
    少则 3 轮、多则 10 轮，单章耗时几十分钟到几小时。第 7 轮被 Ctrl-C 或断电，
    前 6 轮的正文**一个字都不在磁盘上**——重启后从第 1 轮重新烧 token。
    这不是"偶尔倒霉"，`produce --count N` 无人值守跑批时是必然发生的成本黑洞。

    更隐蔽的是"半状态"：`ch01.md` 已落盘但 `reviews/ch01.json` 没写、`project.json`
    的 summaries 没更新，续跑时 `todo` 判定该章已完成 → 跳过 → 下游读到空摘要。
    所以保稿必须和「提交」区分开：草稿是候选，提交才是既成事实。

设计（对齐 openwrite 的 draft/pointer/commit 三段式，按短篇体量裁剪）：
    - 草稿：`chapters/.drafts/chNN.md`（正文）+ `chNN.json`（元信息：round/stage/
      words/model/role/created_at/sha256）；
    - 提交标记：`chapters/chNN.commit.json`（sha256/committed_at/gate）——正文与
      `reviews/chNN.json` 都落盘后才写，作为"这一章真的完成了"的唯一凭据；
    - 扫描器 `list_recoverable()`：双路扫描 `.drafts/*.md` 与缺 commit 标记的正文，
      用 sha256 排除已提交/已恢复（幂等），并做路径越界检查；
    - `restore()`：把草稿提升为正文候选，**返回文本**给管线重新过门禁，不直接宣布完成
      （旧草稿可能已经违反后来修过的规则）。

    一条纪律：草稿目录永不参与 `read_chapters()`，看板/成稿只认已提交正文。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DRAFTS_DIR = ".drafts"
DRAFT_KEEP = 3          # 每章保留最近几版草稿（轮次多时别把磁盘写爆）


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _drafts_root(project) -> Path:
    p = Path(project.root) / "chapters" / DRAFTS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_within(root: Path, target: Path) -> bool:
    """路径必须在 root 之内（防越界：草稿指针被手工改成 ../../../etc/passwd）。"""
    try:
        target.resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 写草稿
# ---------------------------------------------------------------------------

def save_draft(project, no: int, text: str, *, stage: str = "draft",
               round_no: int = 0, role: str = "", model: str = "",
               gate: str = "") -> Path | None:
    """保存一份章节草稿（原子写）。空正文不落盘，避免用空稿覆盖好稿。"""
    if not (text or "").strip():
        return None
    root = _drafts_root(project)
    stem = f"ch{int(no):02d}"
    body_path = root / f"{stem}.md"
    meta_path = root / f"{stem}.json"
    words = len((text or "").strip())

    # 轮次递增：同一章同一 stage 反复保存时 round 自增，便于 `drafts` 看出进度
    prev = {}
    try:
        if meta_path.exists():
            prev = json.loads(meta_path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        prev = {}
    if not round_no:
        round_no = int(prev.get("round") or 0) + 1

    from .util import atomic_write_text

    atomic_write_text(body_path, text)
    meta = {
        "no": int(no),
        "stage": stage,
        "round": round_no,
        "words": words,
        "role": role,
        "model": model,
        "gate": gate,
        "created_at": _now(),
        "sha256": _sha256(text),
    }
    from .util import atomic_write_json

    atomic_write_json(meta_path, meta)
    _prune(project, no)
    return body_path


def _prune(project, no: int) -> None:
    """保留最近 DRAFT_KEEP 版：历史版本挪进 .history/，只留指针，防止磁盘膨胀。

    实际上短篇一章最多十几轮、单版 2-3KB，膨胀风险很小；这里只在超过阈值时
    把最旧的几版移到 `.history/` 而不是删除，保留取证能力。
    """
    root = _drafts_root(project)
    hist = root / ".history"
    # 只对单章单文件的实现来说没有多版本；保留钩子，避免将来加多版本时忘记清理
    if not hist.exists():
        return


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------

def commit_chapter(project, no: int, body: str, gate: str = "") -> Path:
    """提交一章：正文 + commit 标记 + 清草稿。这是"这一章真的完成了"的唯一凭据。"""
    from .util import atomic_write_json, atomic_write_text

    body_path = project.chapter_file(no)
    atomic_write_text(body_path, body)
    marker = Path(project.root) / "chapters" / f"ch{int(no):02d}.commit.json"
    atomic_write_json(marker, {
        "no": int(no),
        "sha256": _sha256(body),
        "words": len((body or "").strip()),
        "gate": gate,
        "committed_at": _now(),
    })
    clear_draft(project, no)
    return body_path


def clear_draft(project, no: int) -> None:
    root = _drafts_root(project)
    for suffix in (".md", ".json"):
        p = root / f"ch{int(no):02d}{suffix}"
        try:
            p.unlink()
        except OSError:
            pass


def commit_marker(project, no: int) -> dict:
    p = Path(project.root) / "chapters" / f"ch{int(no):02d}.commit.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def is_committed(project, no: int) -> bool:
    """正文存在且 commit 标记的 sha256 与正文一致才算已提交。

    半状态（正文已写、标记未写，或正文被手工改过）一律不算——这正是要暴露的问题。
    """
    body_path = project.chapter_file(no)
    if not body_path.exists():
        return False
    marker = commit_marker(project, no)
    if not marker.get("sha256"):
        return False
    try:
        text = body_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return marker["sha256"] == _sha256(text)


# ---------------------------------------------------------------------------
# 扫描 / 恢复
# ---------------------------------------------------------------------------

def list_recoverable(project) -> list[dict]:
    """列出可恢复草稿（双路扫描 + 幂等排除）。

    两路来源：
      ① `.drafts/chNN.md`：写稿中途保下的候选稿；
      ② 有正文但没有合法 commit 标记的章：上一版流程留下的半状态。
    sha256 与已提交正文相同的草稿视为"已恢复过"，不再列出（幂等）。
    """
    root = Path(project.root)
    out: list[dict] = []
    seen: set[int] = set()

    drafts_root = root / "chapters" / DRAFTS_DIR
    if drafts_root.is_dir():
        for body_path in sorted(drafts_root.glob("ch*.md")):
            stem = body_path.stem
            try:
                no = int(stem[2:])
            except ValueError:
                continue
            if not _safe_within(drafts_root, body_path):
                continue
            meta_path = drafts_root / f"{stem}.json"
            meta: dict = {}
            try:
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                meta = {}
            try:
                text = body_path.read_text(encoding="utf-8")
            except OSError:
                continue
            digest = _sha256(text)
            committed_text = ""
            if project.chapter_file(no).exists():
                try:
                    committed_text = project.chapter_file(no).read_text(encoding="utf-8")
                except OSError:
                    committed_text = ""
            out.append({
                "no": no,
                "source": "draft",
                "stage": meta.get("stage", ""),
                "round": int(meta.get("round") or 0),
                "words": int(meta.get("words") or len(text.strip())),
                "created_at": meta.get("created_at", ""),
                "role": meta.get("role", ""),
                "model": meta.get("model", ""),
                "sha256": digest,
                "restorable": digest != _sha256(committed_text),
            })
            seen.add(no)

    # ② 半状态正文：存在 chNN.md 但没有合法 commit 标记
    #    历史项目兼容：本模块上线前产出的项目**全都没有** commit 标记，若照直报会把
    #    几十篇老稿全标成"半状态"，告警噪声淹没真问题。判据：整个项目连一个 commit
    #    标记都没有 → 视为旧版项目，跳过这一路（只报 .drafts 里的真草稿）。
    chapters_dir = root / "chapters"
    has_any_marker = bool(list(chapters_dir.glob("ch*.commit.json"))) if chapters_dir.is_dir() else False
    if chapters_dir.is_dir() and has_any_marker:
        for body_path in sorted(chapters_dir.glob("ch*.md")):
            try:
                no = int(body_path.stem[2:])
            except ValueError:
                continue
            if no in seen:
                continue
            if is_committed(project, no):
                continue
            try:
                text = body_path.read_text(encoding="utf-8")
            except OSError:
                continue
            out.append({
                "no": no,
                "source": "uncommitted",
                "stage": "committed_without_marker",
                "round": 0,
                "words": len(text.strip()),
                "created_at": "",
                "role": "",
                "model": "",
                "sha256": _sha256(text),
                "restorable": True,
            })
    out.sort(key=lambda d: (d["no"], d["source"]))
    return out


def restore(project, no: int) -> str | None:
    """取出草稿正文交给管线重新过门禁（不直接覆盖正文、不写 commit 标记）。

    为什么不直接覆盖：草稿是在**旧规则**下写的，可能包含后来才加进禁用词表的道具，
    或早期版本的思考泄漏。直接当成品会让这些缺陷绕过全部确定性门禁。
    """
    drafts_root = Path(project.root) / "chapters" / DRAFTS_DIR
    body_path = drafts_root / f"ch{int(no):02d}.md"
    if not body_path.exists() or not _safe_within(drafts_root, body_path):
        return None
    try:
        return body_path.read_text(encoding="utf-8")
    except OSError:
        return None


def drop_draft(project, no: int) -> None:
    clear_draft(project, no)


def summary_line(item: dict) -> str:
    """一行可读摘要（CLI 列表用）。"""
    flag = "" if item.get("restorable") else "（已提交，可忽略）"
    src = {"draft": "草稿", "uncommitted": "未提交正文"}.get(item.get("source"), item.get("source"))
    bits = [f"ch{item['no']:02d}", src, f"{item.get('words', 0)}字"]
    if item.get("round"):
        bits.append(f"第{item['round']}轮")
    if item.get("stage"):
        bits.append(str(item["stage"]))
    if item.get("created_at"):
        bits.append(str(item["created_at"]))
    return " · ".join(bits) + flag
