# -*- coding: utf-8 -*-
"""素材库（知识库系统）：自动沉淀创作素材，并按 类型×题材×标签 分类。

设计：
- 存储：workspace/materials.jsonl，一行一条（追加式，带 .bak 备份后重写）；
- 类型体系：标题 / 导语 / 开头钩子 / 章末钩子 / 核心梗 / 反转设计 / 伏笔设计 /
  人设卡 / 情绪爆点 / 修改教训 / 热点素材；
- 自动沉淀：成稿后从项目产物（pitch/lead/outline/bible/reviews/hotspots）抽取入库，
  按 (source_project, type, text指纹) 去重，重复沉淀不会翻倍；
- 分类：type（抽取即知）+ direction（题材）+ 自动标签（数字梗/偷听/倒计时/假死/对账/
  重生/打脸/晕倒吐血/全世界/直播…关键词规则，可在 data/material_tags.json 扩展）；
- 反哺：digest() 挑高分素材压缩后注入 mine 选题提示词。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .config import data_path
from .util import now_ts

TYPES = ["标题", "导语", "开头钩子", "章末钩子", "核心梗", "反转设计", "伏笔设计",
         "人设卡", "情绪爆点", "修改教训", "热点素材", "金句", "写作技法"]

DEFAULT_TAG_RULES = {
    "数字梗": r"\d{2,}|九百九十九|一千零",
    "偷听": r"偷听|听到.*对话",
    "倒计时": r"倒计时|还有\d+天|最后一天",
    "假死": r"假死|讣告|遗书|葬礼",
    "对账": r"对账|账本|流水|记账",
    "重生": r"重生|上一世|前世",
    "打脸": r"打脸|反击|反杀",
    "晕倒吐血": r"吐血|晕倒|昏厥",
    "全世界": r"全世界|全网|全球",
    "直播": r"直播|热搜",
    "公证": r"公证|判决书|协议",
    "净身出户": r"净身出户|离婚协议",
}


def materials_path(cfg: dict) -> Path:
    p = Path(cfg["studio"]["workspace_abs"]) / "materials.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def trash_path(cfg: dict) -> Path:
    p = Path(cfg["studio"]["workspace_abs"]) / "materials_trash.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load(cfg: dict) -> list[dict]:
    p = materials_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _save(cfg: dict, items: list[dict]):
    """全库重写。走原子写：素材库是全量重写的单文件，写一半崩掉就是整库丢失。"""
    from .util import atomic_write_text

    p = materials_path(cfg)
    if p.exists():
        import shutil

        shutil.copy2(p, p.with_suffix(".jsonl.bak"))
    atomic_write_text(p, "\n".join(json.dumps(x, ensure_ascii=False) for x in items))


def _fingerprint(text: str) -> str:
    return hashlib.md5(re.sub(r"\s", "", text).encode("utf-8")).hexdigest()[:12]


def _tag_rules() -> dict:
    custom = data_path("material_tags.json")
    if custom.exists():
        try:
            return json.loads(custom.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return DEFAULT_TAG_RULES


def auto_tags(text: str) -> list[str]:
    return [tag for tag, pat in _tag_rules().items() if re.search(pat, text)]


def _direction_of(cfg: dict, text: str, fallback: str = "") -> str:
    """无 LLM 兜底：按梗库关键词给素材归属题材。"""
    try:
        from .pipeline import match_direction

        return match_direction(text) if not fallback else fallback
    except Exception:  # noqa: BLE001
        return fallback or "未分类"


def add_material(cfg: dict, mtype: str, text: str, direction: str = "",
                 platform: str = "", tags: list[str] | None = None,
                 source_project: str = "", score: float | None = None,
                 note: str = "") -> dict:
    items = _load(cfg)
    fp = _fingerprint(f"{mtype}|{text}")
    for it in items:
        if it.get("fp") == fp:
            return it  # 去重
    item = {
        "id": "M" + now_ts().replace("-", "").replace(" ", "").replace(":", "") + f"{len(items) % 100:02d}",
        "type": mtype if mtype in TYPES else "核心梗",
        "text": text.strip()[:600],
        "direction": direction or "未分类",
        "platform": platform,
        "tags": sorted(set((tags or []) + auto_tags(text))),
        "source_project": source_project,
        "score": score,
        "note": note,
        "fp": fp,
        "quality": "mid",
        "curated": False,
        "created_at": now_ts(),
    }
    items.append(item)
    _save(cfg, items)
    return item


def remove_material(cfg: dict, material_id: str) -> bool:
    items = _load(cfg)
    kept = [x for x in items if x.get("id") != material_id]
    if len(kept) == len(items):
        return False
    _save(cfg, kept)
    return True


def _load_trash(cfg: dict) -> list[dict]:
    p = trash_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def list_trash(cfg: dict) -> list[dict]:
    return list(reversed(_load_trash(cfg)))


def restore_material(cfg: dict, material_id: str) -> bool:
    """从回收站恢复（fp 相同的现存素材会被先移除，避免重复）。"""
    tr = _load_trash(cfg)
    item = next((x for x in tr if x.get("id") == material_id), None)
    if not item:
        return False
    items = [x for x in _load(cfg) if x.get("fp") != item.get("fp")]
    items.append(item)
    _save(cfg, items)
    _save_trash(cfg, [x for x in tr if x.get("id") != material_id])
    return True


def _save_trash(cfg: dict, items: list[dict]):
    p = trash_path(cfg)
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items), encoding="utf-8")


def _to_trash(cfg: dict, item: dict):
    tr = _load_trash(cfg)
    tr.append({**item, "trashed_at": now_ts()})
    _save_trash(cfg, tr)


def query(cfg: dict, mtype: str = "", direction: str = "", keyword: str = "",
          limit: int = 50) -> list[dict]:
    items = _load(cfg)
    kw = keyword.strip().lower()
    out = []
    for it in reversed(items):  # 新的在前
        if mtype and it.get("type") != mtype:
            continue
        if direction and it.get("direction") != direction:
            continue
        if kw and kw not in json.dumps(it, ensure_ascii=False).lower():
            continue
        out.append(it)
        if len(out) >= limit:
            break
    return out


def stats(cfg: dict) -> dict:
    items = _load(cfg)
    return {
        "total": len(items),
        "by_type": dict(Counter(x.get("type", "?") for x in items)),
        "by_direction": dict(Counter(x.get("direction", "?") for x in items).most_common(10)),
        "by_quality": dict(Counter(x.get("quality", "未整理") for x in items)),
        "curated": sum(1 for x in items if x.get("curated")),
    }


# ---------------------------------------------------------------------------
# 自动沉淀：从项目产物抽取素材
# ---------------------------------------------------------------------------

def convert_hotspots(cfg: dict, llm, items: list[dict], direction: str = "",
                     batch_size: int = 10) -> list[dict]:
    """素材转换门：热点事件 → 可用小说素材（核心梗/钩子）。不可用的直接丢弃。"""
    from .prompts import render_prompt

    out: list[dict] = []
    items = [x for x in items if (x.get("title") or "").strip()]
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        listing = "\n".join(
            f"{k + 1}. {it.get('title', '')[:110]}" for k, it in enumerate(batch)
        )
        sys_p, user_p = render_prompt(
            "material_from_hot", DIRECTION=direction or "不限", ITEMS=listing
        )
        try:
            data = llm.chat_json(sys_p, user_p)
        except Exception as e:  # noqa: BLE001
            print(f"    [convert] 转换批次失败，本批丢弃（{e}）")
            continue
        for k, v in enumerate(data.get("items", [])):
            if not v.get("usable"):
                continue
            src = batch[k] if k < len(batch) else None
            out.append({
                "type": "开头钩子",
                "text": f"【梗】{v.get('core_trope', '')}｜【钩】{v.get('hook', '')}",
                "direction": v.get("direction") or direction or "未分类",
                "tags": ["热点改编", "novel"],
                "score": None,
                "note": f"自热点：{(src or {}).get('title', '')[:40]}",
                "source_project": src.get("source", "") if src else "hot",
            })
    return out


def deposit_from_project(cfg: dict, project, force: bool = False,
                          convert_llm=None) -> dict:
    """成稿后自动抽取素材入库。返回 {added, skipped}。"""
    from .util import count_words_zh

    name = project.root.name
    existing_fps = {x.get("fp") for x in _load(cfg)}
    pitch = project.load_json("pitch.json") or {}
    concept = project.load_json("concept.json") or {}
    lead = project.load_json("lead.json") or {}
    outline = project.load_json("outline.json") or {}
    report = project.load_json("report.json") or {}
    direction = concept.get("direction", "")
    platform = pitch.get("platform", "")

    raw: list[tuple[str, str, float | None]] = []  # (type, text, score)

    for t in pitch.get("titles", []) or []:
        raw.append(("标题", t, None))
    if pitch.get("logline"):
        raw.append(("核心梗", pitch["logline"], None))
    if concept.get("core_trope"):
        raw.append(("核心梗", concept["core_trope"], None))
    if concept.get("hook"):
        raw.append(("开头钩子", concept["hook"], None))
    if lead.get("text"):
        raw.append(("导语", lead["text"], None))

    for ch in outline.get("chapters", []) or []:
        if ch.get("end_hook"):
            raw.append(("章末钩子", f"小节{int(ch.get('no')):02d} {ch.get('title','')}：{ch['end_hook']}", None))
        for b in ch.get("beats", []) or []:
            if "反转" in b:
                raw.append(("反转设计", f"小节{int(ch.get('no')):02d}：{b}", None))
    for f in outline.get("foreshadows", []) or []:
        if f.get("content"):
            raw.append(("伏笔设计", f"{f.get('id')}：{f.get('content')}（{f.get('plant_chapter')}章埋/{f.get('payoff_chapter')}章收）", None))

    bible = project.load_json("bible.json") or {}
    for c in bible.get("characters", []) or []:
        if c.get("name"):
            raw.append(("人设卡", f"{c['name']}（{c.get('role','')}，{c.get('age','?')}岁，{c.get('occupation','')}）：{c.get('traits','')}"
                       + (f"｜秘密：{c.get('secret')}" if c.get("secret") else ""), None))

    reviews = project.meta.get("reviews", {})
    for no_str, rev in reviews.items():
        if not isinstance(rev, dict):
            continue
        emo = rev.get("emotion") or {}
        if isinstance(emo, dict) and emo.get("strongest"):
            raw.append(("情绪爆点", f"第{no_str}章：{emo['strongest']}", rev.get("review_score")))

    # 已通过门禁章节的修改意见 → 教训（只收 pass 章的，信号干净）
    for no_str, rev in reviews.items():
        if not (isinstance(rev, dict) and rev.get("gate") == "pass"):
            continue
        chrev = project.load_json(f"ch{int(no_str):02d}.json", subdir="reviews") or {}
        ch_issues = chrev.get("issues") or []
        if isinstance(ch_issues, str):   # 部分模型把 issues 输出成字符串
            ch_issues = []
        for iss in ch_issues:
            if isinstance(iss, dict) and iss.get("fix"):
                raw.append(("修改教训", f"第{no_str}章[{iss.get('criterion','')}] {iss.get('note','')} → {iss.get('fix','')}", None))

    # 热点事件不直接入库：必须有转换 LLM 把事件改写成小说素材，否则跳过
    hotspots = project.load_json("hotspots.json") or []
    if convert_llm is not None and hotspots:
        try:
            converted = convert_hotspots(cfg, convert_llm, hotspots[:15],
                                         direction=concept.get("direction", ""))
            for c in converted:
                raw.append(("开头钩子", c["text"], None))
            if converted:
                print(f"    [convert] 热点转小说素材：{len(converted)}/{min(len(hotspots), 15)} 条可用")
            else:
                print("    [convert] 本批热点均无可转化素材")
        except Exception as e:  # noqa: BLE001
            print(f"    [convert] 转换失败（热点跳过）：{e}")

    added = 0
    skipped = 0

    # 专名脱敏：本篇 bible 角色名 → 角色占位，防止下篇照搬人名班子
    try:
        from .propername import build_name_map, clean_proper_names
    except Exception:
        build_name_map, clean_proper_names = None, None
    name_map = build_name_map(bible) if build_name_map else {}

    def _clean(t: str) -> str:
        """入库阀门：剥大纲引擎编号头（小节NN/第N章/拍N），拒收截断文本。"""
        t = re.sub(r"^\s*第\s*\d+\s*章\s*", "", t)
        t = re.sub(r"^\s*小节\s*\d{1,2}\s*", "", t)
        t = re.sub(r"^\s*拍\s*\d+\s*[（(]?[^）)]{0,14}[）)]?\s*[:：]?", "", t)
        if clean_proper_names and name_map:
            t = clean_proper_names(t, name_map)
        return t.strip()

    # 人设卡类型：去掉“名字（”前缀，只保留 角色/年龄/职业/特质/秘密（名字已被专名清洗替换为占位）
    raw = [(mt, (re.sub(r"^[^（(]+（", "（", str(t)) if mt == "人设卡" else t), sc) for mt, t, sc in raw]
    for mtype, text, score in raw:
        text = _clean(str(text))
        if len(text) < 6 or count_words_zh(text) < 4:
            continue
        if text[-1] in "：（(、，；":   # 半句截断=流程残渣，不入库
            continue
        fp = _fingerprint(f"{mtype}|{text}")
        if fp in existing_fps and not force:
            skipped += 1
            continue
        existing_fps.add(fp)
        add_material(
            cfg, mtype, text,
            direction=direction or _direction_of(cfg, text),
            platform=platform, source_project=name, score=score,
        )
        added += 1
    return {"added": added, "skipped": skipped}


# ---------------------------------------------------------------------------
# 反哺生成
# ---------------------------------------------------------------------------

def curate(cfg: dict, llm, direction: str = "", batch_size: int = 15,
           max_batches: int = 6) -> dict:
    """整理 Agent：批量质量评估 + 分类纠偏 + 低质量素材入回收站。"""
    from .prompts import render_prompt

    items = _load(cfg)
    pending = [x for x in items
               if not x.get("curated") and (not direction or x.get("direction") == direction)]
    pending.sort(key=lambda x: -(x.get("score") or 0))
    by_id = {x["id"]: x for x in items}
    result = {"checked": 0, "removed": 0, "high": 0, "mid": 0, "low": 0, "corrected": 0, "batches": 0}
    tropes = json.loads(data_path("tropes.json").read_text(encoding="utf-8"))["directions"]
    dir_names = [d["name"] for d in tropes]

    for bi in range(max_batches):
        batch = pending[bi * batch_size : (bi + 1) * batch_size]
        if not batch:
            break
        listing = "\n".join(
            f"{x['id']} | 类型:{x.get('type')} | 题材:{x.get('direction')} | {x.get('text','')[:90]}"
            for x in batch
        )
        sys_p, user_p = render_prompt(
            "material_curate", DIRECTION=direction or "全部", DIR_NAMES="、".join(dir_names),
            LISTING=listing, TYPE_LIST="、".join(TYPES)
        )
        try:
            data = llm.chat_json(sys_p, user_p)
        except Exception as e:  # noqa: BLE001
            print(f"    [curate] 整理输出解析失败，本批跳过：{e}")
            break
        result["batches"] += 1
        for v in data.get("items", []):
            it = by_id.get(v.get("id"))
            if it is None or it.get("curated"):
                continue
            q = v.get("quality")
            it["curated"] = True
            it["curated_at"] = now_ts()
            if q in ("high", "mid", "low"):
                it["quality"] = q
                result[q] = result.get(q, 0) + 1
            if v.get("better_type") and v["better_type"] in TYPES and v["better_type"] != it.get("type"):
                it["type"] = v["better_type"]
                result["corrected"] += 1
            if v.get("better_direction") and v["better_direction"] in dir_names and v["better_direction"] != it.get("direction"):
                it["direction"] = v["better_direction"]
                result["corrected"] += 1
            extra = v.get("tags_add") or []
            if extra:
                it["tags"] = sorted(set(it.get("tags", []) + [str(t) for t in extra]))
            if v.get("reason"):
                it["note"] = str(v["reason"])[:120]
            if q == "low" and v.get("keep") is False:
                _to_trash(cfg, it)          # 进回收站
                by_id.pop(v["id"], None)
                result["removed"] += 1
            result["checked"] += 1
        # 写回（移除已入回收站的）
        removed_ids = {x["id"] for x in _load_trash(cfg)}
        _save(cfg, [x for x in items if x["id"] not in removed_ids or not x.get("_trash_done")])
    # 统一落盘：剔除回收过的
    trashed_ids = {x["id"] for x in _load_trash(cfg)}
    _save(cfg, [x for x in items if x["id"] not in trashed_ids])
    return result


def _mat_bigrams(text: str) -> list[str]:
    """字符二元组（用于素材间相似度去重）。"""
    cjk = re.findall(r"[一-龥]", (text or "").lower())
    out = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return out or cjk


def digest(cfg: dict, direction: str = "", mtypes: list[str] | None = None,
           n: int = 8, max_chars: int = 800) -> str:
    """挑高分/新素材压缩成可注入 prompt 的摘要。

    多样性优先（修复"使用即强化"导致的套路固化）：
    - 近期使用过的素材降权（last_used 越近降越多），不再让高频素材永远插队；
    - MMR 去重：字符二元组 Jaccard 过高的相似素材只留一条，避免注入一堆同质套路；
    - **禁用道具过滤**：命中 config.kb.negative_terms 的素材直接丢弃。素材库是
      从爆款拆解里攒的，账本/存折/借条类内容很多（实测 1367 条里 198 条命中），
      不过滤就会被注入提示词、再被模型写回正文（"成稿老出现账本"的根源之一）。
    """
    import time as _time

    from .kb import _is_negative, _negative_terms

    neg_terms = _negative_terms(cfg)
    mtypes = mtypes or ["标题", "开头钩子", "章末钩子", "反转设计"]
    items = query(cfg, direction=direction, limit=400)
    items = [x for x in items if not _is_negative(x.get("text", ""), neg_terms)]
    pool = [x for x in items if x.get("type") in mtypes] or items
    if not pool:
        return ""
    qw = {"high": 3.0, "mid": 2.0}
    now = _time.time()

    def _recency_penalty(x: dict) -> float:
        lu = x.get("last_used")
        if lu:
            try:
                age_h = (now - float(lu)) / 3600.0
                if age_h < 24:
                    return 1.5      # 一天内用过：强降权
                if age_h < 72:
                    return 0.8      # 三天内用过：中降权
            except (TypeError, ValueError):
                pass
        return 0.0

    def _rank_key(x: dict) -> float:
        base = max(x.get("score") or 0, 0) * 0.4 + qw.get(x.get("quality"), 2.2)
        return -(base - _recency_penalty(x))

    pool.sort(key=_rank_key)
    lines, used, sel_bgs = [], 0, []
    for x in pool:
        bgs = frozenset(_mat_bigrams(x.get("text", "")))
        if bgs and any(len(bgs & s) > 0.5 * len(bgs | s) for s in sel_bgs):
            continue
        line = f"- [{x['type']}·{x['direction']}] {x['text'][:80]}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
        sel_bgs.append(bgs)
        if len(lines) >= n:
            break
    return "\n".join(lines)
