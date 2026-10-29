# -*- coding: utf-8 -*-
"""记忆库——参考 VCPToolBox 多层级持久化记忆思想的轻量实现。

设计来源：lioensky/VCPToolBox（GitHub, MIT）记忆系统后端思想：
  1) 记忆引力（信息主动流向模型，而非被动等待查询）；
  2) 标签语义河道（共享标签的记忆单元互相增权，联想沿语义脉络扩散）；
  3) 冷热分层（L1 工作记忆 / L2 短期 / L3 长期 / L4 冷归档，高热上浮、低热沉降）；
  4) 使用即强化（被检索命中的记忆增加热度，长期不用热度衰减）。

实现约束：零第三方依赖、不建独立存储——素材库 materials.jsonl 即记忆单元池，
usage_count / last_used / layer 作为动态字段读写；检索复用 rag 的 BM25 倒排做种子，
再用标签河道做一跳联想扩散。教训库（lessons.jsonl）作为 L2 短期通道一并纳入。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import material, rag

HEAT_DECAY = 0.85          # 整理时长期未用素材的热度衰减系数
STALE_DAYS = 30            # 超过该天数未使用的素材视为"冷"
TAG_FLOW_BOOST = 0.55      # 标签河道一跳联想的增益权重
QUALITY_WEIGHT = {"high": 1.0, "mid": 0.5, "low": 0.0}


def _now() -> float:
    return time.time()


def _ts(it: dict, now: float | None = None) -> float:
    """素材时间戳：优先 last_used，退 created_at（字符串 'YYYY-MM-DD HH:MM:SS'）。"""
    v = it.get("last_used")
    if v:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    c = str(it.get("created_at") or "").strip()
    if c:
        try:
            return time.mktime(time.strptime(c[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
    return now if now is not None else _now()


def _load(cfg: dict) -> list[dict]:
    items = material._load(cfg)
    for it in items:
        it.setdefault("usage_count", 0)
        it.setdefault("last_used", 0.0)
    return items


def _save(cfg: dict, items: list[dict]) -> None:
    material._save(cfg, items)


# ---------------------------------------------------------------- 标签语义河道

def tag_graph(items: list[dict]) -> dict[str, dict[str, int]]:
    """共现河道：tag -> {相邻tag: 共现次数}。共享标签越多的记忆离得越近。"""
    g: dict[str, dict[str, int]] = {}
    for it in items:
        tags = [t for t in (it.get("tags") or []) if t]
        for i, a in enumerate(tags):
            g.setdefault(a, {})
            for b in tags:
                if a != b:
                    g[a][b] = g[a].get(b, 0) + 1
    return g


def _seed_tags(query: str, seed_items: list[dict], items: list[dict]) -> dict[str, float]:
    """种子标签：查询词直配 + 种子命中素材自身标签（带质量权重）。"""
    from .material import auto_tags
    seed: dict[str, float] = {}
    for t in auto_tags(query):
        seed[t] = seed.get(t, 0.0) + 1.0
    by_id = {it.get("id"): it for it in items}
    for d in seed_items:
        it = by_id.get(d.get("id"))
        if not it:
            continue
        w = QUALITY_WEIGHT.get(it.get("quality") or "mid", 0.5)
        for t in (it.get("tags") or []):
            seed[t] = seed.get(t, 0.0) + 0.5 * w
    return seed


# ---------------------------------------------------------------- 记忆引力检索

def recall(cfg: dict, query: str, direction: str = "", max_chars: int = 700,
           top_k: int = 10, reinforce: bool = True) -> tuple[str, list[str]]:
    """引力检索：BM25 种子 + 标签河道一跳扩散 + 质量/热度加权。

    返回 (可注入 prompt 的记忆块, 被使用的素材 id 列表)。命中即强化（usage_count++）。
    """
    items = _load(cfg)
    live = [it for it in items if it.get("quality") != "低"]
    graph = tag_graph(live)

    # 1) BM25 语义种子（含 RAG 索引里的知识卡/教训/拆书）
    seeds = rag.search(cfg, f"{query} {direction}", top_k=top_k)

    # 2) 标签河道一跳联想：与种子共享标签的素材获得河道增益
    seed_tags = _seed_tags(query, seeds, live)
    flow: dict[str, float] = {}
    for t, tw in seed_tags.items():
        for nb, co in graph.get(t, {}).items():
            flow[nb] = flow.get(nb, 0.0) + TAG_FLOW_BOOST * tw * min(co, 3)
    by_text = {}
    for it in live:
        for t in (it.get("tags") or []):
            if t in flow:
                by_text[it.get("id")] = max(by_text.get(it.get("id"), 0.0), flow[t])

    # 3) 合并排序：词法分 + 河道增益 + 质量权重；同方向加权（mid/id 双通道去重）
    def _key(d: dict) -> str:
        return str(d.get("mid") or d.get("id") or (d.get("text") or "")[:40])

    merged: dict[str, dict] = {}
    for d in seeds:
        merged[_key(d)] = {**d, "via": "bm25"}
    for iid, boost in by_text.items():
        it = next((x for x in live if x.get("id") == iid), None)
        if not it:
            continue
        k = str(iid)
        if k in merged:
            merged[k]["score"] = merged[k].get("score", 0) + boost
        else:
            merged[k] = {"id": iid, "source": "materials", "type": it.get("type", ""),
                           "text": it.get("text", ""), "direction": it.get("direction", ""),
                           "score": boost, "via": "tagflow"}
    ranked = sorted(merged.values(), key=lambda d: -(
        d.get("score", 0)
        + QUALITY_WEIGHT.get(d.get("quality") or "mid", 0.5)
        + (0.4 if direction and d.get("direction") == direction else 0.0)))

    lines, used_ids, used_ch = [], [], 0
    # 禁用道具过滤：这条路径会混入 rag.search 的种子与标签扩散结果，
    # 两者都可能带账本类桥段（实测不投毒时也会召回）。命中即跳过，不注入提示词。
    from .kb import _is_negative, _negative_terms

    neg_terms = _negative_terms(cfg)
    for d in ranked:
        text = (d.get("text") or "").strip()
        if not text:
            continue
        if _is_negative(text, neg_terms):
            continue
        line = f"- [{d.get('source')}·{d.get('type')}{'·联想' if d.get('via') == 'tagflow' else ''}] {text[:110]}"
        if used_ch + len(line) > max_chars:
            break
        lines.append(line)
        used_ch += len(line)
        if d.get("id") or d.get("mid"):
            used_ids.append(str(d.get("id") or d.get("mid")))
    block = "\n".join(lines)
    if reinforce and used_ids:
        touch(cfg, used_ids)
    return block, used_ids


def touch(cfg: dict, ids: list[str]) -> int:
    """使用即强化：命中素材 usage_count++ 并刷新 last_used。返回更新条数。"""
    if not ids:
        return 0
    items = _load(cfg)
    idset = set(ids)
    n = 0
    for it in items:
        if it.get("id") in idset:
            it["usage_count"] = int(it.get("usage_count") or 0) + 1
            it["last_used"] = _now()
            n += 1
    if n:
        _save(cfg, items)
    return n


# ---------------------------------------------------------------- 冷热分层

def layer_of(it: dict, now: float | None = None) -> str:
    """L1 工作记忆（高质量且近期使用）/ L2 短期（近期沉淀）/ L3 长期（高质量少用）/ L4 冷归档。"""
    now = now or _now()
    q = it.get("quality") or "mid"
    stale = (now - _ts(it, now)) > STALE_DAYS * 86400
    hot = int(it.get("usage_count") or 0) >= 2
    if q == "low":
        return "L4"
    if q == "high" and hot and not stale:
        return "L1"
    if not stale:
        return "L2" if hot else "L3"
    return "L3" if q == "high" else "L4"


def layers(cfg: dict) -> dict[str, list[dict]]:
    now = _now()
    out: dict[str, list[dict]] = {"L1": [], "L2": [], "L3": [], "L4": []}
    for it in _load(cfg):
        out.setdefault(layer_of(it, now), []).append(it)
    return out


def consolidate(cfg: dict, dry_run: bool = False) -> dict:
    """冷热维护：冷素材热度衰减；长期零使用的低质素材建议入回收站。"""
    items = _load(cfg)
    now = _now()
    changed, to_trash = 0, []
    for it in items:
        last = _ts(it, now)
        idle_days = (now - last) / 86400
        if idle_days > STALE_DAYS:
            heat = float(it.get("heat", 1.0)) * (HEAT_DECAY ** int(idle_days // STALE_DAYS))
            if abs(heat - float(it.get("heat", 1.0))) > 1e-6:
                it["heat"] = round(max(heat, 0.05), 3)
                changed += 1
            if int(it.get("usage_count") or 0) == 0 and idle_days > STALE_DAYS * 2 \
                    and (it.get("quality") or "mid") == "low":
                to_trash.append(it.get("id"))
    report = {"decayed": changed, "suggest_trash": to_trash}
    if not dry_run and changed:
        _save(cfg, items)
    if not dry_run and to_trash:
        moved = 0
        for iid in to_trash:
            it = next((x for x in items if x.get("id") == iid), None)
            if it:
                material._to_trash(cfg, it)
                material.remove_material(cfg, iid)
                moved += 1
        report["moved_to_trash"] = moved
    return report


def rules_path(cfg: dict) -> Path:
    return Path(cfg["studio"]["workspace_abs"]) / "memory_rules.json"


def rules_brief(cfg: dict, max_chars: int = 300) -> str:
    """反思守则（Generative Agents 反思树的高层节点）：写作前注入的是守则，
    不是原始标签罗列——描述性摘要没有可执行动作，正是旧记忆系统沦为摆设的原因。"""
    try:
        r = json.loads(rules_path(cfg).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    rules = [x for x in (r.get("rules") or []) if x][:5]
    if not rules:
        return ""
    return "\n".join(f"- {x}" for x in rules)[:max_chars]


def reflect(cfg: dict, llm, tropes: list | None = None) -> dict:
    """成稿后反思：LLM 从近期自产记录归纳重复模式 → 高层守则，存 memory_rules.json。

    不训练模型（DivPO 思想的推理端替代）——用反思把"已写滥什么"
    显式化成下一篇的差异化指令。
    """
    from .prompts import render_prompt

    items = _load(cfg)
    mine = [x for x in items if x.get("source_project")]
    mine.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    def grp(mtype: str, n: int) -> str:
        out = [f"- {str(x.get('text', ''))[:60]}" for x in mine if x.get("type") == mtype][:n]
        return "\n".join(out) or "（无）"

    tropes_txt = "\n".join(f"- [{t.get('direction')}] {t.get('core_trope')}"
                           for t in (tropes or [])[:8]) or "（无）"
    sys_p, user_p = render_prompt(
        "memory_reflect",
        TITLES=grp("标题", 8), HOOKS=grp("开头钩子", 6), TWISTS=grp("反转设计", 5),
        END_HOOKS=grp("章末钩子", 5), TROPES=tropes_txt,
    )
    data = llm.chat_json(sys_p, user_p)
    rules = [str(r) for r in (data.get("rules") or []) if str(r).strip()][:5]
    if rules:
        rules_path(cfg).write_text(
            json.dumps({"rules": rules, "summary": data.get("summary", ""),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return {"n_rules": len(rules), "summary": data.get("summary", "")}


def memory_brief(cfg: dict, direction: str = "", max_chars: int = 420) -> str:
    """跨篇反重复记忆摘要：近期自产写法 = 近似禁用清单 + 高频复用 = 写滥预警。

    旧版"热忆优先继承其写法"与防千篇一律的目标正好相反——记忆的职责是记住
    已经写过什么，让下一篇换写法，而不是把热度最高的句式再抄一遍。
    自产物以 source_project 字段区分（deposit_from_project 写入）。
    """
    items = _load(cfg)
    mine = [x for x in items if x.get("source_project")]
    mine.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    def recent(mtype: str, n: int, cap: int = 48) -> list[str]:
        return [str(x.get("text", ""))[:cap] for x in mine if x.get("type") == mtype][:n]

    secs: list[str] = []
    t = recent("标题", 6, 40)
    if t:
        secs.append("近期已用标题（句式结构/悬念点/数字用法都不能近似）：" + "；".join(t))
    h = recent("开头钩子", 4)
    if h:
        secs.append("近期已用开场方式（禁同款开场，须换叙事入口）：" + "；".join(h))
    e = recent("章末钩子", 3)
    if e:
        secs.append("近期已用节尾钩子：" + "；".join(e))
    r = recent("反转设计", 3)
    if r:
        secs.append("近期已用反转设计（同型反转须换落点）：" + "；".join(r))
    hot = sorted((x for x in items if int(x.get("usage_count") or 0) >= 2),
                 key=lambda x: -int(x.get("usage_count") or 0))[:6]
    if hot:
        secs.append("写滥预警（复用度过高，本次降权或换写法）：" + "；".join(
            f"{str(x.get('text', ''))[:36]}（已用{int(x.get('usage_count') or 0)}次）" for x in hot))
    if not secs:
        return ""
    return "\n".join("- " + s for s in secs)[:max_chars]
