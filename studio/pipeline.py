# -*- coding: utf-8 -*-
"""创作流水线：热点 → 核心梗 → 书名方案 → 章节大纲 → 故事圣经 → 逐章扩写 → 润色体检。

每个 stage 独立成函数、产物落盘，run 时已完成 stage 自动跳过（断点续跑）。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

NL = chr(10)
from . import agents, drafts as _drafts, foreshadow as _foreshadow, hotspots, knowledge
from . import title_gate as _TG
from . import review as review_mod
from .config import data_path, load_config
from .langqa import banned_regex, banned_terms
from .llm import BaseLLM, MockLLM, _build_llm, resolve_llm, usage_summary
from .prompts import load_prompt, render_prompt
from .store import Project
from .util import count_words_zh, now_ts
from .util import (MAX_SECTIONS, SEC_HEAD, SEC_HEAD_TITLE, atomic_write_text,
                   sec_head_no, sec_head_title)
from . import runlog

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 风格范文缺省提示（无同题材范文时注入给写手）。统一常量，避免多处硬编码漂移。
STYLE_SAMPLE_EMPTY = "（暂无范文，按语言规则写作）"


# ---------------------------------------------------------------------------
# 静态资产加载
# ---------------------------------------------------------------------------

def load_tropes() -> dict:
    return json.loads(data_path("tropes.json").read_text(encoding="utf-8"))


_GENRE_CHAINS: dict | None = None


def split_combo(direction: str) -> list[str]:
    """组合方向拆分：'规则怪谈×女主后宫' → ['规则怪谈', '女主后宫']。"""
    if not direction:
        return []
    return [x.strip() for x in re.split(r"[×·+*]", direction) if x.strip()]


def _family_of(direction: str) -> tuple[str, dict] | None:
    """单个方向名 → (族键, 族dict)。"""
    fams = (_GENRE_CHAINS or {}).get("families") or {}
    for fk, fv in fams.items():
        if direction in (fv.get("directions") or []):
            return fk, fv
    return None


def _merge_chains(fam_a: dict, fam_b: dict) -> dict:
    """未收录组合的动态合成：主族链 + 交叉融合公式 + 副族工具与红线。

    组合不是两族拼盘——先回答"两族互相解决对方什么问题"，答不出就是
    没有化学反应的假组合。
    """
    return {
        "name": (fam_a['name'] if fam_a is fam_b else f"{fam_a['name']}×{fam_b['name']}") + "（融合）",
        "chain": [
            "① 主从定位：前一族出骨架（大纲结构/节奏/冲突主链），后一族出内核（设定/氛围/第二层爽点）",
            "② 交叉点前置：先写两句话回答——A 族的什么难题恰好被 B 族元素解开，B 族的什么代价恰好由 A 族结构兑现；写不出说明组合无化学反应，必须换组合或换主从",
            "③ 每节双层卡点：主族节拍（钩子/反转/爽点）+ 副族元素（设定细节/情感点/氛围）各至少 1 个，共用同一批事件",
            "④ 工具混用：副族工具改造主族节拍（例：用怪谈的『日常异常化』写言情的日常戏；用悬疑的『倒序埋线』设计打脸的证据链）",
            "⑤ 双线共用事件：任何推进必须同时推进两条线至少一条、不得出现只服务单线的整节",
        ],
        "tools": fam_a.get("tools", []) + fam_b.get("tools", []),
        "errors": fam_a.get("errors", []) + fam_b.get("errors", [])
        + ["两族各自为政两张皮（写完一段A再写一段B）", "组合只换皮不换结构（副族沦为布景）"],
    }


def genre_chain(direction: str) -> dict | None:
    """方向 → 题材族思维链。支持组合方向（'A×B'）：先查 combos 融合链，
    未收录则按族动态合成。返回结构兼容单族（chain/tools/errors/…）。"""
    global _GENRE_CHAINS
    if _GENRE_CHAINS is None:
        try:
            _GENRE_CHAINS = json.loads(data_path("genre_chains.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _GENRE_CHAINS = {"families": {}}
    if not direction:
        return None
    # 组合方向
    parts = split_combo(direction)
    if len(parts) > 1:
        fams = [_family_of(p) for p in parts[:2]]
        fams = [f for f in fams if f]
        if not fams:
            return None
        fam_keys = {fk for fk, _ in fams}
        for ck, c in (_GENRE_CHAINS.get("combos") or {}).items():
            if ck.startswith("_") or not isinstance(c, dict):
                continue
            if set(c.get("parts") or []) == fam_keys:
                c = dict(c)
                c["chain"] = c.get("fusion") or c.get("chain") or []
                c.setdefault("outline_rule",
                             "大纲每节标注：主线族节拍+副线族元素各至少1个，且共用同一批事件；禁止出现只服务单线的整节。")
                c.setdefault("draft_rule",
                             f"本篇是{c.get('name', '组合题材')}：两条题材线必须共用事件互相咬合，"
                             "写任何场景前先问'这个场景同时推进了几条线'。")
                return c
        a, b = fams[0][1], (fams[1][1] if len(fams) > 1 else fams[0][1])
        return _merge_chains(a, b)
    hit = _family_of(direction)
    return hit[1] if hit else None


def _ban_props_text(cfg: dict) -> str:
    """config.kb.negative_terms → 写手/重写 prompt 的道具黑名单文本。"""
    terms = banned_terms(cfg)
    return "、".join(terms) if terms else "（无）"


def _guide_query_terms(direction: str) -> list[str]:
    """扩展题材查询词（去重保序）：原始方向、×·+拆词、tropes id 对应的 name。

    使 run 传 tropes id（如 guaidanxiuxian）或 name（如 怪诞修仙脑洞）时，
    都能对齐到 genre_guides 的题材键。
    """
    id2name: dict[str, str] = {}
    try:
        for d in load_tropes()["directions"]:
            if d.get("id"):
                id2name[d["id"]] = d.get("name", "")
    except Exception:  # noqa: BLE001
        pass
    terms: list[str] = []
    for t in [direction] + split_combo(direction):
        if not t:
            continue
        for x in (t, id2name.get(t, "")):
            if x and x not in terms:
                terms.append(x)
    return terms


def load_genre_guide(cfg: dict, direction: str) -> str:
    """按题材方向选择指导文件（config.knowledge.genre_guides 映射），返回其 SYSTEM 段内容。

    匹配顺序（对每个查询词）：
    1. 精确匹配 genre_guides 键（查询词含原始方向、×·+拆词、tropes id→name）；
    2. 精确未命中任何键时，做包含匹配（键与查询词互相包含，键长≥2）；
    **复合方向（如 修仙×短剧改编）会收集所有命中的题材文件并合并注入**（去重、保持顺序），
    使题材层（修仙/古言）与风格层（短剧改编）可叠加；全部未命中回退 genre_guide.md；
    映射值为空字符串表示该题材不注入（命中但全为空时返回空，不回退）。
    文件用 load_prompt 读（进程缓存，run 均为新进程）。
    """
    guides = ((cfg.get("knowledge") or {}).get("genre_guides") or {})
    terms = _guide_query_terms(direction)
    hit = False
    fnames: list[str] = []
    for t in terms:
        matched = None
        if t in guides:  # 1) 精确匹配
            hit = True
            matched = guides[t]
        else:  # 2) 对该查询词做包含匹配（键与词互相包含，键长≥2）
            for key, f in guides.items():
                if key and f and len(key) >= 2 and (key in t or t in key):
                    hit = True
                    matched = f
                    break
        if matched and matched not in fnames:
            fnames.append(matched)
    # 古代题材不得注入现代赛道模板：`genre_hougong.md` 写的是买股文（贵族学院/娱乐圈/
    # 选秀综艺），而「女主后宫」「买股文」这些方向名同样会命中它。用户点名要「女主后宫
    # 向古言」时，注入现代模板等于让写手把宫廷写成校园。古装方向改用 genre_guyan.md，
    # 并在末尾补一条古言后宫的硬约束（多倾心者可以写，结局必须收敛到 1v1 或开放）。
    _ancient_hougong = False
    try:
        from . import langqa as _lq

        if _lq.genre_tags(direction) == ("guyan",) and "genre_hougong.md" in fnames:
            fnames = ["genre_guyan.md" if f == "genre_hougong.md" else f for f in fnames]
            _ancient_hougong = True
    except Exception:  # noqa: BLE001
        _ancient_hougong = False
    parts = []
    for fname in fnames:
        if fname.endswith(".md"):  # 映射值可带 .md，load_prompt 会自动拼后缀
            fname = fname[:-3]
        try:
            sys_p, _ = load_prompt(fname)
            if sys_p:
                parts.append(sys_p)
        except Exception:  # noqa: BLE001
            pass
    if _ancient_hougong:
        _ancient_note = NL.join([
            "## 古言后宫补充（与上面的宫廷指导叠加）",
            "- 多倾心者可以写，但结局必须收敛到 1v1 或开放式；表站禁止多夫结局。",
            "- 情感线必须落在宫规上：位分、份例、记档、请安站队、赏赐与申饬，"
            "不能让男主一句「朕护着你」解决。",
            "- 现代买股文的术语（股、打投、CP、修罗场排面）一律不许出现；"
            "改用「宠、失宠、抬举、冷落、体面」这类宫廷词。",
        ])
        parts.append(_ancient_note)
    return "\n\n---\n\n".join(parts)


def load_platform(key: str) -> dict:
    plats = json.loads(data_path("platforms.json").read_text(encoding="utf-8"))["platforms"]
    if key not in plats:
        raise SystemExit(f"未知平台 {key}，可选：{list(plats)}")
    spec = dict(plats[key])
    spec["key"] = key
    return spec


def load_style_guide() -> str:
    return data_path("style_guide.md").read_text(encoding="utf-8")


def lessons_digest(cfg: dict, max_chars: int = 900, platform: str = "") -> str:
    """读取发布数据沉淀的教训库，压缩成可注入 prompt 的摘要。

    platform 非空时优先只取同平台（或历史无平台标签）的教训：知乎与番茄的字数下限、
    节头规范、钩子密度都不同，跨平台套用教训会把上一个平台的规则强加到本平台
    （例：番茄"每章2050-2300字"套到知乎 800-1200 字小节的稿子上）。
    若该平台一条都没有（新平台/标签缺失），降级为全量并给每行标注来源平台，
    否则「换平台就彻底没有历史经验」比跨平台污染更糟。
    """
    from pathlib import Path

    f = Path(cfg["studio"]["workspace_abs"]) / "feedback_lessons.json"
    if not f.exists():
        return ""
    try:
        entries = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""

    def _collect(matched_only: bool) -> list[str]:
        lines: list[str] = []
        used = 0
        for e in reversed(entries[-5:]):  # 最近 5 条记录
            ep = e.get("platform") or ""
            if matched_only and platform and ep and ep != platform:
                continue
            tag = f"[{ep}]" if ep and (not matched_only or not platform) else ""
            for l in e.get("lessons", []):
                line = (f"- [{e.get('project', '')[:16]}]{tag}"
                        f"[{l.get('dimension', '')}] {l.get('action', '')}")
                if used + len(line) > max_chars:
                    return lines
                lines.append(line)
                used += len(line)
        return lines

    out = _collect(True) if platform else _collect(False)
    if not out and platform:
        out = _collect(False)   # 降级：该平台无历史教训，退回全量并标注平台
    return "\n".join(out)


def trope_table() -> str:
    rows = []
    for d in load_tropes()["directions"]:
        rows.append(f"{d['name']}（{d['id']}）| 情绪:{d['emotion']} | 冲突:{d['core_conflict']}")
    return "\n".join(rows)


def match_direction(text: str) -> str:
    """题材方向匹配：题材适配 → 关键词 → 判别器语义路由 → 题材最热兜底。

    顺序为什么这样排（2026-10-26 古言实测教训）：旧实现是「判别器优先 → 关键词计数，
    零命中回落硬编码『全村人的眼光』」。宫斗选题（嫡姐/进宫/皇上/盖头）与 42 个方向
    的关键词零交集，于是宫斗被写成「宫廷 + 全村」的四不像。
    """
    tags = ()
    try:
        from . import langqa as _lq

        tags = _lq.idea_genre(text)
    except Exception:  # noqa: BLE001
        tags = ()
    dirs = load_tropes()["directions"]

    def _dir_ancient(d: dict) -> bool:
        """方向本身是不是古代题材。

        不能只看方向名：方向名多是「彩礼纠纷」「婆媳大战」这类现代词，古装方向的
        名字里也不带「古」字（「宫廷权谋」带，但「换嫁冲喜」不带）。所以名字 +
        关键词 + 核心冲突 + 关键场景一起判，命中任一古代标记即算古代方向。
        """
        try:
            from . import langqa as _lq

            if _lq.genre_tags(d.get("name", "")):
                return True
            blob = " ".join([
                str(d.get("name", "")), str(d.get("core_conflict", "")),
                " ".join(d.get("keywords") or []), " ".join(d.get("key_scenes") or []),
                " ".join(d.get("reversals") or []), " ".join(d.get("title_examples") or []),
            ])
            return any(m in blob for m in _lq._IDEA_ANCIENT_MARKS)
        except Exception:  # noqa: BLE001
            return False

    def _compatible(d: dict) -> bool:
        if not tags:
            return True
        want_ancient = tags == ("guyan",)
        return _dir_ancient(d) == want_ancient

    pool = [d for d in dirs if _compatible(d)] or dirs
    if tags and len(pool) < len(dirs):
        print(f"[trope] 题材={tags} → 候选方向 {len(pool)}/{len(dirs)} 个")

    # 1) 关键词计数（在题材适配的子集里算，避免古装选题命中现代方向的词）
    best, best_hits = "", 0
    for d in pool:
        hits = sum(1 for kw in d.get("keywords", []) if kw.strip() and kw in text)
        if hits > best_hits:
            best, best_hits = d["name"], hits
    if best:
        return best

    # 2) 判别器语义路由（只在题材适配的候选里选）
    try:
        from .config import load_config as _lc

        _cfg = _lc(None)
        _lcfg = (_cfg.get("laya") or {})
        if _lcfg.get("enabled", True) and _lcfg.get("route_enabled", True):
            from . import laya_judge

            d = laya_judge.route_direction(
                text, [x["name"] for x in pool][:20],
                base_url=_lcfg.get("route_url", "http://127.0.0.1:8150"))
            if d and any(x["name"] == d for x in pool):
                return d
    except Exception:  # noqa: BLE001
        pass

    # 3) 兜底：题材适配子集里热度最高的方向（确定性、可解释，不再是硬编码常量）
    fallback = max(pool, key=lambda x: int(x.get("heat") or 0))["name"]
    print(f"[trope] ⚠ 选题未命中任何方向关键词（题材={tags or '未定'}），"
          f"按该题材最热方向 [{fallback}] 兜底")
    return fallback


def platform() -> str:
    return load_config()["studio"].get("platform", "zhihu")


def calc_chapter_words(plat: dict, total: int, chapters: int) -> int:
    lo, hi = plat.get("chapter_words", [1500, 2500])
    w = max(lo, min(hi, total // max(chapters, 1)))
    return w


# ---------------------------------------------------------------------------
# Stage 1：抓热点
# ---------------------------------------------------------------------------

@runlog.staged("抓取热点")
def stage_fetch(cfg: dict, project: Project, sources: list[str] | None = None,
                top: int | None = None, direction_hint: str = "") -> list:
    if project.has("hotspots.json"):
        print("[stage] hotspots 已存在，跳过（删除该文件可重抓）")
        return [hotspots.HotItem.from_dict(d) for d in project.load_json("hotspots.json")]
    print(f"[stage] 抓取热点中 …")
    dirs = [direction_hint] if direction_hint else []
    if not sources and cfg["studio"].get("novel_sources", True):
        sources = list(cfg["studio"].get("sources") or []) + ["novel"]
    items = hotspots.fetch_all(cfg, sources=sources, top=top, directions=dirs)
    project.save_json("hotspots.json", [i.to_dict() for i in items])
    project.set_stage("hotspots", f"{len(items)}条")
    print(f"[stage] 热点聚合去重后 {len(items)} 条 → hotspots.json")
    return items


# ---------------------------------------------------------------------------
# Stage 2：提核心梗（热点 → 梗卡候选）
# ---------------------------------------------------------------------------

def recent_tropes(cfg: dict, n: int = 12) -> list[dict]:
    """读取最近 n 个已写项目（含 rejected 淘汰区）的题材/核心梗，用于查重防重复。"""
    ws = Path(cfg["studio"]["workspace_abs"])
    out = []
    for base in (ws / "projects", ws / "rejected"):
        if not base.exists():
            continue
        dirs = sorted(base.iterdir(), key=lambda d: d.name, reverse=True)
        for d in dirs:
            f = d / "concept.json"
            if not f.exists():
                continue
            try:
                c = json.loads(f.read_text(encoding="utf-8"))
                out.append({"direction": c.get("direction", ""),
                            "core_trope": (c.get("core_trope") or "")[:60],
                            "title": (c.get("hot_title") or "")[:30]})
            except Exception:  # noqa: BLE001
                continue
    return out[:n]


# 写法变换轴（DivLM 正交维度 + 传统叙事轴 + 网文"框架内变体"手法）：
# 同一题材反复写时，靠这五根轴强制换血，而不是让模型自由发挥（必然回声）
AXES_SPEC = {
    "tone": "幽默荒诞|阴郁压抑|冷峻克制|温情治愈|疯批癫狂|悲壮苍凉",
    "style": "描写沉浸式|动作推进式|对话推进式",
    "pov": "第一人称限知|第一人称不可靠叙述|第三人称限知|双视角轮换|罗生门多视角",
    "time": "正叙|开篇倒叙|双线交叉|环形结构",
    "gap": "读者知而主角不知|主角知而读者不知|双层信息差结尾对调|中途揭底反转",
}


def recent_axes(cfg: dict, n: int = 10) -> list[dict]:
    """读取最近 n 个项目 pitch.json 里的写法轴取值，做轴级反重复。"""
    ws = Path(cfg["studio"]["workspace_abs"])
    out = []
    for base in (ws / "projects", ws / "rejected"):
        if not base.exists():
            continue
        for d in sorted(base.iterdir(), key=lambda d: d.name, reverse=True):
            f = d / "pitch.json"
            if not f.exists():
                continue
            try:
                ax = json.loads(f.read_text(encoding="utf-8")).get("axes") or {}
            except Exception:  # noqa: BLE001
                continue
            if isinstance(ax, dict) and ax:
                out.append(ax)
    return out[:n]


def axes_ban_text(cfg: dict) -> str:
    """近期已用写法轴组合 → 禁用清单文本。"""
    used = recent_axes(cfg)
    if not used:
        return ""
    lines = []
    for ax in used[:6]:
        parts = [f"{k}={ax[k]}" for k in AXES_SPEC if ax.get(k)]
        if parts:
            lines.append("- " + "，".join(parts))
    return "\n".join(lines)


def axes_spec_text(pitch: dict) -> str:
    """本篇已定的写法轴 → 指定文本（注入 outline/draft，防方案层定了写作层丢）。"""
    ax = pitch.get("axes") or {}
    if not isinstance(ax, dict) or not ax:
        return ""
    return "；".join(f"{k}：{ax[k]}" for k in AXES_SPEC if ax.get(k))


def _trope_metrics(a: str, b: str) -> tuple[float, int]:
    """返回 (清洗后 bigram Jaccard, b 文本中命中的 a 的 3 字片段数)。"""
    def clean(s: str) -> str:
        return re.sub(r"[^一-龥a-z0-9]", "", s.lower())
    ca, cb = clean(a), clean(b)
    if not ca or not cb:
        return 0.0, 0
    ga = {ca[i:i + 2] for i in range(len(ca) - 1)}
    gb = {cb[i:i + 2] for i in range(len(cb) - 1)}
    jac = len(ga & gb) / len(ga | gb) if ga and gb else 0.0
    ag3 = {ca[i:i + 3] for i in range(len(ca) - 2)}
    common3 = len({g for g in ag3 if g in cb})
    return jac, common3


def _trope_similar(a: str, b: str, threshold: float = 0.22) -> bool:
    """规则查重：字符 bigram Jaccard ≥阈值，或共享 ≥3 个不同 3 字片段（换皮检测）。"""
    jac, common3 = _trope_metrics(a, b)
    return jac > threshold or common3 >= 3


_DUP_GRAY_JAC = 0.12   # 低于它视为"措辞全新"——规则层足够判新，不必花语义查重


def _dup_needs_llm(ranked: list, hist: list) -> bool:
    """是否存在灰区候选（与历史有一定字面重叠但未达规则判死阈值）。

    有灰区才值得花一次主模型语义查重；候选全部与历史措辞层明显不同时跳过，
    省一次 5-10s 的 LLM 调用。语义查重保护的是"字面全新但骨架雷同"的换皮梗，
    这类梗多少会留下词语影子，完全零重叠（<0.12 且 3-gram≤1）时误漏概率极低。
    """
    for c in ranked:
        t = c.get("core_trope", "")
        for h in hist:
            jac, c3 = _trope_metrics(t, h.get("core_trope", ""))
            if jac > 0.22 or c3 >= 3:
                break  # 规则已判死，无需语义复核
            if jac >= _DUP_GRAY_JAC or c3 >= 2:
                return True
    return False


def _log_dup_decisions(candidates: list, hist: list, flags: list, project_name: str = ""):
    """数据飞轮：把主模型仲裁的查重决策落盘（v4 自训判别器的真实标注来源）。

    每条 {ts, project, candidate, verdict, matched_hist, n_hist}；matched_hist 取
    规则相似度最高的历史梗（dup 时它就是训练正对，new 时全 hist 都是负参照）。
    失败静默——飞轮永远不影响主流程。
    """
    try:
        from .config import load_config
        from .util import now_ts

        cfg = load_config(None)
        p = Path(cfg["studio"]["workspace_abs"]) / "kb" / "dup_decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for k, f in enumerate(flags):
            cand = candidates[k].get("core_trope", "")
            if not cand:
                continue
            best, best_score = "", (-1, -1)
            for h in hist:
                score = _trope_metrics(cand, h.get("core_trope", ""))
                if score > best_score:
                    best, best_score = h.get("core_trope", ""), score
            rows.append({"ts": now_ts(), "project": project_name,
                         "candidate": cand, "verdict": "dup" if f else "new",
                         "matched_hist": best, "n_hist": len(hist)})
        with open(p, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        if rows:
            print(f"[flywheel] 查重决策落盘 {len(rows)} 条（v4 训练数据）")
    except Exception:  # noqa: BLE001
        pass


def _llm_dup_check(llm, candidates: list, hist: list, project_name: str = "") -> list:
    """轻量模型语义查重：换皮情节（换名/换场景）也能识别。返回 bool 列表，失败返回 []。

    每次仲裁结果自动落盘（数据飞轮），供自训判别器 v4+ 攒真实标注。
    """
    if not llm or not hist or not candidates:
        return []
    try:
        hlines = "\n".join(f"{i}. {h.get('core_trope', '')}" for i, h in enumerate(hist))
        clines = "\n".join(f"{i}. {c.get('core_trope', '')}" for i, c in enumerate(candidates))
        user = (
            "已写过的故事梗：\n" + hlines + "\n\n新候选梗：\n" + clines + "\n\n"
            "判断每个新候选是否与某条历史梗本质相同（题材相同且情节骨架相同判重复；"
            "仅换人名、换场景但骨架一致也算重复；题材与情节都不同才算新）。\n"
            '只输出：{"dup": [判为重复的新候选编号]}'
        )
        out = llm.chat_json("你是选题查重裁判。只输出 JSON，不写解释。", user)
        idx = set()
        for x in (out.get("dup") or []):
            try:
                idx.add(int(x))
            except (TypeError, ValueError):
                continue
        flags = [i in idx for i in range(len(candidates))]
        if flags:
            _log_dup_decisions(candidates, hist, flags, project_name)
        return flags
    except Exception as e:  # noqa: BLE001
        print(f"[stage] LLM 查重失败，回退规则判定：{type(e).__name__}")
        return []


@runlog.staged("提核心梗")
def stage_mine(cfg: dict, llm: BaseLLM, project: Project, n: int | None = None,
               direction_hint: str = "") -> list[dict]:
    if project.has("concepts.json"):
        print("[stage] concepts 已存在，跳过")
        return project.load_json("concepts.json")
    items = [hotspots.HotItem.from_dict(d) for d in project.load_json("hotspots.json") or []]
    if not items:
        raise SystemExit("没有热点数据，请先执行 stage_fetch（或用 --idea 直接给梗）")
    llm = resolve_llm(cfg, llm, "planner")
    n = n or cfg["studio"].get("concept_candidates", 6)
    hot_lines = "\n".join(
        f"{i.source}|{i.rank}|{i.title}|{int(i.hot_value)}" for i in items[:60]
    )
    # 教训库按目标平台过滤：stage_mine 发生在 pitch 之前，平台只能来自
    # --platform / config.studio.platform（与 stage_pitch 选平台的口径一致）。
    feedback = lessons_digest(cfg, platform=cfg.get("studio", {}).get("platform", ""))
    from . import material as material_mod

    mat = material_mod.digest(cfg, n=6, max_chars=420)
    if mat:
        feedback = (feedback + "\n【素材库高分范例（标题/钩子/反转，可借鉴写法勿照抄）】\n" + mat).strip()
        print(f"[stage] 注入素材库范例 {len(mat)} 字")
    try:
        from . import memory

        brief = memory.memory_brief(cfg, direction=direction_hint, max_chars=420)
        if brief:
            feedback = (feedback + "\n【反重复记忆（以下写法近期已用过，近似即不合格，必须换写法）】\n" + brief).strip()
            print(f"[stage] 注入反重复记忆摘要 {len(brief)} 字")
        rules = memory.rules_brief(cfg)
        if rules:
            feedback = (feedback + "\n【写作守则（反思生成，逐条执行）】\n" + rules).strip()
            print(f"[stage] 注入反思守则 {len(rules)} 字")
    except Exception:
        pass
    # 知识库题材热度：让提梗 AI 自动避开已写滥的方向（AI 自用，无需用户触发）
    try:
        from . import kb as _kb

        _tops = (_kb.stats(cfg).get("top_directions") or {})
        _tops = {d: c for d, c in _tops.items() if d and d != "未分类"}
        if _tops:
            _heat = "；".join(f"{d} 已沉淀{c}条" for d, c in list(_tops.items())[:6])
            feedback = (feedback + "\n【知识库题材热度（越靠前越写滥，"
                        "提梗必须差异化骨架或直接避开这些方向）】\n" + _heat).strip()
            print(f"[stage] 注入知识库题材热度 {len(_tops)} 项")
    except Exception:
        pass
    if feedback:
        print(f"[stage] 注入历史数据反馈：{len(feedback)} 字")
    # 防重复：把最近已写的题材/情节作为禁止清单注入（查 rejected 淘汰区）
    hist = recent_tropes(cfg)
    avoid = ""
    if hist:
        lines = [f"- [{h['direction']}] {h['core_trope']}" for h in hist]
        avoid = ("以下题材与核心情节近期已经写过，本次候选必须全部避开——"
                 "既不能用同一题材组合，也不能套用同一情节骨架（只换人物名字无效）："
                 + "\n".join(lines))
        print(f"[stage] 查重：注入 {len(hist)} 条已写题材禁止清单")
    if direction_hint:
        combo_parts = split_combo(direction_hint)
        if len(combo_parts) > 1:
            hint_text = (
                f"必须产出「{direction_hint}」组合题材的选题：两族题材必须真正融合（共用同一批事件互相咬合），"
                f"不是先写一段{combo_parts[0]}再写一段{combo_parts[1]}的两张皮；"
                "所有 concept 的 direction 字段必须原样填写「" + direction_hint + "」。"
            )
        else:
            hint_text = (
                f"必须优先产出「{direction_hint}」方向的选题：热点仅作为由头适配该方向；"
                "所有 concept 的 direction 必须为该方向（确实无法适配时最多 1 张其他方向）。"
            )
        # 题材硬性元素清单注入:梗卡情节必须包含方向标志元素,仅打标签不写元素=不合格
        for want in combo_parts or [direction_hint]:
            for d_ in load_tropes()["directions"]:
                if d_["name"] == want and d_.get("required_elements"):
                    hint_text += (f"\n「{want}」梗卡必须包含以下标志性元素（写进 core_trope 与 hook，"
                                  "缺一即不合格）：\n" + "\n".join(f"- {e}" for e in d_["required_elements"]))
                    break
    else:
        hint_text = "（未指定，自由选题；各 concept 的 direction 与情节骨架必须互不相同）"
    if avoid:
        hint_text = hint_text + "\n" + avoid
    meta = project.meta
    meta["direction_hint"] = direction_hint
    project.save_meta(meta)
    system, user = render_prompt(
        "mine", TROPE_TABLE=trope_table(), HOT_LIST=hot_lines, N=n,
        FEEDBACK=feedback or "（暂无，正常选题）", DIRECTION_HINT=hint_text,
        BAN_PROPS=_ban_props_text(cfg)
    )
    print(f"[stage] 分析热点、提炼核心梗（生成 {n} 张梗卡）…")
    data = llm.chat_json(system, user)
    concepts = data.get("concepts", [])
    if not concepts:
        raise SystemExit(f"提梗失败，模型输出：{json.dumps(data, ensure_ascii=False)[:300]}")
    concepts.sort(key=lambda c: -int(c.get("potential_score", 0)))
    project.save_json("concepts.json", concepts)
    project.set_stage("concepts", f"{len(concepts)}张梗卡")
    for i, c in enumerate(concepts):
        print(f"  [{i}] {c.get('potential_score')}分 | {c.get('direction')} | {c.get('core_trope', '')[:36]}")
    return concepts


def make_idea_concept(idea: str, direction: str | None = None) -> dict:
    """用户直接给核心梗（跳过热点），构造等价的梗卡。"""
    return {
        "hot_title": "（用户自拟核心梗）",
        "source": "idea",
        "direction": direction or match_direction(idea),
        "core_trope": idea.strip(),
        "emotion": "怒→爽",
        "conflict": "",
        "hook": "",
        "potential_score": 90,
        "reason": "用户指定选题",
    }


def _recent_tropes_safe(project: Project) -> list:
    """从项目路径推断 workspace，读最近已写梗（排除当前项目）。"""
    ws = project.root.parent.parent
    out = []
    for base in (ws / "projects", ws / "rejected"):
        if not base.exists():
            continue
        for d in sorted(base.iterdir(), key=lambda x: x.name, reverse=True):
            if d.name == project.root.name:
                continue
            f = d / "concept.json"
            if not f.exists():
                continue
            try:
                c = json.loads(f.read_text(encoding="utf-8"))
                out.append({"direction": c.get("direction", ""),
                            "core_trope": (c.get("core_trope") or "")[:60]})
            except Exception:  # noqa: BLE001
                continue
    return out[:12]


def _sem_dup_flags(cfg: dict | None, llm, ranked: list, hist: list,
                   project_name: str = "") -> list:
    """语义查重三层：Laya 常驻快筛（每次选题必跑）→ LLM 只仲裁不确定的候选。

    返回与 ranked 等长的 bool 列表（True=判重）。Laya 不可用/关闭时退回纯 LLM
    路径，此时保留灰区门控（无灰区不花大模型）。
    """
    from .llm import MockLLM

    laya_cfg = (cfg or {}).get("laya") or {}
    flags = None
    if laya_cfg.get("enabled", True) and not isinstance(llm, MockLLM):
        try:
            from . import laya_judge

            flags = laya_judge.dup_flags(
                [c.get("core_trope", "") for c in ranked],
                [h.get("core_trope", "") for h in hist],
                checkpoint=laya_cfg.get("checkpoint", "multilingual"),
                device=laya_cfg.get("device", "cpu"),
                threshold_dup=float(laya_cfg.get("threshold_dup",
                                                 laya_cfg.get("threshold", 0.60))),
                threshold_new=float(laya_cfg.get("threshold_new", 0.80)),
                backend=laya_cfg.get("backend", "laya"),
                self_url=laya_cfg.get("self_url", "http://127.0.0.1:8152"),
                api_url=laya_cfg.get("api_url", "") or "",
                api_key=laya_cfg.get("api_key", "") or "",
                api_model=laya_cfg.get("api_model", "") or "",
            )
            for k, f in enumerate(flags):
                if f is True:
                    print(f"[laya] 判重命中 → 跳过：{ranked[k].get('core_trope', '')[:34]}")
        except Exception as e:  # noqa: BLE001
            print(f"[stage] laya 快筛不可用：{type(e).__name__}")
            flags = None
    if flags is None:
        # 纯 LLM 兜底：有灰区才值得花主模型
        if not _dup_needs_llm(ranked, hist):
            print("[stage] laya 不可用且无灰区候选，跳过语义查重")
            return [False] * len(ranked)
        sub = _llm_dup_check(llm, ranked, hist, project_name)
        return [bool(f) for f in sub] if sub else [False] * len(ranked)
    unsure = [k for k, f in enumerate(flags) if f is None]
    if unsure:
        llm_sub = _llm_dup_check(llm, [ranked[k] for k in unsure], hist, project_name)
        out = list(flags)
        for i, k in enumerate(unsure):
            out[k] = bool(llm_sub[i]) if i < len(llm_sub) else False
        print(f"[laya] 判重 {len(flags) - len(unsure)}/{len(flags)} 确定，"
              f"{len(unsure)} 个交主模型仲裁")
        return out
    print(f"[laya] 判重全部确定（{len(flags)}/{len(flags)}），主模型查重零调用")
    return list(flags)


def stage_select(project: Project, pick=None, llm=None, cfg: dict | None = None) -> dict:
    """从梗卡候选中选定一张：pick=序号 / None 或 'auto'=自动选最高分 / 'ask'=交互选择。"""
    if project.has("concept.json"):
        return project.load_json("concept.json")
    concepts = project.load_json("concepts.json") or []
    if not concepts:
        raise SystemExit("没有梗卡候选")
    if pick in (None, "auto"):
        hint = project.meta.get("direction_hint", "")
        pool = concepts
        if hint:
            matching = [c for c in concepts if c.get("direction") == hint]
            if matching:
                pool = matching
                print(f"[stage] 按指定题材 [{hint}] 在 {len(matching)} 张梗卡中优选")
        ranked = sorted(pool, key=lambda c: -int(c.get("potential_score", 0)))
        # 查重换选：规则（bigram/3-gram）→ Laya 毫秒快筛 → LLM 仲裁兜底，
        # 与历史梗雷同的一律跳过。灰区门控仍在最外层：全"措辞全新"时连语义层都跳过
        hist = _recent_tropes_safe(project)
        rule_flags = [False] * len(ranked)
        sem_flags: list = []
        if hist:
            print(f"[stage] 查重基线：{len(hist)} 条历史梗")
            rule_flags = [any(_trope_similar(c.get("core_trope", ""), h["core_trope"]) for h in hist)
                          for c in ranked]
            # Laya 常驻第一道语义裁判：每次选题都跑（毫秒级，零成本），
            # 只有它拿不准的候选才升级主模型
            sem_flags = _sem_dup_flags(cfg, llm, ranked, hist, project.root.name)
        # 文档一致的候选结构：把查重标志**并进候选本身**，再做禁用道具过滤。
        # 旧实现是三个平行数组（ranked/rule_flags/sem_flags）按下标对齐，
        # 中途 ranked=_clean 后索引错位 → 误杀新梗或放行重复梗（缺陷 P1-1）。
        chosen = None
        _ban_re = banned_regex(cfg or {})
        for k, cand in enumerate(ranked):
            _dup_rule = bool(rule_flags[k]) if k < len(rule_flags) else False
            _dup_sem = bool(sem_flags[k]) if (sem_flags and k < len(sem_flags)) else False
            if _ban_re is not None and _ban_re.search(
                    str(cand.get("core_trope", "")) + str(cand.get("hook", ""))):
                print(f"[stage] 跳过禁用道具梗卡：{cand.get('core_trope', '')[:36]}")
                continue
            if _dup_rule or _dup_sem:
                why = "规则" if _dup_rule else "语义"
                print(f"[stage] 跳过重复梗卡（{why}）：{cand.get('core_trope', '')[:36]}")
                continue
            chosen = cand
            break
        if chosen is None:
            # 全部被筛（重复或禁用道具）→ 优先选不重复的，再退到最高分
            chosen = ranked[0]
            print("[warn] 候选均被判重复或命中禁用道具，按最高分放行（建议换热点源或题材）")
        print(f"[stage] 自动选择梗卡：{chosen.get('core_trope', '')[:40]}")
    elif pick == "ask":
        for i, c in enumerate(concepts):
            print(f"  [{i}] {c.get('potential_score')}分 | {c.get('direction')} | {c.get('core_trope')}")
        # 非 tty（管道 / CI / 后台任务）下 input() 会抛 EOFError 直接把整条流程打断。
        # 这类环境没人能答题，等价于"默认选最高分"，与 `_pick_mode` 的 auto 兜底保持一致。
        try:
            raw = input("选择梗卡序号（回车=0）：").strip() or "0"
        except EOFError:
            raw = "0"
            print("[warn] 非交互环境（stdin 非 tty），自动选择第 0 张梗卡")
        try:
            chosen = concepts[int(raw)]
        except (ValueError, IndexError):
            print(f"[warn] 无效序号 {raw!r}，回退到第 0 张梗卡")
            chosen = concepts[0]
    else:
        chosen = concepts[int(pick)]
    project.save_json("concept.json", chosen)
    project.set_stage("concept", chosen.get("direction", ""))
    return chosen


# ---------------------------------------------------------------------------
# Stage 3：题材知识库（自动搜集）
# ---------------------------------------------------------------------------

@runlog.staged("题材知识卡")
def stage_kb(cfg: dict, llm: BaseLLM, project: Project, force: bool = False):
    """确保所选题材方向有知识卡：没有就自动搜集（失败降级为 LLM 内化）。

    方向缺失时先按核心梗关键词兜底匹配梗库方向；仍然没有方向则直接用核心梗
    联网搜索一轮真实细节——保证"写作前必有一次联网搜集"。
    """
    concept = project.load_json("concept.json") or {}
    direction = concept.get("direction")
    if not direction:
        # 兜底1：核心梗关键词匹配梗库方向
        try:
            direction = match_direction(concept.get("core_trope", ""))
            if direction:
                concept["direction"] = direction
                project.save_json("concept.json", concept)
                print(f"[stage] 方向缺失，按核心梗匹配为「{direction}」")
        except Exception:  # noqa: BLE001
            pass
    if not direction:
        # 兜底2：无方向也联网搜一轮核心梗真实细节，直接作为知识摘要
        if not cfg["knowledge"].get("auto_collect", True) and not force:
            print("[stage] 知识自动搜集已关闭（knowledge.auto_collect=false）")
            return None
        core = (concept.get("core_trope") or project.title or "").strip()
        if not core:
            return None
        try:
            from . import knowledge as kbmod

            hits = kbmod.search_bing(f"{core} 小说 真实细节 常见情节", n=6)
            digest = "\n".join(f"- {h.get('title', '')}：{(h.get('snippet') or h.get('text') or '')[:120]}"
                               for h in hits[:6])
            info = {"direction": "", "digest": digest[:2000], "collected_at": now_ts(),
                    "fallback": False, "source": "direct_search"}
            project.save_json("kb.json", info)
            print(f"[stage] 无方向，已按核心梗联网搜集 {len(hits)} 条资料")
            return info
        except Exception as e:  # noqa: BLE001
            print(f"[stage] 联网搜集失败（{type(e).__name__}），写作将按常识进行")
            return None
    if project.has("kb.json") and not force:
        return project.load_json("kb.json")
    if not cfg["knowledge"].get("auto_collect", True) and not force:
        print("[stage] 知识自动搜集已关闭（knowledge.auto_collect=false）")
        return None
    card = knowledge.load_card(direction)
    if card is None or force:
        card = knowledge.build_card(cfg, resolve_llm(cfg, llm, "light"), direction, force=force)
    info = {
        "direction": direction,
        "digest": knowledge.digest(card, int(cfg["knowledge"].get("digest_budget", 2200))),
        "collected_at": card.get("collected_at", ""),
        "fallback": card.get("fallback", False),
    }
    project.save_json("kb.json", info)
    src = "降级内化" if info["fallback"] else f"{card.get('source_count', 0)}页资料"
    print(f"[stage] 题材知识卡就绪：{direction}（{src}）")
    return info


# ---------------------------------------------------------------------------
# Stage 4：书名与方案
# ---------------------------------------------------------------------------

_TITLE_BAN_WORDS = ("震惊", "惊呆", "竟然", "绝密", "刚刚", "突发", "真相竟是")


def _title_ok(title: str) -> bool:
    """标题门：长度/引号/问号/标题党 只算硬伤，真正决定成败的是「卖点分」。

    口径与理由集中在 `studio/title_gate.py`——书名是信息流里的第一条广告，
    番茄签约被拒的高频原因就是书名没卖点（4 字名、纯名词堆叠、文艺空词）。
    """
    return _TG.acceptable(title)


def _pick_title(pitch: dict) -> str | None:
    """从 pitch 的 title/titles 里挑一个合格标题；都不合格返回 None。"""
    if _title_ok(pitch.get("title", "")):
        return pitch["title"].strip()
    for tt in pitch.get("titles", []) or []:
        if _title_ok(tt):
            return tt.strip()
    return None


def _fallback_title(concept: dict) -> str:
    """模型两次都没给合格标题时的确定性兜底：从梗提炼一个第一人称冲突标题。"""
    trope = (concept.get("core_trope") or concept.get("idea") or "").strip()
    for anchor in ("重生回", "穿成", "这一次", "我要", "我是"):
        idx = trope.find(anchor)
        if idx >= 0:
            seg = re.split(r"[。！？]", trope[idx:])[0].rstrip("，,、 ")
            if 10 <= len(seg) <= 30:
                return seg
    return re.split(r"[。！？]", trope)[0][:28]


@runlog.staged("书名方案")
def stage_pitch(cfg: dict, llm: BaseLLM, project: Project) -> dict:
    if project.has("pitch.json"):
        return project.load_json("pitch.json")
    llm = resolve_llm(cfg, llm, "planner")
    concept = project.load_json("concept.json")
    plat = load_platform(cfg["studio"].get("platform", "zhihu"))
    target = int(cfg["studio"].get("target_words", 10000))
    lo, hi = plat.get("chapter_words", [1500, 2500])
    kb_info = project.load_json("kb.json") or {}
    ct_path = data_path("character_templates.json")
    ct_digest = ""
    if ct_path.exists():
        ct = json.loads(ct_path.read_text(encoding="utf-8"))
        role_lines = [f"- {r['role']}：{r['template']}" for r in ct.get("roles", [])]
        ct_digest = (
            "【人设与叠加梗模板（言情/世情向适用，男频向可参考叠加梗）】\n"
            + "\n".join(role_lines)
            + "\n- 可叠加梗："
            + "、".join(ct.get("stacking_tropes", []))
            + "\n- 人物关系叠加："
            + "、".join(ct.get("relationship_combos", []))
            + "\n- 夸张写法："
            + ct.get("exaggeration", {}).get("note", "")
        )
    # 知识库自动召回：同题材钩子/金句/技法供方案参考（AI 自用，无需用户触发）
    kb_recall_block = ""
    try:
        from . import kb as _kb

        kb_recall_block, _n = _kb.recall(
            cfg, f"{concept.get('core_trope', '')} {concept.get('direction', '')}".strip(),
            direction=concept.get("direction", ""),
            max_chars=420)
        if _n:
            print(f"[stage] 方案注入知识库召回 {len(kb_recall_block)} 字（{_n} 条）")
    except Exception:  # noqa: BLE001
        pass
    # 反重复：近期已用标题禁用清单（自产沉淀，防同款句式反复）
    title_ban = ""
    try:
        from . import memory as _mem

        _mt = [x for x in _mem._load(cfg)
               if x.get("source_project") and x.get("type") == "标题"]
        _mt.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        if _mt:
            title_ban = ("【近期已用标题（句式/悬念点/数字用法都不得近似）】\n"
                         + "\n".join(f"- 《{str(x.get('text', ''))[:36]}》" for x in _mt[:8]))
            print(f"[stage] 标题查重：注入 {len(_mt[:8])} 条近期已用标题")
    except Exception:  # noqa: BLE001
        pass
    # 题材族思维链:不同题材用不同的情节设计顺序(悬疑先诡计/言情先CP/怪谈先场景)
    chain_text = ""
    fam = genre_chain(concept.get("direction", ""))
    if fam:
        chain_text = (f"{fam['name']}——本题材情节必须按以下思维链顺序设计"
                      f"（禁止套用通用言情/世情写法）：\n"
                      + "\n".join(fam.get("chain", []))
                      + "\n特有工具：" + "；".join(fam.get("tools", [])))
        print(f"[stage] 注入题材思维链：{fam['name']}（{len(fam.get('chain', []))} 步）")
    sys_p, user_p = render_prompt(
        "pitch",
        DIRECTION=concept.get("direction", ""),
        EMOTION=concept.get("emotion", ""),
        CORE_TROPE=concept.get("core_trope", ""),
        CONFLICT=concept.get("conflict", ""),
        HOOK=concept.get("hook", ""),
        PLATFORM_NAME=plat["name"],
        PLATFORM_STYLE=plat["style"],
        TARGET_WORDS=target,
        CHAPTER_WORDS=f"{lo}-{hi}",
        KB_DIGEST=kb_info.get("digest", "（无）"),
        KB_RECALL=kb_recall_block or "（暂无召回）",
        CHARACTER_TEMPLATES=ct_digest,
        TITLE_BAN=title_ban,
        DIRECTION_CHAIN=chain_text or "（本题材无专属思维链，按通用设计）",
        BAN_PROPS=_ban_props_text(cfg),
        AXES_SPEC="\n".join(f"- {k}：{v}" for k, v in AXES_SPEC.items()),
        AXES_BAN=axes_ban_text(cfg) or "（暂无，各轴自由选择）",
        IDEA_EXTRA=(
            f"【用户补充】热点原题：{concept.get('hot_title')}；开头钩子参考：{concept.get('hook')}"
            if concept.get("source") != "idea"
            else ""
        ),
    )
    print("[stage] 生成书名与故事方案 …")
    pitch = None
    _base_user_p = user_p
    for _attempt in range(2):
        _p = llm.chat_json(sys_p, user_p)
        if isinstance(_p, dict):
            _t = _pick_title(_p)
            if _t:
                _p["title"] = _t
                pitch = _p
                break
        if _attempt == 0:
            print("[stage] 书名不合格（卖点不足/过长过短/引号问号），带卖点公式重新生成 …")
            user_p = _base_user_p + "\n\n" + _TG.hint()
    if not isinstance(pitch, dict):
        pitch = _p if isinstance(_p, dict) else {}
    if not _title_ok(pitch.get("title", "")):
        pitch["title"] = _fallback_title(concept)
        print(f"[stage] 模型未给合格标题，兜底：《{pitch['title']}》")
    if not isinstance(pitch.get("axes"), dict) or not pitch.get("axes"):
        # 模型漏输出轴时兜底：各轴取近期未用的第一项
        used_vals: set[str] = set()
        for ax in recent_axes(cfg, 3):
            used_vals.update(str(v) for v in ax.values())
        pitch["axes"] = {k: next((o for o in v.split("|") if o not in used_vals), v.split("|")[0])
                         for k, v in AXES_SPEC.items()}
    pitch.setdefault("chapter_count", max(5, min(12, target // 1800)))
    pitch["platform"] = plat["key"]
    pitch["target_words"] = target
    # 书名卖点分随 pitch 落盘：复盘时能回答「当初为什么选这个名」
    pitch["title_score"] = _TG.score(pitch.get("title", ""))
    pitch["title_family"] = _TG.family(pitch.get("title", ""))
    project.save_json("pitch.json", pitch)
    project.set_stage("pitch", pitch.get("title", ""))
    print(f"[stage] 书名：《{pitch.get('title')}》 | 卖点分 {pitch['title_score']}"
          f"（{pitch['title_family']}） | {pitch.get('chapter_count')}章 | 目标{target}字")
    return pitch


# ---------------------------------------------------------------------------
# Stage 3.5：导语（商业短篇标配：导语+编号小节）
# ---------------------------------------------------------------------------

def _fix_banned_text(cfg: dict, llm: BaseLLM, text: str, what: str) -> tuple[str, dict]:
    """禁用道具**定点替换**：只换被点名的道具，不改情节与人设。

    为什么不做整篇重做：禁用道具表是"别自己发明这些东西"，属于局部用词问题；
    `stage_quality_gate` 为它整稿重做实测一轮烧 90s 且字数漂移（e2e9 导语 3 稿
    全是同一处「欠条」）。这里改成一句 LLM 定点替换 + 确定性止损：
    换完仍命中就退回原文并留痕，交给下游章级门兜底。
    """
    from .langqa import format_banned, scan_banned

    brep = scan_banned(text, cfg)
    rep = {"what": what, "before": brep["hits"], "after": None, "applied": False}
    if brep["pass"]:
        rep["after"] = []
        return text, rep
    print(f"[ban] {what} 命中禁用道具 " + "、".join(f"「{h['term']}」×{h['count']}" for h in brep["hits"])
          + " → 定点替换（不重做整稿）")
    try:
        sys_b = ("你是短篇网文校对员。任务只有一件事：把正文里被点名的禁用道具"
                 "替换成等价的情节载体，其余一字不改。")
        user_b = (
            f"{format_banned(brep)}\n\n【硬约束】\n"
            f"- 输出仍是「{what}」本身，除被点名道具外**逐字保留**原句，不要重写、不要润色、不要调顺序\n"
            f"- 字数保持原样（±10%）\n"
            "- 只输出正文，不要任何解释、不要加标题\n"
            f"【原文】\n{text}"
        )
        new = (llm.chat(sys_b, user_b) or "").strip()
        new = re.sub(r"^\s*(?:导语|正文)[:：]\s*", "", new)
        _nb = scan_banned(new, cfg)
        if not new:
            rep["reject"] = "empty"
            print(f"[ban] ⚠ {what} 定点替换返回空，保留原稿")
            return text, rep
        if len(new) < len(text) * 0.8:
            rep["reject"] = f"shrunk {len(new)}/{len(text)}"
            print(f"[ban] ⚠ {what} 定点替换夹带重写（{len(text)}→{len(new)} 字），保留原稿")
            return text, rep
        if _nb["pass"]:
            rep.update({"after": [], "applied": True})
            print(f"[ban] {what} 定点替换完成（{len(text)}→{len(new)} 字）")
            return new, rep
        rep["after"] = _nb["hits"]
        rep["reject"] = "still_hit"
        print(f"[ban] ⚠ {what} 定点替换未消除（剩余 "
              + "、".join(h["term"] for h in _nb["hits"]) + "），保留原稿交下游兜底")
    except Exception as e:  # noqa: BLE001
        rep["error"] = f"{type(e).__name__}: {e}"
        print(f"[ban] ⚠ {what} 定点替换失败（{e}），保留原稿")
    return text, rep


@runlog.staged("导语")
def stage_lead(cfg: dict, llm: BaseLLM, project: Project) -> dict:
    """生成 300-400 字导语（六拍框架：噱头→接情绪→叠加反转→下饵），带阶段达标门。"""
    if project.has("lead.json"):
        return project.load_json("lead.json")
    pitch = project.load_json("pitch.json") or {}
    concept = project.load_json("concept.json") or {}
    llm = resolve_llm(cfg, llm, "planner")

    _last = {"text": "", "words": 0}

    def _gen(round: int, issues: str):
        sys_p, user_p = render_prompt(
            "lead",
            TITLE=pitch.get("title", ""),
            CORE_TROPE=concept.get("core_trope", ""),
            CONFLICT=concept.get("conflict", "") or pitch.get("core_conflict", ""),
            HOOK=concept.get("hook", ""),
            PROTAGONIST=json.dumps(pitch.get("protagonist", {}), ensure_ascii=False),
            ANTAGONIST=json.dumps(pitch.get("antagonist", {}), ensure_ascii=False),
            EMOTION=concept.get("emotion", ""),
            GENRE_GUIDE=load_genre_guide(cfg, concept.get("direction", "")),
        )
        if round > 1 and issues:
            # 字数纠偏：评审文字建议常被写手无视（235→509→237 式震荡），
            # 改为把上一稿原文回灌做「只扩不改 / 只压不删拍」的具体任务——比整篇重写容易得多
            prev_t, prev_w = _last["text"], _last["words"]
            if prev_t and prev_w < 280:
                user_p += (
                    f"\n\n【扩写任务（只扩不改）】下面这篇导语初稿只有 {prev_w} 字，太短。"
                    "请一字不改地保留它的全部句子、语序和情节，只在每一拍内部补充具体细节"
                    "（场景名称、人物动作与反应、具体数字、对话碎片），把全文扩写到 320-400 字。"
                    "严禁新增情节、严禁换一种写法、严禁删句子。\n【初稿】\n" + prev_t
                )
            elif prev_t and prev_w > 420:
                user_p += (
                    f"\n\n【压缩任务（只压不删拍）】下面这篇导语初稿 {prev_w} 字，太长。"
                    "请保留六拍各自的核心信息，删掉背景铺垫、次要人物反应与抒情闲笔，压到 300-400 字。\n【初稿】\n" + prev_t
                )
            elif prev_t:
                user_p += (
                    f"\n\n【保数令】上一稿 {prev_w} 字已在合格区间。本稿修改内容问题时，"
                    "全文必须保持 300-400 字：每删一句就要在原位补一句，净增减不超过 ±30 字，"
                    "不许整篇重写导致字数漂移。"
                )
            user_p += (f"\n\n【主编打回意见（必须逐条改进，这是第{round}稿）】\n{issues}")
        try:
            from . import kb as _kb

            lp = _kb.lead_patterns(cfg, direction=concept.get("direction", ""))
            if lp:
                user_p += ("\n\n【导语库高分范例（同题材开头模式对照，写法可借鉴，内容禁照抄）】\n" + lp
                           + "\n注意：范例中的人名、题材道具与句模板一律禁止模仿（禁止同姓/同字辈/近义道具，"
                             "禁止复刻‘又是穿书死局/又是算账’式套路模板）。导语必须为本篇题材与梗原创，"
                             "第一句就要有具体冲突画面。")
                if round == 1:
                    print("[stage] 注入导语模式库")
        except Exception:
            pass
        print(f"[stage] 撰写导语（六拍框架）{f'（第{round}稿）' if round > 1 else ''}…")
        raw = llm.chat(sys_p, user_p)
        m = re.search(r"导语[:：]\s*(.*)", raw, re.S)
        text = (m.group(1) if m else raw).strip()
        lines = [
            l for l in text.splitlines()
            if l.strip() and not re.fullmatch(r"[（(][^（）)]{1,20}[）)]", l.strip())
        ]
        text = "\n".join(lines).strip()
        _last["text"], _last["words"] = text, count_words_zh(text)
        return {"text": text, "words": _last["words"], "created_at": now_ts()}

    lead, gate = stage_quality_gate(cfg, llm, project, "导语", _gen)
    lead["gate"] = gate["gate"]
    # 禁用道具只能定点替换，不能整稿重做（决策 7 / e2e9 实测 3 稿同一处「欠条」）
    _fixed, _ban_rep = _fix_banned_text(cfg, llm, lead["text"], "导语")
    if _ban_rep.get("applied") and _fixed != lead["text"]:
        lead["text"] = _fixed
        lead["words"] = count_words_zh(_fixed)
    if _ban_rep.get("before"):
        lead["ban_fix"] = _ban_rep
    project.save_json("lead.json", lead)
    project.set_stage("lead", f"{lead['words']}字")
    print(f"[stage] 导语 {lead['words']} 字（{gate['gate']}）")
    return lead


# 商贾算账套路词族：命中过密说明大纲又钻"账本/记账/搞钱"套路（用户高频反馈）
# 含马甲词：连本带利/小本本/清算/血债/从…算到…（换个道具还是记账式记仇）
# 记账道具词（用户反复点名禁止的"记账式记仇"载体）：这些反复出现才是真套路。
_ACCOUNT_WORDS = re.compile(
    r"账本|记账|账册|账房|算盘|欠条|收据|小本本|记仇本|盘账|对账|记在账|这账我记下|账上记"
)
# 注意："算账"（"找你算账"=报仇/讨说法）是口语，不算记账道具，不纳入。
# 通用账务词（古言/修仙背景常见，如"银子""铺子""买卖""还债"，不单独判定为记账套路）。
_ACCOUNT_GENERIC = re.compile(
    r"银子|铺子|买卖|划算|还债|欠债|清算|血债|讨回|本金|利息|讨价|还价|地契|卖身契"
)


def _trope_reuse_check(text: str) -> str:
    """记账式套路密度检测：只统计"账本/记账/算盘/欠条"等记账道具词（≥2次），
    通用账务词（银子/铺子等古言常见）不计入。命中返回清单，否则空串。"""
    if not text:
        return ""
    hits = _ACCOUNT_WORDS.findall(text)
    if len(hits) < 2:
        return ""
    from collections import Counter
    top = Counter(hits).most_common(4)
    return "、".join(f"{w}×{n}" for w, n in top)


# ---------------------------------------------------------------------------
# Stage 4：章节大纲
# ---------------------------------------------------------------------------

@runlog.staged("章节大纲")
def stage_outline(cfg: dict, llm: BaseLLM, project: Project) -> dict:
    if project.has("outline.json"):
        return project.load_json("outline.json")
    llm = resolve_llm(cfg, llm, "planner")
    pitch = project.load_json("pitch.json")
    plat = load_platform(pitch.get("platform", "zhihu"))
    chapters = int(pitch.get("chapter_count", 6))
    wpc = calc_chapter_words(plat, int(pitch.get("target_words", 10000)), chapters)

    def _gen(round: int, issues: str):
        direction = (project.load_json("concept.json") or {}).get("direction", "")
        sys_p, user_p = render_prompt(
            "outline",
            TITLE=pitch.get("title", ""),
            LOGLINE=pitch.get("logline", ""),
            PROTAGONIST=json.dumps(pitch.get("protagonist", {}), ensure_ascii=False),
            ANTAGONIST=json.dumps(pitch.get("antagonist", {}), ensure_ascii=False),
            CONFLICT=pitch.get("core_conflict", ""),
            PEAKS="；".join(pitch.get("emotional_peaks", [])),
            ENDING=pitch.get("ending_type", ""),
            CHAPTERS=chapters,
            WORDS_PER_CHAPTER=wpc,
            GENRE_GUIDE=load_genre_guide(cfg, direction),
            BAN_PROPS=_ban_props_text(cfg),
        )
        if round > 1 and issues:
            user_p += (f"\n\n【主编打回意见（必须逐条改进，这是第{round}稿）】\n{issues}")
        wspec = axes_spec_text(pitch)
        if wspec:
            user_p += (f"\n\n【本篇写法指定（方案层已定，大纲必须结构化落实，不得漂移）】\n{wspec}"
                       "\n落实要求：时序轴决定章节编排顺序；信息差轴决定每章 open_hook/end_hook"
                       "的信息释放方向；视角轴决定各节叙事主体。")
        fam = genre_chain((project.load_json("concept.json") or {}).get("direction", ""))
        if fam:
            user_p += (f"\n\n【{fam['name']}·大纲设计思维链（按此顺序设计情节，"
                       "禁止通用套路）】\n" + "\n".join(fam.get("chain", []))
                       + "\n" + fam.get("outline_rule", ""))
        if skel:
            user_p += "\n\n【同题材高分结构骨架（对照布局用，情节必须原创）】\n" + skel
            if round == 1:
                print("[stage] 注入结构骨架对照")
        print(f"[stage] 生成章节大纲与卡点表 {f'（第{round}稿）' if round > 1 else ''}…")
        outline = llm.chat_json(sys_p, user_p)
        chs = outline.get("chapters", [])
        # 部分模型（GLM 等）会把 chapters 输出成字符串数组：带纠错提示重试一次
        if chs and not all(isinstance(c, dict) for c in chs):
            outline = llm.chat_json(
                sys_p, user_p + "\n\n【格式纠错】你输出的 chapters 不是对象数组。"
                "重新输出：chapters 必须是 JSON 对象数组，每个元素含 "
                "title/goal/beats(数组)/open_hook/end_hook 字段，禁止输出字符串。")
            chs = [c for c in outline.get("chapters", []) if isinstance(c, dict)]
        # 章节数不足：模型偶尔只吐 1 节（长 JSON 截断/偷懒），带结构纠错重试一次，
        # 否则整篇会退化成单节，门禁必然打回且成稿字数无法达标。
        expected = max(4, chapters)
        if len(chs) < expected - 1:
            outline2 = llm.chat_json(
                sys_p, user_p + f"\n\n【结构纠错】你只输出了 {len(chs)} 节，本篇必须 "
                f"{expected} 节。请重新输出完整 JSON：chapters 必须恰好 {expected} 个对象，"
                "逐节推进冲突并给出章末钩子，不得省略或合并小节。")
            chs2 = [c for c in outline2.get("chapters", []) if isinstance(c, dict)]
            if len(chs2) > len(chs):
                outline, chs = outline2, chs2
                print(f"[stage] 大纲结构纠错：{len(chs)}/{expected} 节")
        if not chs:
            raise SystemExit("大纲生成为空")
        for i, c in enumerate(chs):
            c["no"] = i + 1
            # 钉进平台区间：知乎 800-1200 反转单元、番茄 2050-2300，防写手超写
            lo, hi = plat.get("chapter_words", [1500, 2500])
            wt = int(c.get("words_target") or wpc)
            c["words_target"] = max(int(lo), min(int(hi), wt))
        return outline

    # 知识库结构骨架视图：同方向已拆书目的弧线分布（_gen 内每轮附加到 user_p）
    try:
        from . import kb as _kb

        skel = _kb.arc_skeleton(cfg, direction=(project.load_json("concept.json") or {}).get("direction", ""))
    except Exception:
        skel = ""
    # 道具/玩法去重外层循环：大纲过评审后仍检测"商贾算账"套路密度，命中则打回换玩法
    reuse_fb = ""
    outline: dict = {}
    gate: dict = {}
    for _ in range(3):
        outline, gate = stage_quality_gate(cfg, llm, project, "大纲", _gen, extra_issues=reuse_fb)
        chs_t = outline.get("chapters", []) or []
        outline_text = " ".join(
            str(c.get("title", "")) + " " + " ".join(str(b) for b in (c.get("beats") or [])[:8])
            for c in chs_t
        )
        reuse = _trope_reuse_check(outline_text)
        if not reuse:
            break
        reuse_fb = ("【核心道具/玩法去重】你的大纲仍围绕账务/交易套路展开（命中：" + reuse +
                    "）。近期作品已把账本/记账/算盘/死契类写滥，必须整体更换核心道具与玩法，"
                    "角色姓名也禁止含账务/交易词（「沈算盘」「赵守财」式名字必须换姓换名、"
                    "禁止同姓/同字辈/近形字），围绕全新设定重写全部小节标题、beats 与钩子"
                    "（禁止再用账本/记账/算盘/死契/地契/铺子/军饷/银子买卖类设定）。")
        print(f"[gate] 大纲核心道具雷同（{reuse}），打回更换玩法（第{_ + 1}轮）…")
    chs = outline.get("chapters", [])
    project.save_json("outline.json", outline)
    project.set_stage("outline", f"{len(chs)}节")
    print(f"[stage] 大纲 {len(chs)} 节，每节约 {wpc} 字（{gate['gate']}）")
    return outline


# ---------------------------------------------------------------------------
# Stage 5：故事圣经
# ---------------------------------------------------------------------------

@runlog.staged("故事圣经")
def stage_bible(cfg: dict, llm: BaseLLM, project: Project) -> dict:
    if project.has("bible.json"):
        return project.load_json("bible.json")
    llm = resolve_llm(cfg, llm, "planner")
    pitch = project.load_json("pitch.json")
    outline = project.load_json("outline.json")
    # 知识库自动召回：同类故事的冲突/结构模式，供伏笔与场景设计参考
    kb_block = ""
    try:
        from . import kb as _kb

        kb_block, _n = _kb.recall(
            cfg, f'{pitch.get("logline", "")} {pitch.get("core_conflict", "")}'.strip(),
            direction=(project.load_json("concept.json") or {}).get("direction", ""),
            max_chars=360)
        if _n:
            print(f"[stage] 圣经注入知识库召回 {len(kb_block)} 字（{_n} 条）")
    except Exception:  # noqa: BLE001
        pass

    def _gen(round: int, issues: str):
        sys_p, user_p = render_prompt(
            "bible",
            TITLE=pitch.get("title", ""),
            LOGLINE=pitch.get("logline", ""),
            PITCH_JSON=json.dumps(pitch, ensure_ascii=False),
            OUTLINE_JSON=json.dumps(outline, ensure_ascii=False),
            KB_BLOCK=kb_block or "（暂无召回）",
            GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
        )
        if round > 1 and issues:
            user_p += (f"\n\n【主编打回意见（必须逐条改进，这是第{round}稿）】\n{issues}")
        print(f"[stage] 生成故事圣经（人物/伏笔/时间线）{f'（第{round}稿）' if round > 1 else ''}…")
        bible = llm.chat_json(sys_p, user_p)
        # 字符串字段归一：部分模型（GLM 等）会把 timeline/setting 等输出成数组
        for key in ("timeline", "setting"):
            v = bible.get(key)
            if isinstance(v, list):
                bible[key] = "；".join(str(x) for x in v)
            elif v is not None and not isinstance(v, str):
                bible[key] = json.dumps(v, ensure_ascii=False)
        tone = bible.get("tone")
        if isinstance(tone, str):
            bible["tone"] = {"style": tone}
        return bible

    bible, gate = stage_quality_gate(cfg, llm, project, "圣经", _gen)
    # 与大纲伏笔表合并
    ol_f = [f for f in ((outline or {}).get("foreshadows", []) or []) if isinstance(f, dict)]
    bl_f = [f for f in (bible.get("foreshadows", []) or []) if isinstance(f, dict)]
    bible["characters"] = [c for c in (bible.get("characters", []) or []) if isinstance(c, dict)]
    seen = {f.get("id") for f in bl_f}
    bible["foreshadows"] = bl_f + [f for f in ol_f if f.get("id") not in seen]
    bible["gate"] = gate["gate"]
    project.save_json("bible.json", bible)
    project.set_stage("bible", f"{len(bible.get('characters', []))}人")
    return bible


# ---------------------------------------------------------------------------
# Stage 6：逐章扩写
# ---------------------------------------------------------------------------

def _chain_errors(direction: str) -> str:
    """题材族高频设计错误 → 审稿红线文本。"""
    fam = genre_chain(direction)
    if not fam or not fam.get("errors"):
        return ""
    return "；".join(fam["errors"])


def _genre_note(cfg: dict, direction: str) -> str:
    """题材写作约束:把方向的硬性元素+题材族写法要领转成每章写作提示。支持组合方向。"""
    if not direction:
        return "（无特殊题材约束）"
    parts = []
    dirs = load_tropes()["directions"]
    for want in split_combo(direction):
        for d in dirs:
            if d["name"] == want:
                if d.get("required_elements"):
                    parts.append(f"{want}每章必须体现：" + "；".join(d["required_elements"][:3]))
                if d.get("persona_formulas"):
                    parts.append("人物配平参考：" + d["persona_formulas"][0])
                break
    if any(k in direction for k in ("后宫", "言情", "追妻")):
        parts.append("事业/冲突戏推进时,同步写至少1处情感互动细节(眼神/动作/吃醋/守护),不许整章零情感线")
    fam = genre_chain(direction)
    if fam and fam.get("draft_rule"):
        parts.append(fam["draft_rule"])
    # 题材正文提示卡（data/genre_cards.json，来自本地开源项目的同题材长篇抽样）：
    # 只取「节奏密度 + 禁止漂移 + 样本统计」三行，塞进每章提示，成本 <150 字。
    try:
        from . import langqa as _lq
        from . import genre_cards as _gc

        _ancient = bool(_lq.genre_tags(direction))
        _name, _card = _gc.match(direction, ancient=_ancient)
        if _card:
            _note = _gc.note(_card)
            if _note:
                parts.append(f"【{_name}题材卡】{_note}")
    except Exception:  # noqa: BLE001 - 增强项，失败不影响写作
        pass
    return "；".join(parts) if parts else "（按方向常规写法）"


def _chapter_ctx(cfg, project: Project, no: int, state: dict) -> dict:
    """构造单章扩写/审查/重写共用的上下文（即 draft prompt 的全部占位符）。"""
    pitch = project.load_json("pitch.json")
    outline = project.load_json("outline.json")
    bible = project.load_json("bible.json")
    chs = outline["chapters"]
    ch = chs[no - 1]
    total = len(chs)
    prev_summary = state["summaries"].get(str(no - 1), "（这是第一章，无）")
    prev_tail = state["tails"].get(str(no - 1), "（无）")
    outline_brief = "\n".join(
        f"小节{int(c['no']):02d} {c['title']}：{c.get('goal', '')}" for c in chs
    )
    # 伏笔义务简报：按本章过滤「该埋/该收/逾期该补」，替代旧的全量平铺。
    # 旧实现把 bible.foreshadows 每条都拼成一行塞进提示词（不管和本章有没有关系），
    # 模型分不清哪条是本章义务；也没有状态和超期概念，收没收无人校验。
    # 现在走 studio.foreshadow（借鉴 openwrite B08 的状态机，按短篇体量裁剪）。
    foreshadow_brief = _foreshadow.chapter_brief(bible, no)
    if not foreshadow_brief:
        foreshadow_brief = "（本章无伏笔埋设/回收义务）"
    bible_brief = json.dumps(
        {
            "characters": bible.get("characters", []),
            "timeline": bible.get("timeline", ""),
            "setting": bible.get("setting", ""),
            "tone": bible.get("tone", {}),
            "foreshadows": foreshadow_brief,
        },
        ensure_ascii=False,
        indent=1,
    )
    kb_info = project.load_json("kb.json") or {}
    # 知识卡按章选材：旧实现每章注入同一份截断摘录，写手被反复锚定同一批事实，
    # 直接助长成稿里"同一信息写三遍"。这里按本章 title/goal/beats 检索相关条目，
    # 并把前面章节用过的条目记进 kb_used.json 不再重复喂（跨章不重复）。
    kb_text = kb_info.get("digest", "")
    if kb_info.get("direction"):
        _card = knowledge.load_card(kb_info["direction"])
        if _card:
            _used_ledger = project.load_json("kb_used.json") or {}
            _used_keys = set(_used_ledger.get("keys") or [])
            _q = " ".join([str(ch.get("title") or ""), str(ch.get("goal") or ""),
                           " ".join(str(b) for b in (ch.get("beats") or []))])
            _sel, _keys = knowledge.digest_for(
                _card, query=_q, budget=int(cfg["knowledge"].get("digest_budget", 2200)),
                exclude=_used_keys)
            # 条目被前几章吃光时会选不出东西（8 章 × 每章 6-9 条 > 卡片条目数）。
            # 此时不能给写手一份空卡，退回"允许复用"再选一次——宁可重复也不能空。
            if not _sel or _sel == "（无）" or len(_sel) < 120:
                _sel2, _keys2 = knowledge.digest_for(
                    _card, query=_q,
                    budget=int(cfg["knowledge"].get("digest_budget", 2200)))
                if _sel2 and _sel2 != "（无）" and len(_sel2) > len(_sel or ""):
                    _sel, _keys = _sel2, _keys2
            if _sel and _sel != "（无）":
                kb_text = _sel
                _used_ledger["keys"] = sorted(_used_keys | set(_keys))
                project.save_json("kb_used.json", _used_ledger)
    concept = project.load_json("concept.json") or {}
    # 弧位推导：章节序号映射到叙事弧（起/承/转/合），供知识系统定向检索
    arc_of_ch = "起" if no <= max(1, int(total * 0.2)) else         "承" if no <= max(2, int(total * 0.55)) else         "转" if no <= max(3, int(total * 0.85)) else "合"
    # 知识引力检索：kb 三路融合（BM25+向量+共现扩散）→ memory 河道 → 素材 digest 逐级兜底
    rag_block, n_hit = "", 0
    try:
        from . import material as _mat, memory, rag

        rag.ensure(cfg)
        rag_q = " ".join([concept.get("core_trope", ""), concept.get("direction", ""),
                          ch.get("goal", ""), ch.get("title", "")]).strip()
        try:
            from . import kb as _kb

            rag_block, _used = _kb.recall(cfg, rag_q or concept.get("direction", ""),
                                          arc=arc_of_ch,
                                          direction=concept.get("direction", ""), max_chars=520)
            if rag_block:
                print(f"[ctx] 小节{no:02d}（弧:{arc_of_ch}）注入知识引力 {len(rag_block)} 字")
        except Exception:
            pass
        if not rag_block:
            rag_block, _used = memory.recall(cfg, rag_q or concept.get("direction", ""),
                                             direction=concept.get("direction", ""), max_chars=520)
        if not rag_block:
            rag_block = rag.context_for(cfg, rag_q or concept.get("direction", ""),
                                        direction=None, max_chars=520)
        if not rag_block:
            rag_block = _mat.digest(cfg, n=5, max_chars=400)
        n_hit = 0 if rag_block in ("", "（无）") else len(rag_block.splitlines())
        if n_hit and not rag_block.startswith("["):
            print(f"[ctx] 小节{no:02d} 注入知识检索 {len(rag_block)} 字（{n_hit} 条素材/知识卡）")
        if rag_block in ("", "（无）"):
            rag_block = "（知识库暂无相关素材，按题材常识与知识卡写作）"
    except Exception:
        rag_block = "（知识库暂无相关素材，按题材常识与知识卡写作）"
    # 跨章情绪衔接：统计连续虐章数，给本章补偿提示
    emotions = state.get("emotions", {})
    nue_run = 0
    no_ = no - 1
    while no_ >= 1 and emotions.get(str(no_)) == "虐":
        nue_run += 1
        no_ -= 1
    if nue_run >= 1:
        hint = (
            f"上一章情绪偏虐（已连续{nue_run}章），本章必须安排爽点补偿与情绪释放，"
            "让读者'给情绪→给发泄→给补偿→给爽感'闭环"
        )
    else:
        hint = "上一章非虐章，按大纲正常安排情绪落点，保持至少两种情绪类型"
    return {
        "TITLE": pitch.get("title", ""),
        "NO": no,
        "NO2": f"{no:02d}",
        "TOTAL": total,
        "TOTAL2": f"{total:02d}",
        "CH_TITLE": ch.get("title", ""),
        "STYLE_GUIDE": load_style_guide(),
        "BIBLE": bible_brief,
        "FORESHADOW": foreshadow_brief,
        "_BIBLE_RAW": bible,   # 下划线键不进提示词；供 agents 的 pov 人称漂移门禁用
        "KNOWLEDGE": kb_text or "（无，按常识写作）",
        "RAG": rag_block,
        "OUTLINE": outline_brief,
        "PREV_TAIL": prev_tail,
        "PREV_SUMMARY": prev_summary,
        "GOAL": ch.get("goal", ""),
        "BEATS": "\n".join(f"- {b}" for b in ch.get("beats", [])),
        "OPEN_HOOK": ch.get("open_hook", ""),
        "END_HOOK": ch.get("end_hook", ""),
        "WORDS": int(ch.get("words_target", 2000)),
        "SCOPE": f"小节{no:02d}",
        "EMOTION_HINT": hint,
        # 题材名单独占一个键：语言规则（`langqa.scan(genres=...)`）要按题材分流，
        # 不能靠提示词里的 GENRE_* 文本反推。
        "DIRECTION": concept.get("direction", ""),
        "GENRE_NOTE": _genre_note(cfg, concept.get("direction", "")),
        "GENRE_GUIDE": load_genre_guide(cfg, concept.get("direction", "")),
        "WRITE_SPEC": axes_spec_text(pitch) or "（按大纲默认写法）",
        "CHAIN_ERRORS": _chain_errors(concept.get("direction", "")),
        "BAN_PROPS": _ban_props_text(cfg),
    }


def _should_refresh_criteria(cfg: dict, verdict: dict, body: str, review_enabled: bool) -> bool:
    """评审卡证据是否已与终稿脱节，需要重审一次。

    触发条件（全部满足）：
      1. 本轮真的跑过主编门（review_enabled 且 verdict 里带 criteria）；
      2. verdict 记录了它审过的那一版正文（reviewed_text_sha）；
      3. 该版本与将要落盘的终稿不同 —— 说明读者团/情绪门/终稿复查改写过正文。

    为什么必须重审而不是"标注 stale 就算了"：评分卡的用途是交付证据（看板详情、
    复盘、投稿包都读它）。一份整体失效的评分卡比没有评分卡更糟——它会让"证据驱动"
    退化成"看起来有证据"。实测 42 张卡 24 张 stale，引文在终稿里 0 条可核验。
    """
    if not review_enabled or not isinstance(verdict, dict) or not verdict:
        return False
    if not (verdict.get("criteria") or []):
        return False
    old_sha = str(verdict.get("reviewed_text_sha") or "")
    if not old_sha:
        return False
    if old_sha == review_mod.text_sha(body):
        return False
    # 成本闸：refresh 每章最多 1 次主编调用，配置可关（离线/省钱场景）
    rcfg = ((cfg or {}).get("review") or {}) if isinstance(cfg, dict) else {}
    return bool(rcfg.get("rubric_refresh_on_drift", True))


def _refresh_review_verdict(cfg: dict, editor, ctx: dict, body: str,
                            old_verdict: dict, no: int) -> tuple[dict | None, dict]:
    """用终稿重审一次，产出与交付稿对齐的 verdict（不触发重写）。

    只做一件事：把 criteria/evidence 重新建立在对的正文上。失败则返回 (None, rep)，
    调用方保留旧 verdict —— 评分卡会标 criteria_stale 并给出说明，比丢掉证据好。
    """
    rep = {"attempted": True, "ok": False}
    try:
        rcfg = cfg["review"]
        max_rounds = int(rcfg.get("max_review_rounds", 3))
        verdict = editor.review(ctx, body, 1, max_rounds)
        if str(verdict.get("verdict")).lower() in ("degraded", "failed"):
            rep["error"] = str(verdict.get("error") or "degraded")[:160]
            print(f"    [rubric] 终稿重审降级（保留原证据并标注 stale）：{rep['error']}")
            return None, rep
        if not (verdict.get("criteria") or []):
            rep["error"] = "重审未返回 criteria"
            return None, rep
        new_verdict = {
            # 门禁结论（verdict/score/issues）**沿用原始那一轮**：它已经驱动过重写
            # 决策，事后重审不能反过来改写"这稿是通过还是打回"。重审只负责把
            # criteria/evidence 重新挂到交付稿上，属观测层，不得篡改判定层。
            "verdict": old_verdict.get("verdict"),
            "score": old_verdict.get("score"),
            "issues": old_verdict.get("issues") or [],
            "criteria": verdict.get("criteria") or [],
            "reviewed_text_sha": review_mod.text_sha(body),
            "reviewed_text": body[:4000],
            "refreshed_from_stale": True,
            # 重审自身的判断另存，供排查"重审与首审结论不一致"的情况
            "refresh_verdict": verdict.get("verdict"),
            "refresh_score": verdict.get("score"),
        }
        rep["ok"] = True
        print(f"    [rubric] 终稿与主编所见版本不一致，已重审并重建证据（ch{no:02d}）")
        return new_verdict, rep
    except Exception as e:  # noqa: BLE001
        rep["error"] = f"{type(e).__name__}: {e}"[:160]
        print(f"    [rubric] 终稿重审失败（保留原证据并标注 stale）：{rep['error']}")
        return None, rep


def _stage_write_hybrid(cfg: dict, llm: BaseLLM, project: Project, writer_llm: BaseLLM,
                        writer: agents.WriterAgent, todo: list[int], chs: list[dict],
                        state: dict, meta: dict) -> None:
    """hybrid：单 writer 同一上下文续写全部章节。

    - history 累积前文 assistant 正文，每次续写全量携带（上下文不重置）
    - 不跑 review/reader/emotion/langqa 逐章门禁，不打回
    - 每章正文落盘并更新衔接状态；断点续跑时从已落盘章节重建 history
    """
    history: list[dict] = []
    first_no = todo[0] if todo else 1
    # 断点续跑：把早于本次首章的已落盘正文重建进 history，保住上下文连续性
    for no in range(1, first_no):
        f = project.chapter_file(no)
        if f.exists():
            history.append({"role": "assistant", "content": f.read_text(encoding="utf-8")})
    for no in todo:
        ch = chs[no - 1]
        ctx = _chapter_ctx(cfg, project, no, state)
        if isinstance(writer_llm, MockLLM):
            writer_llm.target_words = int(ch.get("words_target", 2000))
        print(f"[stage] 撰写小节 {no:02d}/{len(chs)}：{ch.get('title')} …")
        raw = writer.write_full(ctx, history)
        # 英文思考泄漏兜底：thinking=disabled 偶发失败时，剥离思考前缀或重试
        if _english_think_leak(raw):
            stripped = _strip_english_think(raw)
            if stripped and count_words_zh(stripped) >= int(ch.get("words_target", 1000)) * 0.6:
                print("    [stage] 检测到英文思考前缀，已剥离")
                raw = stripped
            else:
                print("    [stage] 检测到英文思考泄漏且无正文，重试…")
                raw = writer.write_full(ctx, history)
        body, summary = agents.split_summary(raw)
        # hybrid 也写 reviews/chNN.json：下游（看板详情/素材沉淀/复盘/批处理脚本）
        # 全部按章读这个文件，旧实现只写 project.json.reviews → hybrid 项目在各处
        # 显示 0 章 0 门禁（缺陷 P1-7）。gate 语义必须诚实：这是"跳过了逐章门禁"，
        # 不是"通过了门禁"。
        _hrep = {"no": no, "gate": "skipped_hybrid",
                 "reason": "hybrid 模式：单写手同上下文续写，不做逐章四道门禁",
                 "words": count_words_zh(body)}
        meta.setdefault("reviews", {})[str(no)] = {"gate": "skipped_hybrid"}
        project.save_json(f"ch{no:02d}.json", _hrep, subdir="reviews")
        _drafts.commit_chapter(project, no, body, gate=_hrep.get("gate", "skipped_hybrid"))
        state["summaries"][str(no)] = summary
        state["tails"][str(no)] = _tail(body, 300)
        project.save_meta(meta)
        history.append({"role": "assistant", "content": raw})
        print(f"    → 小节{no:02d} {count_words_zh(body)} 字（hybrid 续写）")


# 英文思考泄漏：判据实现集中在 langqa.think_leak（写稿/续写/润色共用同一套规则，
# 避免规则双副本漂移）。这里只保留兼容入口。
def _english_think_leak(text: str) -> bool:
    """正文是否为思考泄漏（全文口径，委托 langqa.think_leak）。"""
    from .langqa import think_leak

    return think_leak(text)


def _strip_english_think(text: str) -> str:
    """剥离英文思考前缀，返回后面的中文正文；找不到正文则返回空串。

    模型思考结束后会另起一段写正文（首行通常是中文）。逐段扫描，
    跳过"英文为主或纯思考"的前导段，从第一个"中文为主"的段落开始保留。
    """
    paras = text.split("\n")
    for i, p in enumerate(paras):
        cn = len(re.findall(r"[一-龥]", p))
        en = len(re.findall(r"[A-Za-z]", p))
        if cn >= 4 and cn > en:
            return "\n".join(paras[i:]).strip()
    return ""


_META_PREFACE = re.compile(
    r"(我将为?(您|你|大家)?(创作|撰写|生成|写|带来|开始)|"
    r"我来为?(您|你)(创作|撰写|写)|"
    r"以下是(我)?(为?(您|你)|一篇)|"
    r"好的?[，,]?\s*我(将|来|这就)|"
    r"这是(一)?篇(关于)?|"
    r"严格遵循(所有|上述)?(写作)?规范|格式要求|"
    r"我先定(标题|骨架|个?标题)|先定(标题|骨架)|"
    r"一气呵成(写完|写|完成|写完全稿))"
)
# 结构/过程元说明：导语与正文之间的过渡句、结尾的字数/分节/结构总结（不是故事内容）。
_META_STRUCTURE = re.compile(
    r"(导语(写完|结束|已完|完毕|到这里)|"
    r"正文(从|开始|如下|起)|"
    r"以下(为|是)?正文|"
    r"(全稿|全文|本篇|本稿|全文)(约|共|分|字数)|"
    r"分\s*\d+\s*节|共\s*\d+\s*节|"
    r"每节(均|在|字数|约)|"
    r"梗的(核心|三个|设定|主线)|"
    r"(字数|章节|结构)(统计|说明|总结|如下)|"
    r"分别落在第|围绕这(几条|三条|些))"
)


def _strip_chapter_summary(text: str) -> str:
    """删除模型误输出的「[本章摘要]/[本章小结]」段。

    两种形态都覆盖：①标记与摘要同行（"[本章摘要] xxx"）；②标记单独一行、摘要在下一行。
    摘要段一直删到空行或下一节头为止。
    """
    _mark = re.compile(r"^[\[【「]\s*本章(?:摘要|小结|梗概|总结|内容回顾)\s*[\]】」]?\s*[:：]?")
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for ln in lines:
        s = ln.strip()
        if _mark.match(s):
            skipping = True
            continue
        if skipping:
            if not s:  # 空行：摘要段结束
                skipping = False
                out.append(ln)
                continue
            if _SEC_HEAD.match(s):  # 节头：摘要段结束
                skipping = False
                out.append(ln)
                continue
            continue  # 摘要正文，删除
        out.append(ln)
    return "\n".join(out)


def _final_clean(full: str, keep_titles: bool = False,
                 max_sections: int | None = None) -> str:
    """成稿装配前的最终全局清理（兜底，无论中间哪轮产生）。

    1) 删 [本章摘要]/[本章小结] 段（重写/补丁轮可能再次产生）；
    2) 删中文语境里的英文泄漏（如 beneficiaries，长英文单词多为模型失控）；
    3) 统一半角逗号为中文逗号（中文正文里的半角逗号是泄漏）；
    4) 统一节头：word_gate 扩写/重写后模型会混写 ## 0X / 0X / 第N节。
       - 知乎盐选要求「纯数字小节、不加小节标题」→ 默认只留「NN」；
       - 番茄/七猫等排版稿需要「NN 标题」可读性 → keep_titles=True；
       - 只对**真节头**重写编号，数字开头的叙述行原样保留（历史事故：旧实现
         把「24 岁那年…」「2. 葛掌柜拿出验灵石板…」整行替换成裸编号，吞掉 4.8 万字）；
       - 编号按「下一个期望编号」判定：首发必须是 1，之后只接受 prev+1；
         跳号或重复时按 prev+1 顺延（保结构、不产生重复/回退节号）；
         编号远超本篇节数的判为正文（原样保留，不吞内容）。
    """
    full = _strip_chapter_summary(full)
    # 长英文单词（≥4，前后紧邻中文或行内）判为泄漏；保留 CEO/OK 等短缩写
    full = re.sub(r"(?<=[一-龥\s])[A-Za-z]{4,}\s*", "", full)
    # 中文之间的半角逗号（"他说这话时,底下"）
    full = re.sub(r"(?<=[一-龥]),(?=[一-龥])", "，", full)
    _unified = []
    _prev_no = 0
    _n_head = 0
    _n_fix = 0
    for _ln in full.splitlines():
        _s = _ln.strip()
        if _SEC_HEAD.match(_s):
            _cn = re.search(r"第\s*([0-9一二三四五六七八九十百零两]+)\s*[章节]", _s)
            _num = re.search(r"\d{1,2}", _s)
            _no = _cn_num_to_int(_cn.group(1)) if _cn else (int(_num.group(0)) if _num else None)
            if _no is not None:
                _emit = None
                if _no == _prev_no + 1:
                    _emit = _no
                elif max_sections is None or _no <= max_sections:
                    _emit = _prev_no + 1  # 跳号 / 重复 / 回退 → 顺延（结构不断，编号唯一）
                    _n_fix += 1
                # 否则（编号远超本篇节数）判为"数字开头的叙述行"，落到下面原样保留
                if _emit is not None:
                    _prev_no = _emit
                    _n_head += 1
                    if keep_titles:
                        _mt = _SEC_HEAD_TITLE.match(_s)
                        _ti = (_mt.group("title").strip() if _mt else "")
                        _unified.append(f"{_emit:02d} {_ti}".strip())
                    else:
                        _unified.append(f"{_emit:02d}")
                    continue
        _unified.append(_ln)
    if _n_fix > 3:
        print(f"[warn] 成稿清理：{_n_fix} 个节头编号跳号/重复，已顺延为连续编号")
    full = "\n".join(_unified)
    # 相邻节边界重复句：扩写越界把下一节首句提前写一遍（如"贺面起手就是本命飞剑"），
    # 确定性删除上一节末尾的重复句（保留下一节首句作为动作起点）
    full = _dedup_section_boundaries(full)
    return full


def _dedup_section_boundaries(full: str) -> str:
    """删除相邻小节边界上的重复句（扩写越界导致同一句在两节都出现）。

    上一节末句与下一节首句相同（或高度相似）→ 删上一节末句（下一节需要它
    作为动作起点）。只处理边界单句，不改正文其他内容。
    """
    secs = _split_full_sections(full)
    if len(secs) < 2:
        return full

    def _body_of(t):
        bl = t["text"].splitlines()
        if bl and _SEC_HEAD.match(bl[0].strip()):
            bl = bl[1:]
        return "\n".join(bl).strip()

    def _similar(a: str, b: str) -> bool:
        a, b = a.strip(), b.strip()
        if not a or not b:
            return False
        if a == b:
            return True
        # 字符集合重合度（去标点/空白）
        ca = set(re.sub(r"[\s，。！？、「」“”'\"]", "", a))
        cb = set(re.sub(r"[\s，。！？、「」“”'\"]", "", b))
        if ca and cb and len(ca & cb) / len(ca | cb) > 0.8 and abs(len(a) - len(b)) < 6:
            return True
        return False

    bodies = [_body_of(s) for s in secs]
    for i in range(len(bodies) - 1):
        cur_sents = [s for s in re.split(r"(?<=[。！？])", bodies[i]) if s.strip()]
        nxt_sents = [s for s in re.split(r"(?<=[。！？])", bodies[i + 1]) if s.strip()]
        if cur_sents and nxt_sents and _similar(cur_sents[-1], nxt_sents[0]):
            cur_sents = cur_sents[:-1]
            bodies[i] = "".join(cur_sents)
    for s, b in zip(secs, bodies):
        # 保留原节头行（裸「NN」或「NN 标题」两种形态都可能）——旧实现固定重建成
        # 裸编号，会把番茄排版稿需要的标题抹掉（缺陷：keep_titles 在边界去重时失效）。
        _hl = s["text"].splitlines()
        _head = _hl[0].strip() if _hl else f"{s['no']:02d}"
        s["text"] = f"{_head}\n\n{b}".strip()
    return "\n\n".join(s["text"] for s in secs)



def _clean_direct_raw(raw: str) -> str:
    """清洗写手输出的非正文残留：开头的"我将为您创作…"元说明、孤立的 --- 分隔线。

    这些是模型把"我要开始写了"的话写进正文，不属故事内容；标题行（## 标题）不在此删，
    由后续标题提取/剥离处理。
    """
    raw = _strip_chapter_summary(raw)  # 先删 [本章摘要] 段（可跨多行）
    out: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        if set(s) <= {"-", "*", "_"} and len(s) >= 3:  # 孤立分隔线 --- / *** / ___
            continue
        if s in ("导语：", "导语:", "导语", "【导语】", "正文：", "正文:", "【正文】"):  # 孤立的"导语：/正文："标签
            continue
        if _META_PREFACE.search(s) and len(s) <= 60:  # 元说明开场白（整行、较短）
            continue
        if _META_STRUCTURE.search(s):  # 导语过渡/结尾结构总结（可较长，整行删除）
            continue
        out.append(ln)
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out)


def _unwrap_md_emphasis(s: str) -> str:
    """剥掉行首尾成对的 markdown 强调符（**...** / *...* / __...__ / _..._）。

    写手常把标题写成 `**## 标题**`，不剥掉会导致标题提取漏判。
    """
    s = s.strip()
    for _ in range(3):
        if len(s) >= 5 and s.startswith("**") and s.endswith("**"):
            s = s[2:-2].strip()
        elif len(s) >= 3 and s.startswith("*") and s.endswith("*"):
            s = s[1:-1].strip()
        elif len(s) >= 5 and s.startswith("__") and s.endswith("__"):
            s = s[2:-2].strip()
        elif len(s) >= 3 and s.startswith("_") and s.endswith("_"):
            s = s[1:-1].strip()
        else:
            break
    return s


def _strip_direct_title_line(raw: str, title: str) -> str:
    """删除正文中与提取标题相同的 `# 标题` / `## 标题` 行（成稿头部统一写标题）。"""
    lines = raw.splitlines()
    kept: list[str] = []
    removed = False
    for ln in lines:
        s = _unwrap_md_emphasis(ln)
        if not removed:
            t = None
            if s.startswith(("# ", "## ")):
                t = s.lstrip("#").strip().strip("《》\"'「」").strip()
            elif s.strip().startswith(("标题：", "标题:", "书名：", "书名:", "短标题：", "短标题:")):
                t = _clean_title_prefix(s.strip())
            elif s.strip() and "。" not in s and len(s.strip()) <= 20:
                # 裸标题行（无 #，导语之前的短行）：与提取标题一致即删
                if s.strip() == title or s.strip() in title or title in s.strip():
                    t = s.strip()
            if t and (t == title or title in t or t in title):
                removed = True
                continue
        kept.append(ln)
    if removed:
        # 去掉剥离标题后开头的多余空行
        while kept and not kept[0].strip():
            kept.pop(0)
        return "\n".join(kept)
    return raw


def _clean_title_prefix(t: str) -> str:
    """剥掉模型输出的标题引导前缀（如「短标题：」「书名：」「标题：」）。"""
    for p in ("短标题：", "书名：", "标题：", "短标题:", "书名:", "标题:", "短标题", "书名"):
        if t.startswith(p):
            t = t[len(p):].strip()
            break
    return t.strip().strip("《》\"'「」").strip()


def _extract_direct_title(raw: str, idea: str) -> str:
    """从写手成稿提取短标题：## 标题 > 首行 # 标题 > 梗截断兜底。"""
    normed = [_unwrap_md_emphasis(line) for line in raw.splitlines()]
    # 1) 找「## 」二级标题（写手自拟短标题，优先）
    for line in normed[:40]:
        if line.startswith("## "):
            t = _clean_title_prefix(line[3:].strip())
            if 2 <= len(t) <= 30:
                return t
    # 2) 找首行「# 」一级标题
    for line in normed[:10]:
        if line.startswith("# "):
            t = _clean_title_prefix(line[2:].strip())
            # H1 若是梗原文的截断（以"……上/里/给"等收尾、且与梗高度重合），不当标题
            if 2 <= len(t) <= 30 and not (t in idea or idea.startswith(t)):
                return t
    # 2.5) 裸「标题：XXX」（无 #，模型常用"标题：书名"格式，可能被强调符包裹）
    for line in normed[:12]:
        stripped = line.strip()
        t = _clean_title_prefix(stripped)
        if t != stripped and 2 <= len(t) <= 30:  # 成功剥掉"标题：/书名："前缀
            return t
    # 2.6) 裸标题行：导语/首节之前的一行短文本（模型常把标题写成无 # 的裸行，如"献祭那天，我把仙骨喂了魔尊"）
    for line in normed[:12]:
        s = line.strip()
        if not s:
            continue
        if s.startswith(("导语", "正文")) or _SEC_HEAD.match(s) or s.startswith("#"):
            break
        if 4 <= len(s) <= 20 and "。" not in s and not _META_PREFACE.search(s):
            return s
    # 3) 兜底：从梗里取一个语义完整的短语（不硬切把词切断）
    return _fallback_title(idea)


def _fallback_title(idea: str) -> str:
    """梗兜底标题：在 12-18 字区间找一个实体名词收尾点，避免硬切把"少主"切成"少"。"""
    s = re.sub(r"[，。！？、；：\s]+", " ", idea.strip()).replace("——", " ")
    if not s:
        return "短篇"
    head = s[:20]
    # 自然收尾：实体名词（少主/魔尊/仙骨/皇后/太子/王爷…）或动作完成态；不含第一人称"我"
    good_end = set("少主魔骨后子人王女妃相爷姐师尊帝妾臣")
    for i in range(min(18, len(head)), 11, -1):
        if head[i - 1] in good_end:
            return head[:i]
    return head[:16] if len(head) >= 16 else head


def _cut_at_punct(s: str, limit: int) -> str:
    """截断到 limit 内的最后一个标点（不在词中间硬切，产生残缺）。"""
    if len(s) <= limit:
        return s
    head = s[:limit]
    for i in range(len(head) - 1, -1, -1):
        if head[i] in "，、；：":
            return head[:i]
    return head


def _extract_logline(raw: str, idea: str) -> str:
    """从 direct 初稿导语提取一句话故事（logline）；无导语退回梗。
    优先选含核心反转动作、本身完整、非承接词开头的句子；不在词中间截断。"""
    lead_lines: list[str] = []
    for ln in raw.splitlines():
        if _SEC_HEAD.match(ln.strip()):
            break
        lead_lines.append(ln)
    lead = "\n".join(lead_lines)
    # 先按换行断，再按句号断——避免无句号的标题行与导语第一句拼成一句
    sents = []
    for chunk in re.split(r"\n", lead):
        for s in re.split(r"(?<=[。！？])", chunk):
            s = re.sub(r"\s+", "", s).strip().strip("」「』『\"'")
            if s and len(s) >= 10:
                sents.append(s)
    if not sents:
        return _cut_at_punct(idea, 40)
    # 承接词开头的句子单独看不通顺（"更不知道…"承接前句"他们不知道"），降权
    _CONNECT = ("更", "也", "还", "又", "但")
    _ACT = (r"(架在.{0,8}(?:喉咙|脖子|颈)|刀转|剑转|转身|转头|反手|当着.{0,6}面|"
             r"抽(走|出|了)|拜了|跪了?下|反杀|一剑|一掌|一刀)")
    # 优先：含核心反转动作、长度适中、非承接词开头
    scored = [s for s in sents
              if re.search(_ACT, s) and 12 <= len(s) <= 45 and not s.startswith(_CONNECT)]
    if scored:
        return min(scored, key=len)  # 最凝练的反转句
    # 次选：非承接词开头、长度适中
    cand = [s for s in sents if not s.startswith(_CONNECT) and len(s) <= 45]
    if cand:
        return max(cand, key=len)
    return _cut_at_punct(max(sents, key=len), 40)


def _style_sample_of(cfg: dict, project) -> str:
    """polish/字数门兜底共用的风格范文（同方向 KB 真实原文，缺省给占位文案）。"""
    try:
        from . import kb as _kb
        d = (project.load_json("concept.json") or {}).get("direction", "")
        return _kb.style_samples(cfg, direction=d, n=2, max_chars=500) or STYLE_SAMPLE_EMPTY
    except Exception:  # noqa: BLE001
        return STYLE_SAMPLE_EMPTY


def _lang_kbcard(cfg: dict, direction: str, budget: int = 900) -> str:
    """语言打磨专用的题材知识卡摘要。

    为什么语言打磨也要吃知识卡：用户抱怨"细节描写详略不当"，而"该详的地方写足"
    需要**具体可写的细节**（真实钱数、真实流程、真实物件），没有知识卡时模型只能
    堆形容词。卡里只取"真实细节"和"可写场景"两段，钱数/行情最能撑住详写；
    篇幅压到 900 字，避免把整节改写变成知识复述。
    """
    try:
        from . import knowledge as _kn

        card = _kn.load_card(direction) if direction else None
        if not card:
            return "（无）"
        trimmed = {
            "summary": card.get("summary", ""),
            "facts": list(card.get("facts") or [])[:8],
            "scenes": list(card.get("scenes") or [])[:5],
            "dialogues": list(card.get("dialogues") or [])[:4],
            "pitfalls": list(card.get("pitfalls") or [])[:4],
            "redlines": list(card.get("redlines") or [])[:3],
        }
        return _kn.digest(trimmed, budget=budget)
    except Exception:  # noqa: BLE001 —— 知识卡缺失不该阻断语言打磨
        return "（无）"


def stage_write_direct(cfg: dict, llm: BaseLLM, project: Project) -> None:
    """直接写模式：跳过提梗/大纲/圣经，写手从idea从头写到尾。

    写手自己决定分几节、每节写什么，一次性输出完整故事。
    写完后由 stage_polish 整稿终审 + 读者团做总验收。
    """
    concept = project.load_json("concept.json") or {}
    idea = concept.get("idea", "") or concept.get("core_trope", "") or project.root.name
    direction = concept.get("direction", "")
    total_words = concept.get("words", 9000)

    writer_llm = resolve_llm(cfg, llm, "writer")
    writer = agents.WriterAgent(writer_llm)

    # 加载题材指导
    genre_guide = load_genre_guide(cfg, direction)

    # 融合检索：知识库三路（BM25+向量+共现）→ 记忆库 → 素材库逐级兜底，
    # 让 direct 写手也能利用同题材爆款钩子/金句/技法，否则 kb/material/memory 检索功能在 direct 主流程里孤立。
    kb_recall_block = ""
    try:
        from . import kb as _kb, memory as _mem, material as _mat

        recall_q = " ".join([str(idea), str(direction)]).strip()
        kb_recall_block, _n = _kb.recall(cfg, recall_q, direction=direction, max_chars=1100)
        if not kb_recall_block:
            kb_recall_block, _n = _mem.recall(cfg, recall_q, direction=direction, max_chars=900)
        if not kb_recall_block:
            kb_recall_block = _mat.digest(cfg, direction=direction, n=8, max_chars=800)
        if kb_recall_block:
            print(f"[stage] 直接写注入同题材检索 {len(kb_recall_block)} 字")
    except Exception as e:  # noqa: BLE001
        print(f"[stage] 检索注入失败（{type(e).__name__}），按题材指导写作")
        kb_recall_block = ""

    # 风格范文：同题材真实原文，让写手直接模仿语感（比抽象禁令有效）
    style_sample = ""
    try:
        from . import kb as _kb0
        style_sample = _kb0.style_samples(cfg, direction=direction, n=2, max_chars=500)
    except Exception:  # noqa: BLE001
        style_sample = ""

    # 分节计划：写手先自己拆成一节节（每节 goal/end_hook），再据此一次写。
    # 计划保证"梗的设定分节落地、节与节首尾相接、写满末节不提前收尾"，比让模型临场决定更稳。
    n_sections = max(10, int(total_words) // 800)
    plan_text = ""
    plan_sections = []
    try:
        psys, puser = render_prompt(
            "section_plan", IDEA=idea, DIRECTION=direction, WORDS=total_words,
            N=n_sections, GENRE_GUIDE=genre_guide, BAN_PROPS=_ban_props_text(cfg))
        plan = writer_llm.chat_json(psys, puser)
        plan_sections = plan.get("sections", []) or []
        if plan_sections:
            plan_text = json.dumps(plan, ensure_ascii=False, indent=1)
            print(f"[stage] 分节计划：{len(plan_sections)} 节")
    except Exception as e:  # noqa: BLE001
        print(f"[stage] 分节计划生成失败（{type(e).__name__}），按题材指导直接写")

    system, user = render_prompt(
        "direct_write",
        IDEA=idea,
        DIRECTION=direction,
        WORDS=total_words,
        GENRE_GUIDE=genre_guide,
        PLAN=plan_text or "（计划生成失败，按题材指导自己分节写满）",
        STYLE_SAMPLE=style_sample or STYLE_SAMPLE_EMPTY,
        KB_RECALL=kb_recall_block or "（暂无相关素材，按题材指导写作）",
        BAN_PROPS=_ban_props_text(cfg),
    )

    print(f"[stage] 直接写模式：写手从头写到尾（{total_words}字）…")
    raw = writer_llm.chat(system, user)
    raw = _clean_direct_raw(raw)  # 清洗元说明/分隔线残留
    # 初稿字数兜底：低于目标 50% 视为输出被截断/模型提前收尾，降级为非流式重试一次。
    # （流式 finish_reason 对部分网关不可靠，靠字数校验兜住残缺稿）
    raw_w = count_words_zh(raw)
    if raw_w < total_words * 0.5:
        print(f"[warn] 直接写初稿仅 {raw_w} 字（<目标50%），疑似输出截断，降级非流式重试一次…")
        stream_backup = getattr(writer_llm, "stream", None)
        if stream_backup is not None:
            writer_llm.stream = False
        try:
            raw2 = writer_llm.chat(system, user)
            raw_w2 = count_words_zh(raw2)
            if raw_w2 > raw_w:
                raw = raw2
                raw_w = raw_w2
                print(f"[warn] 重试后初稿 {raw_w} 字（{raw_w2}），采用更完整版本")
        finally:
            if stream_backup is not None:
                writer_llm.stream = stream_backup

    # 梗即契约确定性校验：从梗里提取核心设定关键词（数字/时间/人物关系/关键道具/反常规则），
    # 正文缺失任一即打回重写一次（模型自觉不可靠，用硬校验兜底）。
    # 判定标准：数字类核心词（含数字的项）至少命中 60%（防跑题），
    # 动作短语（如"他欠我二十年"）允许表达变体，仅作参考不强制——目标是防"完全跑题"，非逐字校对。
    _idea_kws = _extract_idea_core_kws(idea)
    if _idea_kws:
        missing = [k for k in _idea_kws if k not in raw]
        _num_kws = [k for k in _idea_kws if any(c.isdigit() or c in "一二三四五六七八九十百千万两" for c in k)]
        _num_missing = [k for k in _num_kws if k not in raw]
        # 打回条件：数字核心词缺失过半，或（无数字词时）全部核心短语缺失
        _need_rewrite = (len(_num_kws) >= 2 and len(_num_missing) >= max(1, len(_num_kws) // 2)) or \
                        (len(_num_kws) < 2 and missing and len(missing) >= 2)
        if _need_rewrite:
            print(f"[guard] 梗核心设定缺失 {missing}（数字核心缺 {_num_missing}），打回重写一次（梗即契约）…")
            runlog.event("direct_write", f"梗核心缺失{missing}，打回重写")
            sys2 = system + "\n\n【梗即契约强制补全】当前初稿缺失以下故事梗核心设定：\n" + "、".join(missing) + "\n必须在正文中出现并成为情节引擎（时间/关系/矛盾/道具/规则逐条落实），其余设定保持。**字数铁律：重写后全稿字数不得低于原稿的 90%，补设定时必须同时补足内容，严禁越改越短。**"
            raw2 = writer_llm.chat(sys2, user)
            if count_words_zh(raw2) >= count_words_zh(raw) * 0.9:
                raw = raw2
                raw_w2 = count_words_zh(raw)
                print(f"[guard] 重写后 {raw_w2} 字，命中检查通过")
            else:
                print("[guard] 重写缩水，采用原稿")
            missing2 = [k for k in _idea_kws if k not in raw]
            if missing2:
                print(f"[warn] 重写后仍缺梗核心 {missing2}，继续（梗核心不足将拉低终审分）")

    # 账务词族密度拦截（direct 模式无大纲阶段，补上 _trope_reuse_check 的缺口）：
    # 梗本身是"欠钱/还债"时允许欠条欠款出现，但全篇账务词族 ≥4 即判定又钻"记账式记仇"套路。
    _acc = _trope_reuse_check(raw)
    if _acc:
        print(f"[guard] 初稿账务词族密度偏高（{_acc}），打回重写（最多2次，换非账本道具）…")
        runlog.event("direct_write", f"账务词族偏高{_acc}，打回重写")
        acc2 = _acc
        _base_w = count_words_zh(raw)
        for _rw in range(2):
            sys2 = system + f"\n\n【硬性约束】当前初稿被判定又钻'记账/算账/买卖'套路（命中：{acc2}）。非生意主线禁止账本、记账、算账、账册、欠条、收据、铺子、地契、利息、本金等账务道具出现；即使题材涉及欠债，也必须换成非账本载体（如人情债、旧照片、信物、遗物），把'记仇'写成具体行为与情感，不写成账目流水。**字数铁律：重写后全稿字数不得低于原稿的 90%（当前 {_base_w} 字），删账务词必须同时补等价内容（加对话/动作/具体物件），严禁越改越短。**重写全文，删净上述词族。"
            raw2 = writer_llm.chat(sys2, user)
            if count_words_zh(raw2) >= count_words_zh(raw) * 0.9:
                raw = raw2
            acc2 = _trope_reuse_check(raw)
            print(f"[guard] 账务词族第{_rw + 1}次重写：{count_words_zh(raw)} 字（原 {_base_w}），{'已达标' if not acc2 else '仍偏高 ' + str(acc2)}")
            if not acc2:
                break
        if acc2:
            print(f"[warn] 两次重写后账务词族仍偏高（{acc2}），放行（终审将提示）")

    # 标题提取：优先取写手自拟的「## 标题」，否则从正文首行「# 」提取，兜底用梗截断。
    # 禁止拿整个梗当标题（信息流里看不出重点）。
    title = _extract_direct_title(raw, idea)
    # 剥离正文里的标题行（成稿头部会统一写 `# 标题`，避免重复）
    raw = _strip_direct_title_line(raw, title)
    # 确定性分节归并：写手分节不稳定（会写28个短节），合并到 10-13 节
    raw = _normalize_sections(raw)
    # 末节强制收束：一次写高潮后常预告新危机/停在悬念，单独重写末节为大女主爽感收束
    try:
        raw = _force_ending_rewrite(
            cfg, llm, project, raw,
            style_sample or STYLE_SAMPLE_EMPTY,
            plan_sections=plan_sections)
    except Exception as e:  # noqa: BLE001
        print(f"[stage] 末节收束异常（{type(e).__name__}），继续")

    # 创建最小 pitch.json（stage_polish 需要）
    pitch = {
        "title": title,
        "logline": _extract_logline(raw, idea),
        "chapter_count": 1,
        "direction": direction,
    }
    project.save_json("pitch.json", pitch)
    project.save_json("bible.json", {"characters": [], "world": "", "rules": []})

    # 保存成稿
    _drafts.commit_chapter(project, 1, raw, gate="direct")
    project.save_json("direct_draft.json", {"raw_length": len(raw), "direction": direction, "title": title})
    # 持久化产出模式：续跑/看板/复盘都靠它判断"这篇有没有逐章大纲"，
    # 而不是靠 outline.json 是否存在——旧实现续跑会给 direct 稿补生成大纲，
    # 之后 stage_polish 的 direct 分支失效、成稿版式被重排（缺陷 P1-13）。
    _meta = project.meta
    _meta["mode"] = "direct"
    project.save_meta(_meta)
    project.set_stage("chapters", "直接写完成")
    print(f"    → 直接写完成 {count_words_zh(raw)} 字 | 标题《{title}》")


@runlog.staged("逐章扩写与门禁")
def stage_write(cfg: dict, llm: BaseLLM, project: Project, only: int | None = None) -> None:
    outline = project.load_json("outline.json")
    if not outline:
        raise SystemExit("缺少大纲，无法扩写")
    meta = project.meta
    state = meta.setdefault("chapters_state", {"summaries": {}, "tails": {}})
    chs = outline["chapters"]

    writer_llm = resolve_llm(cfg, llm, "writer")
    editor_llm = resolve_llm(cfg, llm, "reviewer")
    panel_llm = resolve_llm(cfg, llm, "panel")
    writer = agents.WriterAgent(writer_llm)
    editor = agents.ChiefEditor(editor_llm)
    panel = None
    if cfg["review"]["enabled"] and cfg["review"].get("reader_enabled", True):
        direction = (project.load_json("concept.json") or {}).get("direction", "")
        panel = agents.ReaderPanel(panel_llm, direction, int(cfg["review"].get("reader_panel", 3)))

    todo = [c["no"] for c in chs if not project.chapter_file(c["no"]).exists()]
    if only:
        todo = [only]
    if not todo:
        print("[stage] 各章均已扩写，跳过")
        return
    review_enabled = cfg["review"]["enabled"]

    # ---- hybrid 模式：单 writer 同一上下文从头到尾续写，不做逐章门禁 ----
    # 文风统一靠上下文不重置（模型记住自己前面怎么写）；写完后由 stage_polish
    # 整稿终审 + 全稿 langqa + 读者团做一次总验收。
    if (cfg.get("pipeline") or {}).get("mode") == "hybrid":
        _stage_write_hybrid(cfg, llm, project, writer_llm, writer, todo, chs, state, meta)
        project.set_stage("chapters", f"{len(chs)}章完成(hybrid)")
        return

    # ---- 阶段一：串行写稿（章节间有衔接依赖：上一章摘要/结尾注入写作上下文，不可并行）----
    drafts: dict[int, tuple[dict, str, dict]] = {}   # no -> (ctx, 初稿正文, core 报告)
    for no in todo:
        ch = chs[no - 1]
        ctx = _chapter_ctx(cfg, project, no, state)
        if isinstance(writer_llm, MockLLM):
            writer_llm.target_words = int(ch.get("words_target", 2000))
        print(f"[stage] 撰写小节 {no:02d}/{len(chs)}：{ch.get('title')} …")
        if review_enabled:
            text, rep_core = agents._chapter_core(cfg, writer, ctx, no, project=project)
            drafts[no] = (ctx, text, rep_core)
            # 先用初稿更新衔接状态（供下一章写作）；终稿摘要/结尾在阶段三落盘时覆盖
            body0, summ0 = agents.split_summary(text)
            state["summaries"][str(no)] = summ0
            state["tails"][str(no)] = _tail(body0, 300)
            project.save_meta(meta)
        else:
            raw = writer.write(ctx)
            body, summary = agents.split_summary(raw)
            meta.setdefault("reviews", {})[str(no)] = {"gate": "review_disabled"}
            print(f"    → {count_words_zh(body)} 字")
            _drafts.commit_chapter(project, no, body, gate="review_disabled")
            state["summaries"][str(no)] = summary
            state["tails"][str(no)] = _tail(body, 300)
            project.save_meta(meta)

    if not review_enabled or not drafts:
        project.set_stage("chapters", f"{len(chs)}章完成")
        return

    # ---- 阶段二：主编审查 + 读者团评分 并行（章间无依赖；写作质量已由初稿状态保证衔接）----
    p_cfg = cfg.get("pipeline") or {}
    max_par = max(1, int(p_cfg.get("max_parallel", 3) or 3))
    workers = min(max_par, len(drafts))
    print(f"[stage] 门禁并行（review+reader，{workers} 线程，阈值 {cfg['review'].get('score_threshold')}）…")

    def _gate_one(no: int) -> tuple[int, str, dict]:
        ctx, text, _ = drafts[no]
        ft, rep = agents._gate_review_reader(cfg, writer, editor, panel, ctx, text, no,
                                             project=project)
        return no, ft, rep

    gated: dict[int, tuple[str, dict]] = {}
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_gate_one, no): no for no in sorted(drafts)}
            for fut in as_completed(futs):
                no, ft, rep = fut.result()
                gated[no] = (ft, rep)
    else:
        for no in sorted(drafts):
            _, ft, rep = _gate_one(no)
            gated[no] = (ft, rep)

    # ---- 阶段三：情绪量化门串行收尾（依赖上一章 dominant，保持串行）+ 落盘 ----
    for no in sorted(drafts):
        ctx, _, rep_core = drafts[no]
        ft, rep_mid = gated[no]
        prev_emo = state.setdefault("emotions", {}).get(str(no - 1), "")
        ft, rep_emo = agents._gate_emotion(cfg, editor, writer, ctx, ft, prev_emo, no,
                                           project=project)
        rep = {**rep_core, **rep_mid, **rep_emo}
        # 最终门禁状态：情绪/审查/读者/语言质检/禁道具 任一门打满都要反映出来。
        # 旧代码只看 rep_emo+rep_mid，rep_core 的 maxed_langqa/maxed_banprops
        # 会被 review 的 pass 合并覆盖——langqa 打满在最终状态里不可见（漏报）
        bad = [g for g in (rep_emo.get("gate"), rep_mid.get("gate"), rep_core.get("gate"))
               if g and g != "pass"]
        rep["gate"] = bad[0] if bad else "pass"
        if len(bad) > 1:
            rep["maxed_all"] = bad
        body, summary = agents.split_summary(ft)
        # ---- 终稿确定性门禁串联复查（思考泄漏 + 禁用道具 + 语言质检）----
        # 缺陷背景：三道确定性门只在初稿后跑一次，之后主编/读者/情绪每一轮都是 LLM
        # 重写，会把清掉的道具与思考泄漏原样写回来而无人复查（实测 ch08 终稿出现
        # 「欠条」而 ch08.json 记 ban_hits=0；另一次 8 章混入 912 行英文思维链）。
        # 这里在落盘前用 agents.deterministic_recheck 重跑全套并串联修复。
        ft_fixed, _rc = agents.deterministic_recheck(cfg, writer, ctx, body)
        if not _rc["pass"]:
            rep["deterministic_recheck"] = _rc
            if _rc["hits"].get("banned"):
                rep["ban_hits"] = _rc["hits"]["banned"]
            if rep["gate"] == "pass":
                rep["gate"] = "maxed_recheck"
            print(f"    [recheck] ⚠ 终稿仍有未清项：{list(_rc['hits'])}（已记录）")
        elif _rc["rounds"] > 1:
            print(f"    [recheck] 终稿复查修复完成（{_rc['rounds'] - 1} 轮重写）")
        body, _s2 = agents.split_summary(ft_fixed)
        if _s2:
            summary = _s2   # 复查重写带回新摘要则覆盖；没带则沿用主编门时的摘要
        # ---- 证据化评审卡（借鉴 openwrite B07）----
        # 为什么要在这里算而不是在主编里算：评分卡要覆盖"终稿"——上面 deterministic_recheck
        # 可能已经重写过了，用重写前的 verdict/正文去算会得出与落盘稿不一致的结论。
        # 也正因为放在最后，字数项与安全红线取的是真实落盘内容，不会被中间轮次糊弄。
        #
        # 但"放在最后算"只解决了字数/安全两项：主编 verdict 里的 criteria/evidence 仍
        # 是针对它审过的那一版。读者团重写、情绪门重写、终稿复查重写任意一次发生，
        # 证据就整体失效（实测 42 张卡里 24 张 criteria_stale，8 条引文在终稿里 0 条可
        # 核验）——评分卡于是退化成"对一份不存在的稿子的评分"。这里在检测到版本漂移
        # 时用终稿重审一次（只 1 轮、只重算证据，不再触发重写），让评分卡真正对应交付稿。
        rubric = None
        verdict_for_rubric = rep.get("review_verdict") or {}
        try:
            if _should_refresh_criteria(cfg, verdict_for_rubric, body, review_enabled):
                refreshed, refresh_rep = _refresh_review_verdict(
                    cfg, editor, ctx, body, verdict_for_rubric, no)
                if refreshed:
                    verdict_for_rubric = refreshed
                    rep["review_verdict"] = refreshed
                    rep["rubric_refresh"] = refresh_rep
            rubric = review_mod.build(
                cfg, verdict_for_rubric, body,
                target_words=int(ctx.get("WORDS", 0) or 0),
                direction=str(ctx.get("DIRECTION") or ""))
            rep["rubric"] = rubric
            rep["target_words"] = int(ctx.get("WORDS", 0) or 0)
            if rubric.get("gate_status") == "blocked" and rep["gate"] == "pass":
                rep["gate"] = "blocked_rubric"
            print("    " + review_mod.render_text(rubric).splitlines()[0])
        except Exception as e:  # noqa: BLE001
            # 评审卡是"观测层"，坏掉不该毁掉已经产出的正文；但要留痕，不许静默。
            rep["rubric_error"] = f"{type(e).__name__}: {e}"[:200]
            print(f"    [rubric] ⚠ 评审卡生成失败（已记录，不影响落盘）：{e}")
        project.save_json(f"ch{no:02d}.json", rep, subdir="reviews")
        _drafts.commit_chapter(project, no, body, gate=rep.get("gate", ""))
        emo = rep.get("emotion") or {}
        state.setdefault("emotions", {})[str(no)] = emo.get("dominant") or ""
        meta.setdefault("reviews", {})[str(no)] = {
            k: rep.get(k)
            for k in ("gate", "langqa_pass", "review_rounds", "review_score",
                      "reader_rounds", "reader_avg", "emotion")
        }
        # 评审卡摘要进 meta：看板列表/汇总要能按「质量分 / 覆盖率 / 交付状态」筛选，
        # 只把完整 rubric 留在 chNN.json 里会让列表页每行都要多读一个文件。
        if rubric:
            meta["reviews"][str(no)].update(review_mod.summary_fields(rubric))
        state["summaries"][str(no)] = summary          # 终稿摘要覆盖初稿
        state["tails"][str(no)] = _tail(body, 300)
        project.save_meta(meta)
        print(f"    → 小节{no:02d} {count_words_zh(body)} 字（门禁：{rep['gate']}）")
        # 跨章重复检测（警告级）：本章终稿摘要 vs 更早章节（隔一章起），判别器毫秒级筛查。
        # 只标记不改判——续写剧情线本就会延续，误报交给复盘人肉判断
        if cfg.get("laya", {}).get("enabled", True) and not isinstance(writer_llm, MockLLM):
            try:
                prev_sums = [f"第{k}章：{v}" for k, v in state["summaries"].items()
                             if k.isdigit() and int(k) < no - 1]
                prev_sums = sorted(prev_sums)[-6:]
                if prev_sums:
                    from . import laya_judge

                    lc = (cfg.get("laya") or {})
                    rep_flags = laya_judge.dup_flags(
                        [summary], prev_sums,
                        backend=lc.get("backend", "self"),
                        self_url=lc.get("self_url", "http://127.0.0.1:8152"),
                        threshold_dup=float(lc.get("threshold_dup", 0.60)),
                        threshold_new=float(lc.get("threshold_new", 0.80)),
                        api_url=lc.get("api_url", "") or "",
                        api_key=lc.get("api_key", "") or "",
                        api_model=lc.get("api_model", "") or "")
                    if rep_flags and rep_flags[0] is True:
                        meta["reviews"][str(no)]["repeat_suspect"] = True
                        # 立即回写：旧实现只在下一章 save_meta 时顺带落盘，
                        # 最后一章的标记必然丢失（缺陷 P1-8）。
                        project.save_meta(meta)
                        print(f"[repcheck] ⚠️ 第{no:02d}章情节与更早章节高度相似（已标记，建议复盘时关注）")
            except Exception:
                pass
    project.set_stage("chapters", f"{len(chs)}章完成")


def _tail(text: str, n: int) -> str:
    body = text.strip()
    return body[-n:]


# ---------------------------------------------------------------------------
# Stage 8：数据回流分析（发布后阅读数据 → 改进教训）
# ---------------------------------------------------------------------------

@runlog.staged("数据复盘")
def stage_improve(cfg: dict, llm: BaseLLM, project: Project) -> dict:
    """对比发布数据与门禁预测，生成改进教训并沉淀进 lessons 库（下此自动注入选题）。"""
    from . import analytics as an

    llm = resolve_llm(cfg, llm, "planner")
    name = project.root.name
    snaps = an.load_series(cfg, name)
    if not snaps:
        raise SystemExit(
            f"项目 {name} 没有阅读数据快照。先执行：studio analytics fetch --project {name}"
        )
    trend = an.compute_trend(snaps)

    pitch = project.load_json("pitch.json") or {}
    concept = project.load_json("concept.json") or {}
    report = project.load_json("report.json") or {}
    full = (report.get("review") or {}).get("full_story") or {}
    reviews = project.meta.get("reviews", {})
    review_scores = [v.get("review_score") for v in reviews.values() if isinstance(v, dict) and v.get("review_score")]
    emo_scores = [
        v.get("emotion", {}).get("emotion_score")
        for v in reviews.values()
        if isinstance(v, dict) and isinstance(v.get("emotion"), dict) and v["emotion"].get("emotion_score")
    ]
    hook_scores = [
        v.get("emotion", {}).get("hook_score")
        for v in reviews.values()
        if isinstance(v, dict) and isinstance(v.get("emotion"), dict) and v["emotion"].get("hook_score")
    ]

    sys_p, user_p = render_prompt(
        "improve",
        TITLE=pitch.get("title", ""),
        DIRECTION=concept.get("direction", ""),
        PLATFORM=pitch.get("platform", ""),
        WORDS=report.get("final_words", "?"),
        REVIEW_SCORE=round(sum(review_scores) / len(review_scores), 1) if review_scores else "—",
        READER_AVG=full.get("reader_avg", "—"),
        EMOTION_SCORE=round(sum(emo_scores) / len(emo_scores), 1) if emo_scores else "—",
        HOOK_SCORE=round(sum(hook_scores) / len(hook_scores), 1) if hook_scores else "—",
        ANALYTICS=an.summary_text(trend),
    )
    print("[stage] 分析阅读数据、生成改进教训 …")
    data = llm.chat_json(sys_p, user_p)

    # 沉淀教训库（下次 mine 选题自动注入）
    ws = Path(cfg["studio"]["workspace_abs"])
    ws.mkdir(parents=True, exist_ok=True)
    lf = ws / "feedback_lessons.json"
    try:
        entries = json.loads(lf.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        entries = []
    entries.append(
        {
            "date": now_ts(),
            "project": name,
            # 平台标签：教训库会跨项目复用，不记平台就会把番茄的教训套到知乎上
            # （两家的字数下限、节头规范、钩子密度都不同）。
            "platform": pitch.get("platform") or (snaps[-1].get("platform") if snaps else "") or "",
            "title": pitch.get("title", ""),
            "metrics_digest": an.summary_text(trend)[:400],
            "diagnosis": data.get("diagnosis", ""),
            "lessons": data.get("lessons", []),
        }
    )
    lf.write_text(json.dumps(entries[-20:], ensure_ascii=False, indent=2), encoding="utf-8")

    # 人类可读报告
    md = [
        f"# 数据复盘：《{pitch.get('title', '')}》",
        "",
        f"> 生成时间：{now_ts()} | 快照数：{trend.get('snapshots', 0)}",
        "",
        "## 数据概览",
        "```",
        an.summary_text(trend),
        "```",
        "",
        "## 诊断",
        data.get("diagnosis", ""),
        "",
        "## 改进教训",
        *[
            f"- **[{l.get('dimension', '')}]** {l.get('lesson', '')} → 下一步：{l.get('action', '')}"
            for l in data.get("lessons", [])
        ],
        "",
        f"**下一个假设**：{data.get('next_try', '')}",
        "",
        "> 以上教训已写入 feedback_lessons.json，下次 `run` 选题时会自动注入。",
    ]
    project.save_text("improvement.md", "\n".join(md), subdir="final")
    print(f"[stage] 复盘报告 → final/improvement.md；教训已入反馈库（共 {len(entries)} 条记录）")
    return data

# 小节头契约的唯一权威源在 studio/util.py（SEC_HEAD / SEC_HEAD_TITLE / MAX_SECTIONS）。
# 这里只做向后兼容别名：历史代码与测试都按 `_SEC_HEAD` / `_MAX_SECTIONS` 引用。
_SEC_HEAD = SEC_HEAD
_SEC_HEAD_TITLE = SEC_HEAD_TITLE
_MAX_SECTIONS = MAX_SECTIONS

# ---------------------------------------------------------------------------
# 阶段达标门：导语 / 大纲 / 圣经 每个环节必须达标才放行进入下一环节
# ---------------------------------------------------------------------------

_STAGE_CRITERIA = {
    "导语": (
        "1) 300-400 字（低于 280 或超过 420 视为不合格），第一句就是炸裂冲突或反常画面；\n"
        "2) 细节具体到钱数/物件/地名，禁空话套话与文艺腔；\n"
        "3) 至少一次反转或反差（如'她笑了'紧跟坏事）；\n"
        "4) 末句留悬念钩子，逼人往下读；\n"
        "5) 口语白话，无 AI 口癖（'赋能/刻画/映入眼帘'等禁用）。"
    ),
    "大纲": (
        "1) 小节结构完整：title/goal/beats(每节≥2拍)/open_hook/end_hook 全字段；\n"
        "2) 小节01 第一拍就是炸裂开头，无铺垫无回忆；\n"
        "3) 每节 end_hook 都是具体危机或信息缺口，不是抒情；\n"
        "4) 后半程有全书最大反转（付费卡点）；\n"
        "5) 节与节之间冲突层层升级，无重复打循环。"
    ),
    "圣经": (
        "1) 主角/对手至少 2 张人设卡，含姓名/年龄/职业/欲望/软肋；\n"
        "2) 伏笔表至少 1 条，标明在哪节埋、在哪节收；\n"
        "3) timeline 时间线具体（精确到日/事件），setting 落到县城乡镇真实场景；\n"
        "4) tone 有明确情绪配比与爽点节奏。"
    ),
}


def _gate_llm_check(cfg, llm, project, stage, content_text):
    """LLM 评审：返回 (score, issues_str)。评审失败视为中性通过（不阻塞流水线）。"""
    editor = resolve_llm(cfg, llm, "reviewer")
    pitch = project.load_json("pitch.json") or {}
    try:
        sys_p, user_p = render_prompt(
            "gate", STAGE=stage, TITLE=pitch.get("title", ""),
            CRITERIA=_STAGE_CRITERIA.get(stage, "按下沉市场短篇行业达标线核对。"),
            PREV_ISSUES="", CONTENT=content_text[:4000],
        )
        data = editor.chat_json(sys_p, user_p)
        if not isinstance(data, dict):
            raise ValueError(f"评审输出非 JSON 对象：{type(data).__name__}")
        score = float(data.get("score") or 0)
        issues = "；".join(str(x) for x in (data.get("issues") or [])[:4])
        return score, issues
    except Exception as e:  # noqa: BLE001
        print(f"[gate] {stage} LLM 评审失败（{type(e).__name__}），按确定性检查放行")
        return 10.0, ""


_AI_CLICHE = re.compile(r"赋能|刻画|映入眼帘|不得不说|值得一提的是|综上所述")


def _ban_issue_text(stage: str, seen: list[str], where: str = "") -> str:
    """禁用道具命中后的打回说明：早期阶段换主线，后期阶段只换词。

    为什么分口径：禁用道具表的语义是"模型别自己发明那些玩意"，不是"整篇不许出现"。
    早期（提梗/概念）这个词会决定主线，才需要重新设计；到了圣经/大纲/逐章，
    故事已经立住，再要求"整篇重新设计主线冲突"只会让模型推翻已通过的结构——
    实测故事圣经因为一句"账单"被要求重做整篇主线，白烧一轮。

    `where`：命中处的原文片段。只报词名时模型下一轮照样在同一句式里沿用原词
    （实测「账页」连挂三轮），给出上下文它才知道该替换哪一句。
    """
    who = "、".join(seen[:6])
    tail = f"。出现位置：{where}" if where else ""
    if stage in ("提梗", "选题", "概念", "核心矛盾"):
        return f"命中用户禁用道具 {who}：整篇重新设计主线冲突，不许拿它们当核心道具{tail}"
    return (f"命中用户禁用道具 {who}：不要改情节、不要改人设、不要重排结构，"
            f"只把这几处换成本场景里等价的具体说法（同一句话内替换即可）{tail}")


def _ban_contexts(ban_re, text: str, limit: int = 3, span: int = 14) -> str:
    """取禁用道具命中处的原文片段，供打回意见定位（最多 limit 处）。"""
    out = []
    for m in ban_re.finditer(text):
        a = max(0, m.start() - span)
        b = min(len(text), m.end() + span)
        frag = text[a:b].replace("\\n", " ").replace("\n", " ").strip()
        frag = frag.strip('{}"').replace('":', "").replace('"', "")
        out.append(f"「…{frag}…」")
        if len(out) >= limit:
            break
    return "；".join(out)


def _det_check_split(stage: str, content, cfg: dict | None = None
                     ) -> tuple[list[str], list[str]]:
    """确定性达标检查，拆成（结构问题、禁用道具问题）两组。

    为什么要拆：大纲阶段的禁用道具命中**不该重做整份大纲**。禁用道具表管的是
    "模型别自己发明那类道具"，而大纲里的一个词完全能在写章时被定点替换掉
    （`stage_write` 的终稿复查正是这么做的，只花一次短调用）。此前两组混在一起判
    硬检不合格，实测一次 run 里大纲被整份重做 3 轮、烧掉 270s 和 3 次 2600-token
    调用，最后仍以 `maxed_stage` 放行——花了钱没买到合规。
    """
    struct_issues: list[str] = []
    ban_issues: list[str] = []
    _ban_re = banned_regex(cfg or {})
    if _ban_re is not None:
        _txt = json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content
        _ban_seen = sorted(set(_ban_re.findall(_txt)))
        if _ban_seen:
            ban_issues.append(_ban_issue_text(stage, _ban_seen,
                                              _ban_contexts(_ban_re, _txt)))
    issues = struct_issues
    if stage == "导语":
        text = (content.get("text") or "").strip() if isinstance(content, dict) else str(content)
        w = count_words_zh(text)
        if w < 260:
            issues.append(f"导语仅 {w} 字，单薄（要求 300-400 字）")
        if w > 460:
            issues.append(f"导语 {w} 字过长，压缩到 300-400 字")
        if _AI_CLICHE.search(text):
            issues.append("导语含 AI 口癖词，改成口语白话")
        if not re.search(r"[0-9一二三四五六七八九十百千万]+|[，,]", text):
            issues.append("导语缺少具体细节（钱数/数字），太空泛")
    elif stage == "大纲":
        chs = content.get("chapters", []) if isinstance(content, dict) else []
        if len(chs) < 4:
            issues.append(f"仅 {len(chs)} 节，短篇至少 4 节")
        for c in chs:
            if not c.get("end_hook"):
                issues.append(f"小节{int(c.get('no', 0)):02d} 缺 end_hook")
            if len(c.get("beats") or []) < 2:
                issues.append(f"小节{int(c.get('no', 0)):02d} beats 不足 2 拍")
        hooks = [c.get("open_hook") for c in chs[1:]]
        if not all(hooks):
            issues.append("存在小节缺 open_hook（承接钩子）")
    elif stage == "圣经":
        chars = content.get("characters", []) if isinstance(content, dict) else []
        foresh = content.get("foreshadows", []) if isinstance(content, dict) else []
        if len(chars) < 2:
            issues.append(f"人设卡仅 {len(chars)} 张（需主角+对手至少 2 张）")
        if len(foresh) < 1:
            issues.append("伏笔表为空，至少设计 1 条伏笔（标明埋设与回收小节）")
        if isinstance(content, dict) and not (content.get("timeline") or "").strip():
            issues.append("timeline 时间线为空")
    return struct_issues, ban_issues


def _det_check(stage: str, content, cfg: dict | None = None) -> tuple[bool, str]:
    """确定性达标检查：结构完整性/字数底线/禁用道具，零 token。返回 (ok, issues)。"""
    struct_issues, ban_issues = _det_check_split(stage, content, cfg)
    issues = ban_issues + struct_issues
    return (not issues), "；".join(issues[:4])


# 这些阶段的禁用道具命中不重做整稿：语言/道具层在后面还有定点门，
# 整份重做只会把已经合格的结构推倒重来。
_BAN_CARRY_STAGES = ("大纲", "圣经", "导语")


def stage_quality_gate(cfg, llm, project, stage, gen_fn, extra_issues: str = ""):
    """阶段达标门：确定性检查 + LLM 评审双门，不达标带意见打回重做。

    gen_fn(round, issues) -> content（第 1 轮 issues 为空串，之后带打回意见重新生成）。
    extra_issues：每轮都附加的硬性打回意见（如核心道具雷同检测结论）。
    轮次用尽仍不达标：取最后一稿放行并标记 maxed_stage（运行日志留痕，不静默）。
    """
    from . import runlog

    rv = cfg["review"]
    if not rv.get("stage_gate", True) or isinstance(llm, MockLLM):
        return gen_fn(1, ""), {"gate": "skipped", "rounds": 0}
    threshold = float(rv.get("stage_gate_threshold", 8.0))
    max_r = int(rv.get("stage_gate_rounds", 2))
    issues = ""
    best, best_score = None, -1.0   # 留优：打回循环可能把稿件改劣（如删句压字数压丢内容），
    # 打满时返回「评审分最高且硬检合格」的那稿，而不是最后一稿
    for rnd in range(1, max_r + 2):
        content = gen_fn(rnd, "；".join(filter(None, [issues, extra_issues])))
        if rnd > 1:
            print(f"[gate] {stage} 第{rnd}轮重做完成")
        _struct, _bans = _det_check_split(stage, content, cfg)
        if _bans and not _struct and stage in _BAN_CARRY_STAGES:
            # 结构合格、只有禁用道具命中：不重做整稿，记账后交给章级定点门。
            # 章级 `stage_write` 的终稿复查会带着命中清单做一次短改写，成本约为
            # 整份重做大纲的 1/10，且改的是真正会提交的正文。
            _bans_list = "；".join(_bans)
            print(f"[gate] {stage} 结构合格，仅命中禁用道具 → 不在本阶段重做（转章级定点替换）："
                  f"{_bans_list[:80]}")
            runlog.event("stage_gate",
                         f"{stage}禁用道具转章级处理：{_bans_list[:80]}", level="warn")
            ok_det, det_issues = True, ""
            carry = _bans_list
        else:
            carry = ""
            ok_det, det_issues = (not (_struct + _bans)), "；".join((_bans + _struct)[:4])
        content_text = json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content
        score, llm_issues = _gate_llm_check(cfg, llm, project, stage, content_text)
        final_issues = "；".join(filter(None, [det_issues, llm_issues]))
        ok = ok_det and score >= threshold
        if ok_det and score > best_score:
            best, best_score = content, score
        if ok:
            print(f"[gate] {stage} 达标（评审 {score} 分）")
            _g = {"gate": "pass", "rounds": rnd, "score": score}
            if carry:
                _g["ban_carry"] = carry[:200]
            return content, _g
        issues = final_issues or "质量未达标，按验收标准逐条改进"
        print(f"[gate] {stage} 未达标（评审 {score} 分）：{issues[:100]}")
        runlog.event("stage_gate", f"{stage}打回重做（第{rnd}轮，评审{score}分）：{issues[:80]}", level="warn")
    print(f"[gate] {stage} 轮次用尽，按现稿放行（maxed_stage，已留运行日志）")
    if best is not None and best_score >= 0:
        print(f"[gate] {stage} 打满轮次：保留评审最优稿（{best_score} 分，硬检合格）")
        return best, {"gate": "maxed_stage", "rounds": max_r, "issues": issues[:200], "kept_score": best_score}
    return content, {"gate": "maxed_stage", "rounds": max_r, "issues": issues[:200]}


def _split_full_sections(full: str) -> list[dict]:
    """把整稿按「NN 标题」小节头切回小节列表 [{no,title,text}]。

    两层防御（对应历史事故：误切 400+ 节 → 按节号批量回写 → 成稿膨胀到 9.9 万字）：
      1) 节数 > _MAX_SECTIONS 直接判定识别失败、返回 []，让调用方退回整稿模式；
      2) 编号必须严格递增：出现重复/回退（模型重写残留旧节头）时按位置顺延，
         保证 `no` 唯一——下游 `next(s["no"] == no+1)` 与按节回写都依赖唯一编号。
    """
    lines = full.splitlines()
    heads = [i for i, ln in enumerate(lines) if _SEC_HEAD.match(ln.strip())]
    if not heads:
        return []
    if len(heads) > _MAX_SECTIONS:
        print(f"[guard] 小节头识别异常（{len(heads)} 个 > 上限 {_MAX_SECTIONS}），"
              "判定正文被误切，本轮跳过逐节处理")
        return []
    secs = []
    prev = 0
    for k, h in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        head_line = lines[h].strip()
        # 节号解析只走 util.sec_head_no（单一权威源）；此前这里自带一份正则，
        # 与 SEC_HEAD 判据不一致，导致「3月18号，周一…」这类日期开头正文被误判成节头。
        _no = sec_head_no(head_line)
        no = _no if _no else k + 1
        if no <= prev:  # 重复/回退 → 顺延，保证唯一且单调
            no = prev + 1
        prev = no
        mt = _SEC_HEAD_TITLE.match(head_line)
        title = ((mt.group("title").strip() if mt else "") or f"小节{no:02d}")
        secs.append({"no": no, "title": title, "text": "\n".join(lines[h:end]).strip()})
    return secs


def _word_stats_text(full: str) -> str:
    """生成确定性字数统计文本（供体检/复评，消除 reviewer 凭感觉误判字数）。"""
    total = count_words_zh(full)
    secs = _split_full_sections(full)
    if secs:
        per = "、".join(f"小节{s['no']:02d} {count_words_zh(s['text'])}字" for s in secs)
        return f"全稿总字数：{total} 字；共 {len(secs)} 节。\n各节字数：{per}。"
    return f"全稿总字数：{total} 字。"


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_NUM_PAT = re.compile(r"\d+|[零一二三四五六七八九十百千万两]+")
# 「两」的自然指代：两人/两个人/两位/这两个/那两位……（替换为"俩"后不参与数字密度统计）
_LIANG_REF = re.compile(r"(?:这|那)?两(?:个(?:人)?|位|人)")


def _cn_num_to_int(s: str):
    """连续中文数字串转整数（支持 个/十/百/千/万）；无法解析返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = section = num = 0
    matched = False
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
            matched = True
        elif ch in _CN_UNITS:
            u = _CN_UNITS[ch]
            if u == 10000:
                section = (section + (num or 1)) * u
                total += section
                section = 0
            else:
                if num == 0:
                    num = 1  # 「十六」「千年」开头是单位
                section += num * u
            num = 0
            matched = True
        else:
            return None
    if not matched:
        return None
    return total + section + num


def _number_density(full: str, limit: int = 2):
    """统计每个数字（汉字+阿拉伯，归一化到数值）全篇出现次数，返回超过 limit 的 {数值: 次数}。

    排除值=1（"一"多为副词"一直/一起"，且"一根一根"由 AA 叠词规则覆盖）。
    「两」的自然指代（两人/两位/这两个/那两个人）不统计——它们是"两个人"的正常说法，
    强行改会生硬；只统计刻意计量（两回/两股/两道/两滚）。
    """
    text = _LIANG_REF.sub(lambda m: m.group(0).replace("两", "俩"), full)
    counts: dict[int, int] = {}
    for m in _NUM_PAT.finditer(text):
        val = _cn_num_to_int(m.group(0))
        if val is None or val <= 1:
            continue
        counts[val] = counts.get(val, 0) + 1
    return {v: c for v, c in counts.items() if c > limit}


def _number_density_text(full: str, limit: int = 2) -> str:
    """数字密度超标提示（确定性统计，供终审/补丁定向削减）。"""
    over = _number_density(full, limit)
    if not over:
        return ""
    lines = [f"【数字密度超标（程序逐字统计，非主观判断，必须削减）】同一数字全篇≤{limit} 次："]
    for v, c in sorted(over.items(), key=lambda x: -x[1]):
        lines.append(f"- 数字「{v}」全篇出现 {c} 次：保留最关键的 {limit} 处，其余换成具体事件/动作/不同数量，或直接删去数字。")
    return "\n".join(lines)


def _normalize_sections(full: str, min_n: int = 10, max_n: int = 14,
                        sec_ceil: int = 950) -> str:
    """确定性分节归并：①无论节数多少，统一节头为「NN」（写手会混写 ## 02 / 03 / 第N节）；
    ②节数过多（大量短节，实测写手会写28节）→ 贪心合并相邻节到 [min_n, max_n] 节。
    导语（首节头之前）保留。"""
    lines = full.splitlines()
    head_idx = [i for i, ln in enumerate(lines) if _SEC_HEAD.match(ln.strip())]
    if not head_idx:
        return full
    lead = "\n".join(lines[:head_idx[0]]).strip()
    secs = _split_full_sections(full)
    # 去掉每节 text 里重复的节头行（重组时统一重新编号）
    bodies = []
    for s in secs:
        bl = s["text"].splitlines()
        if bl and _SEC_HEAD.match(bl[0].strip()):
            bl = bl[1:]
        bodies.append("\n".join(bl).strip())

    def _rebuild(groups: list[str]) -> str:
        out = [lead] if lead else []
        for i, g in enumerate(groups, 1):
            out.append(f"{i:02d}\n\n{g}")
        return "\n\n".join(out)

    # 节数正常（≤max_n）：只统一节头，不合并
    if len(bodies) <= max_n:
        return _rebuild(bodies)

    total = sum(count_words_zh(b) for b in bodies)
    target_n = max(min_n, min(max_n, int(round(total / 700))))
    target_w = total / target_n
    # 贪心归并：当前节达到目标字数（或再并会超上限）即封口
    groups: list[str] = []
    cur = ""
    cur_w = 0
    for b in bodies:
        bw = count_words_zh(b)
        if cur and (cur_w >= target_w or cur_w + bw > sec_ceil):
            groups.append(cur)
            cur, cur_w = b, bw
        else:
            cur = (cur + "\n\n" + b).strip() if cur else b
            cur_w = count_words_zh(cur)
    if cur:
        groups.append(cur)
    while len(groups) > max_n:  # 边界兜底：尾部合并
        groups[-2] = groups[-2] + "\n\n" + groups[-1]
        groups.pop()
    return _rebuild(groups)


def word_gate(cfg: dict, llm, project: Project, full: str) -> tuple[str, dict]:
    """字数门：成稿必须在 [word_floor, word_ceiling] 区间。

    升级式打回策略：
      1) 每轮挑最短的 2 个小节扩写（目标=补齐缺口的六成），压缩时选最长小节；
      2) 某轮扩写增益 <300 字（小模型扩不动），改用整稿扩写兜底一次；
      3) 轮次用尽仍不足 → 标记 maxed_words_low（由 produce 执行淘汰，不留在工作台）。
    """
    from . import runlog

    rv = cfg["review"]
    floor = int(rv.get("word_floor", 8000))
    ceil = int(rv.get("word_ceiling", 12000))
    # 平台签约门槛高于全局下限时，以平台为下界：否则整篇跑完才在 platform_check
    # 判「不到签约字数」，白烧一整轮。番茄短故事的门槛是「短篇 >1 万字」。
    try:
        _pk = str((cfg.get("pitch_source") or {}).get("platform")
                  or cfg.get("studio", {}).get("platform") or "")
        _pmin = int(load_platform(_pk).get("min_words") or 0) if _pk else 0
    except Exception:  # noqa: BLE001
        _pmin = 0
    if _pmin > floor:
        print(f"[words] 下限 {floor} → {_pmin}（平台签约门槛）")
        floor = _pmin
    max_r = int(rv.get("word_gate_rounds", 3))
    if isinstance(llm, MockLLM):
        return full, {"gate": "skipped_mock", "rounds": 0, "total": count_words_zh(full),
                      "floor": floor, "ceiling": ceil}
    writer = resolve_llm(cfg, llm, "writer")
    pitch = project.load_json("pitch.json") or {}
    title = pitch.get("title", "无题")
    total = count_words_zh(full)
    # 扩写目标要高于 floor：成稿最后还有一步 _final_clean 会吃掉 0.5%–1.5% 的字
    # （小节头归一、空行整理、重复段去重），实测 8099→7944 直接跌破下限。
    # 这里按 2% 预留，pass/fail 判据仍用真实 floor（不虚报达标）。
    gate_floor = floor + max(120, int(floor * 0.02))
    rounds = 0

    def _strip_sec_head(text: str) -> str:
        bl = text.splitlines()
        if bl and _SEC_HEAD.match(bl[0].strip()):
            bl = bl[1:]
        return "\n".join(bl).strip()

    def _first_s(text: str) -> str:
        body = _strip_sec_head(text)
        m = re.search(r".+?[。！？]", body)
        return (m.group(0) if m else body[:30]).strip()

    def _last_s(text: str) -> str:
        body = _strip_sec_head(text)
        ss = [s for s in re.split(r"(?<=[。！？])", body) if s.strip()]
        return (ss[-1] if ss else body[-30:]).strip()

    def _rewrite_section(target: dict, mode: str, tgt: int, reason: str, secs: list) -> str | None:
        now = count_words_zh(target["text"])
        extra = ""
        if mode == "扩写":
            extra = f"\n\n**字数铁律：输出字数必须超过原文（当前 {now} 字，目标 {tgt} 字）**——只准加不准减，扩写部分（新增对话/动作/物件/情绪）要占输出的一半以上；若输出比原文短即为失败。"
        # 本节边界：本节最后一句 + 下一节开头第一句（让模型知道写到哪必须停）
        _sec_end = _last_s(target["text"])
        _nxt = next((s for s in secs if s["no"] == target["no"] + 1), None)
        _next_start = _first_s(_nxt["text"]) if _nxt else "（本节是最后一节，无下一节）"
        sys_p, user_p = render_prompt(
            "expand", TITLE=title, NO2=f"{target['no']:02d}", SEC_TITLE=target["title"],
            MODE=mode, NOW_WORDS=now, TARGET_TEXT=f"{'扩写' if mode == '扩写' else '压缩'}到约 {tgt} 字",
            REASON=reason + extra, SECTION_TEXT=target["text"],
            SECTION_END=_sec_end, NEXT_START=_next_start,
            GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
        )
        new_text = writer.chat(sys_p, user_p).strip()
        new_w = count_words_zh(new_text)
        if mode == "压缩":
            ok = now * 0.4 <= new_w < now      # 压缩：确实变小，且不小于原文四成
        else:
            # 扩写：变大但不超目标太多（new_w ≤ tgt+350）——配合"本节边界"提示，
            # 防模型越界往后写、把后续小节情节提前兑现，导致重复与时间线倒退。
            ok = now < new_w <= tgt + 350
        if ok:
            # 必须就地改目标节本身。旧实现按 no 回写整条列表（for s in secs if s["no"]==...），
            # 一旦节号重复就会把同一段新文本刷进多个节，扩写轮次叠加后成稿指数膨胀
            # （实测 9000 字稿被撑到 9.9 万字）。secs 存的就是 target 这个 dict，原地改即可。
            target["text"] = new_text
            return new_text
        print(f"[guard] 小节{target['no']:02d} 改写异常（{now}→{new_w} 字），保留原文")
        return None

    stall = 0  # 连续"字数无实质变化"轮数（≥2 即判定空转终止）
    while (total < gate_floor or total > ceil) and rounds < max_r:
        _round_total0 = total  # 本轮起始字数（空转守卫用）
        # 扩写失控守卫：一旦超过上限 1.5 倍，说明中间出现异常放大（历史上是节头误判
        # + 按节号批量回写把 9000 字稿撑到 9.9 万字）。此时立即停手，保留当前稿。
        if total > ceil * 1.5:
            print(f"[guard] 全稿 {total} 字 > 上限 {ceil} 的 1.5 倍，判定扩写失控，停止字数门")
            runlog.event("word_gate", f"扩写失控守卫：{total} 字（上限 {ceil}）")
            break
        # direct 模式（无 outline）：初稿常无「01/02」小节编号头。
        # 有编号头 → 走逐节扩写（单节任务模型不易坍缩，增益稳定）；
        # 无编号头 → 只能整稿扩写兜底（否则 _split_full_sections 返回空直接 break）。
        is_direct = not project.has("outline.json")
        secs = _split_full_sections(full)
        if total < gate_floor and is_direct and not secs:
            print(f"[gate] 字数门（direct模式，第{rounds + 1}/{max_r}轮）：全稿 {total} 字 → 整稿扩写…")
            runlog.event("word_gate", f"direct整稿扩写：当前 {total} 字")
            full = "\n\n".join(s["text"] for s in _split_full_sections(full)) or full
            sys_p, user_p = render_prompt(
                "polish_final",
                IDEA=(project.load_json("concept.json") or {}).get("core_trope", ""),
                ISSUES=(f"全稿 {total} 字低于下限 {floor} 字。要求：保持全部小节编号结构（01/02/03…）"
                        f"与情节骨架不变，把全稿扩写到约 {floor + 500} 字——逐节增加对话、动作细节、"
                        "外化情绪与具体钱数物件，每节扩到 650-900 字，禁止注水重复。"),
                FULL_TEXT=full,
                GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
                STYLE_SAMPLE=_style_sample_of(cfg, project),
            )
            new_full = writer.chat(sys_p, user_p).strip()
            new_w = count_words_zh(new_full)
            n_heads = sum(1 for ln in new_full.splitlines() if _SEC_HEAD.match(ln.strip()))
            if new_w >= total * 1.1 and n_heads >= 3:
                full = new_full
                total = new_w
                print(f"[gate] 整稿扩写 {new_w} 字（{total} → {new_w}）")
            else:
                print(f"[guard] 整稿扩写异常（{total}→{new_w} 字，{n_heads} 个小节头），保留原文")
            rounds += 1
            continue
        secs = _split_full_sections(full)
        if not secs:
            break
        if total < gate_floor:
            need = gate_floor - total
            order = sorted(secs, key=lambda s: count_words_zh(s["text"]))
            picks = order[:3] if len(order) >= 3 else order
            gain = 0
            for target in picks:
                now = count_words_zh(target["text"])
                tgt = min(now + int(need * 0.5) + 500, now * 2)
                print(f"[gate] 字数门（第{rounds + 1}/{max_r}轮）：全稿 {total} 字，扩写小节{target['no']:02d} → 约{tgt}字")
                runlog.event("word_gate", f"打回扩写：全稿{total}字，小节{target['no']:02d}（目标约{tgt}字）")
                new_text = _rewrite_section(
                    target, "扩写", tgt,
                    f"全稿 {total} 字低于下限 {floor} 字；本节偏薄，扩写补足血肉：增加对话、动作细节、钱数物件等具体描写，情节骨架与节末钩子不变",
                    secs)
                if new_text:
                    gain += count_words_zh(new_text) - now
                total = count_words_zh("\n\n".join(s["text"] for s in secs))
                if total >= gate_floor:
                    break
            # 扩写落盘：secs 已被 _rewrite_section 就地更新，必须回写 full——
            # 旧代码只在兜底路径回写，正常扩写路径的 total 涨了但 full 还是旧文本，
            # 「字数门结束：NNNN 字（pass）」是假象，返回给润色段的full并未变长（实测 8140→成稿7781）
            full = "\n\n".join(s["text"] for s in secs)
            # 扩写增益不足 → 整稿扩写兜底（每轮都可尝试，最多一次）
            if total < gate_floor and gain < 400 and rounds == max_r - 1:
                print(f"[gate] 小节扩写增益仅 {gain} 字，整稿扩写兜底 …")
                runlog.event("word_gate", f"整稿扩写兜底：当前 {total} 字")
                full = "\n\n".join(s["text"] for s in secs)
                sys_p, user_p = render_prompt(
                    "polish_final",
                    IDEA=(project.load_json("concept.json") or {}).get("core_trope", ""),
                    ISSUES=(f"全稿 {total} 字低于下限 {floor} 字。要求：保持全部小节编号结构（01/02/03…）"
                            f"与情节骨架不变，把全稿扩写到约 {floor + 500} 字——逐节增加对话、动作细节、"
                            "外化情绪与具体钱数物件，禁止注水重复。"),
                    FULL_TEXT=full,
                    GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
                    STYLE_SAMPLE=_style_sample_of(cfg, project),
                )
                new_full = writer.chat(sys_p, user_p).strip()
                new_w = count_words_zh(new_full)
                n_heads = sum(1 for ln in new_full.splitlines() if _SEC_HEAD.match(ln.strip()))
                if new_w >= full and n_heads >= 3:
                    full = new_full
                    total = new_w
                else:
                    print(f"[guard] 整稿扩写异常（{total}→{new_w} 字，{n_heads} 个小节头），保留原文")
        else:
            # 严重超标（> 上限 1.2 倍）时"每轮只压最长一节"杯水车薪（实测 193851 字
            # 靠 8 轮单节压缩绝无可能回到 12000），改走整稿压缩兜底——与不足时的
            # 整稿扩写兜底对称，一次重写把全局压回来。
            if total > ceil * 1.2:
                print(f"[gate] 字数门（第{rounds + 1}/{max_r}轮）：全稿 {total} 字严重超标，整稿压缩…")
                runlog.event("word_gate", f"整稿压缩：当前 {total} 字（上限 {ceil}）")
                sys_p, user_p = render_prompt(
                    "polish_final",
                    IDEA=(project.load_json("concept.json") or {}).get("core_trope", ""),
                    ISSUES=(f"全稿 {total} 字严重超出平台上限 {ceil} 字。要求：保持全部小节编号结构"
                            f"（01/02/03…）、情节骨架与每节钩子不变，把全稿压到约 {int(ceil * 0.95)} 字——"
                            "删除重复描写、无效对话与注水动作，只留推动情节和情绪的内容；"
                            "禁止整节删除（每节都必须保留）。"),
                    FULL_TEXT=full,
                    GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
                    STYLE_SAMPLE=_style_sample_of(cfg, project),
                )
                new_full = writer.chat(sys_p, user_p).strip()
                new_w = count_words_zh(new_full)
                n_heads = sum(1 for _ln in new_full.splitlines() if _SEC_HEAD.match(_ln.strip()))
                if new_w < total and n_heads >= 3:
                    full, total = new_full, new_w
                    print(f"[gate] 整稿压缩 {total} 字（{new_w} → 目标 {int(ceil * 0.95)}）")
                else:
                    print(f"[guard] 整稿压缩异常（{total}→{new_w} 字，{n_heads} 个小节头），保留原文")
                    break
            else:
                target = max(secs, key=lambda s: count_words_zh(s["text"]))
                now = count_words_zh(target["text"])
                # 目标值必须与 _rewrite_section 的压缩守卫（new_w ≥ 原文 40%）同源：
                # 旧值 max(..., 600) 在"单节 1.9 万字"时给出 600 的目标，而守卫要求
                # ≥7597 → 每轮必然被拒、8 轮空转烧掉 2/3 写手预算（实测事故）。
                tgt = max(now - (total - ceil) - 200, int(now * 0.55))
                print(f"[gate] 字数门（第{rounds + 1}/{max_r}轮）：全稿 {total} 字，压缩小节{target['no']:02d} → 约{tgt}字")
                runlog.event("word_gate", f"打回压缩：全稿{total}字，小节{target['no']:02d}（目标约{tgt}字）")
                _rewrite_section(target, "压缩", tgt,
                                 f"全稿 {total} 字超出上限 {ceil} 字；压缩本节冗余，保留情节骨架与钩子", secs)
                full = "\n\n".join(s["text"] for s in secs)
                total = count_words_zh(full)
        # 空转守卫：连续 2 轮字数几乎没变（<300 字）说明本轮所有改写都被守卫拒绝，
        # 继续跑只是烧 token（实测 8 轮空转 = 16 次 writer 调用、约 2/3 写手预算）。
        if abs(total - _round_total0) < 300:
            stall += 1
            if stall >= 2:
                print(f"[guard] 连续 2 轮字数无实质变化（{total} 字），停止字数门")
                runlog.event("word_gate", f"字数停滞守卫：{total} 字", level="warn")
                break
        else:
            stall = 0
        rounds += 1
    gate = "pass"
    if total < floor:
        gate = "maxed_words_low"
        runlog.event("word_gate", f"轮次用尽仍不足：{total}<{floor}字（将淘汰）", level="warn")
    elif total > ceil:
        gate = "maxed_words_high"
        runlog.event("word_gate", f"轮次用尽仍超标：{total}>{ceil}字", level="warn")
    print(f"[gate] 字数门结束：{total} 字（{gate}，{floor}-{ceil}）")
    return full, {"gate": gate, "rounds": rounds, "total": total, "floor": floor, "ceiling": ceil}


def _normalize_section_head(text: str, no: int, title: str) -> str:
    """把写手输出的任意形态章节头（## 第N章 X / ## 01 X / 第N章：X）统一成「NN 标题」。"""
    body = text.strip()
    head = f"{no:02d} {title}".strip()
    lines = body.splitlines()
    if lines and _SEC_HEAD.match(lines[0]):
        lines[0] = head
        return "\n".join(lines).strip()
    return f"{head}\n\n{body}".strip()


def _polish_intact(new_full: str, old_full: str) -> bool:
    """润色输出完整性：旧稿的每个小节头都要在、结尾必须成句（防截断稿混入成稿）。"""
    import re as _re

    heads = _re.findall(r"(?m)^\d{2} \S+", old_full)
    if heads and not all(h in new_full for h in heads):
        return False
    tail = (new_full or "").rstrip()
    if not tail or tail[-1] not in "。！？…”」』":
        return False
    return True


# 单节数量词/碎拍统计：模型初稿最严重的"一X/半X/X了一下"依赖。
# 碎拍补语：了一下/了一顿/了一眼/了一道/了半圈……
_QUANT_MICRO = re.compile(r"了[一两三几半]?[下阵圈道声眼口气会]")
# 半+量词：半片/半根/半条/半寸/半里/半盏/半桌/半截/半道/半块/半碗……
_QUANT_HALF = re.compile(r"半[根片条寸里盏桌截道块年辈子碗晌]")
# 一+量词（名量/动量），排除副词"一直/一起/一样/一般/一边/一时/一味"
_QUANT_ONE = re.compile(r"一(?![直起样般边时味])[步抖愣颤分颗根里片寸条顿遍弹道声口气眼会级层级席盅株]")
# 节拍碎句：一X，又一X / 一X一X
_QUANT_BEAT = re.compile(r"一[^\s，。]{1,2}[，,]\s*又一|一[步层级个寸根下条耸]一[步层级个寸根下条耸]")


def _section_quant_score(text: str) -> int:
    """单节数量词/碎拍密度分数（越高越需要去味重写）。"""
    if not text:
        return 0
    return (len(_QUANT_MICRO.findall(text))
            + len(_QUANT_HALF.findall(text))
            + len(_QUANT_ONE.findall(text))
            + len(_QUANT_BEAT.findall(text)) * 2)


def _section_quant_stats(text: str) -> str:
    """单节数量词统计明细（确定性，逐字统计，传给去味提示词）。"""
    from collections import Counter
    micro = _QUANT_MICRO.findall(text)
    half = _QUANT_HALF.findall(text)
    one = _QUANT_ONE.findall(text)
    beat = _QUANT_BEAT.findall(text)
    lines = [f"- 碎拍「X了一下/了X」共 {len(micro)} 处：" + "、".join(micro[:10])]
    if half:
        lines.append(f"- 「半X」共 {len(half)} 处：" + "、".join(half[:10]))
    if one:
        lines.append(f"- 「一X」共 {len(one)} 处：" + "、".join(one[:14]))
    if beat:
        lines.append(f"- 节拍碎句共 {len(beat)} 处：" + "、".join(beat[:6]))
    return "\n".join(lines)


def _rewrite_density_sections(cfg: dict, llm: BaseLLM, project: Project, full: str,
                              style_sample: str, max_secs: int = 3) -> str:
    """对数量词密度最高的几节做"只改表达、不改情节"的定向重写。

    比整稿重写安全（只动问题节，其余零改动），比定点补丁有效（整节重写能
    改变该节的整体数量词密度）。逐节重写后校验：字数不缩水、密度必须下降。
    """
    secs = _split_full_sections(full)
    if not secs:
        return full
    writer_p = resolve_llm(cfg, llm, "writer")
    direction = (project.load_json("concept.json") or {}).get("direction", "")
    gdir = load_genre_guide(cfg, direction)
    # 密度最高的 max_secs 节（阈值 8：只改明显严重的节；分数 5-7 的节重写反而帮倒忙，
    # 实测第二轮改这些节会把 74 分降到 68。提高阈值后，第二轮无节达标、自动停止）
    targets = sorted(secs, key=lambda s: -_section_quant_score(s["text"]))
    targets = [s for s in targets if _section_quant_score(s["text"]) >= 8][:max_secs]
    if not targets:
        return full
    print(f"[density] 逐节定向去味：小节 "
          + "、".join(f"{s['no']:02d}({_section_quant_score(s['text'])})" for s in targets))

    def _body_of(t):
        bl = t["text"].splitlines()
        if bl and _SEC_HEAD.match(bl[0].strip()):
            bl = bl[1:]
        return "\n".join(bl).strip()

    for target in targets:
        no = target["no"]
        now = count_words_zh(target["text"])
        before_score = _section_quant_score(target["text"])
        body = _body_of(target)
        sents = [s for s in re.split(r"(?<=[。！？])", body) if s.strip()]
        sec_end = sents[-1] if sents else body[-30:]
        nxt = next((s for s in secs if s["no"] == no + 1), None)
        nb = _body_of(nxt) if nxt else ""
        nm = re.search(r".+?[。！？]", nb)
        next_start = nm.group(0) if nm else ("（本节是最后一节）" if not nxt else nb[:30])
        try:
            sys_p, user_p = render_prompt(
                "polish_section",
                NO2=f"{no:02d}",
                SECTION_TEXT=target["text"],
                SECTION_END=sec_end,
                NEXT_START=next_start,
                QUANT_STATS=_section_quant_stats(target["text"]),
                STYLE_SAMPLE=style_sample,
                GENRE_GUIDE=gdir,
            )
            new_text = writer_p.chat(sys_p, user_p).strip()
        except Exception as e:  # noqa: BLE001
            print(f"[density] 小节{no:02d} 去味失败（{e}），保留原文")
            continue
        new_w = count_words_zh(new_text)
        new_score = _section_quant_score(new_text)
        # 校验：字数不缩水超 15%、密度必须下降，否则保留原文
        if new_w >= now * 0.85 and new_score < before_score:
            # 保证节头统一
            hl = new_text.splitlines()
            if not (hl and _SEC_HEAD.match(hl[0].strip())):
                new_text = f"{no:02d}\n\n" + new_text
            for s in secs:
                if s is target:  # 就地改目标节（按 no 回写会在节号重复时污染多节）
                    s["text"] = new_text
            print(f"[density] 小节{no:02d} 去味 {before_score}→{new_score}（{new_w}字）")
        else:
            print(f"[density] 小节{no:02d} 去味未达标（字数{new_w}/{now}、密度{new_score}/{before_score}），保留原文")
    return "\n\n".join(s["text"] for s in secs)


def _force_ending_rewrite(cfg: dict, llm: BaseLLM, project: Project, full: str,
                          style_sample: str, plan_sections: list | None = None) -> str:
    """末节强制收束：把最后一节重写为"大女主爽感、干脆收束"。

    一次写时模型常在高潮后预告新危机/停在悬念（魔修攻山、钟声、"才刚开始"）。
    末节是结尾质量的关键，单独重写末节（单一目标、单节），比放进多目标修复
    循环（整节重写/多目标补丁、总分不提升就回滚）更稳。
    """
    secs = _split_full_sections(full)
    if len(secs) < 2:
        return full
    writer_p = resolve_llm(cfg, llm, "writer")
    last = secs[-1]
    no = last["no"]
    now = count_words_zh(last["text"])

    def _body_of(t):
        bl = t["text"].splitlines()
        if bl and _SEC_HEAD.match(bl[0].strip()):
            bl = bl[1:]
        return "\n".join(bl).strip()

    prev = secs[-2]
    pb = _body_of(prev)
    ps = [s for s in re.split(r"(?<=[。！？])", pb) if s.strip()]
    prev_tail = ps[-1] if ps else pb[-40:]
    end_hook = ""
    if plan_sections:
        try:
            lp = plan_sections[-1]
            end_hook = str(lp.get("end_hook") or lp.get("goal") or "")
        except Exception:  # noqa: BLE001
            end_hook = ""
    direction = (project.load_json("concept.json") or {}).get("direction", "")
    gdir = load_genre_guide(cfg, direction)
    try:
        sys_p, user_p = render_prompt(
            "polish_ending",
            NO2=f"{no:02d}",
            END_SECTION=last["text"],
            PREV_TAIL=prev_tail,
            END_HOOK=end_hook or "（无明确计划，按大女主爽感、干脆收束写）",
            STYLE_SAMPLE=style_sample,
            GENRE_GUIDE=gdir,
        )
        new_text = writer_p.chat(sys_p, user_p).strip()
    except Exception as e:  # noqa: BLE001
        print(f"[ending] 末节收束失败（{type(e).__name__}），保留原文")
        return full
    new_w = count_words_zh(new_text)
    if new_w >= now * 0.85:
        hl = new_text.splitlines()
        if not (hl and _SEC_HEAD.match(hl[0].strip())):
            new_text = f"{no:02d}\n\n" + new_text
        secs[-1]["text"] = new_text
        print(f"[ending] 末节强制收束完成（{now}→{new_w}字）")
        return "\n\n".join(s["text"] for s in secs)
    print(f"[ending] 末节收束缩水（{new_w}/{now}），保留原文")
    return full


def _number_fix_once(cfg: dict, llm: BaseLLM, project: Project, full: str,
                     rounds: int = 3) -> str:
    """数字密度定向替换（多轮迭代）：每轮只改含重复数字的句子，其他句子零改动。

    比整节去味（整节重写、易改情节）风险小，比多目标补丁（一次修6个问题）
    目标单一。一次补丁（≤12 个）改不完所有重复数字，故迭代多轮：
    每轮校验"超标数字总出现次数"下降（不是只看种类数——"两"从17降到10
    虽仍超标、但是真实进步），字数允许小幅缩水（≥95%）。
    """
    if not _number_density(full):
        return full
    concept = project.load_json("concept.json") or {}
    idea = concept.get("core_trope", "") or concept.get("idea", "")
    direction = concept.get("direction", "")
    gdir = load_genre_guide(cfg, direction)
    editor_p = resolve_llm(cfg, llm, "reviewer")
    cur = full
    for r in range(rounds):
        over = _number_density(cur)
        if not over:
            break
        before = count_words_zh(cur)
        over_total = sum(over.values())
        try:
            sys_p, user_p = render_prompt(
                "number_fix",
                IDEA=idea,
                FULL_TEXT=cur,
                NUMBER_DENSITY=_number_density_text(cur),
                GENRE_GUIDE=gdir,
            )
            out = editor_p.chat_json(sys_p, user_p)
            plist = (out or {}).get("patches", []) if isinstance(out, dict) else []
            plist = plist[:12]
        except Exception as e:  # noqa: BLE001
            print(f"[numfix] 第{r+1}轮补丁生成失败（{type(e).__name__}），停止")
            break
        if not plist:
            break
        new_full, n_applied, n_skip = _apply_patches(cur, plist)
        print(f"[numfix] 第{r+1}轮：生成 {len(plist)} 补丁，应用 {n_applied}（跳过 {len(n_skip)}）")
        new_w = count_words_zh(new_full)
        new_over_total = sum(_number_density(new_full).values())
        if (n_applied > 0 and new_w >= before * 0.95
                and new_over_total < over_total):
            print(f"[numfix] 超标总次数 {over_total}→{new_over_total}（{new_w}字）")
            cur = new_full.strip()
        else:
            print(f"[numfix] 第{r+1}轮未改善（总次数{over_total}→{new_over_total}，字数{new_w}/{before}），停止")
            break
    return cur


def _sanitize_lead(text: str) -> str:
    """导语元文本清洗：截掉模型附带的【修改逻辑解析】【写作思路】等自评块。"""
    for marker in ("【修改逻辑解析】", "【修改逻辑", "【写作思路】", "【解析】", "【修改说明】"):
        i = text.find(marker)
        if i > 0:
            text = text[:i]
    return text.strip()


def _extract_idea_core_kws(idea: str) -> list[str]:
    """从故事梗提取核心设定关键词，用于梗即契约的确定性校验。

    提取原则（宁缺勿滥，避免误伤）：
    - 数字类设定（“二十年前”“七号”“二十二岁”“一笔钱”）整段保留，正文必须出现；
    - 明确的核心矛盾短语（“欠过”“必须核对”“守则第七条”等）整段保留；
    - 排除通用词（我/他/她/的/了/今晚/今天/明天/没有/不/一/个等），避免过度约束。
    """
    if not idea:
        return []
    import re as _re
    kws: list[str] = []
    # 1) 数字 + 量词/时间单位组合（如 二十年前 / 七号 / 二十二岁 / 一笔钱 / 第七条 / 凌晨两点）
    for m in _re.findall(r"[0-9一二三四五六七八九十百千万两]+(?:[年前号岁笔点条位个次回天夜份])", idea):
        k = m.strip()
        if k and k not in kws and len(k) <= 12:
            kws.append(k)
    # 2) 明确的核心动作/矛盾短语（带“欠/核对/必须/不准/不能/不许/欠过”等强动词的相邻4-6字）
    for m in _re.findall(r"[\u4e00-\u9fa5]{1,2}(?:欠|核对|必须|不许|不准|不能|提醒|提示)[\u4e00-\u9fa5]{1,4}", idea):
        k = m.strip()
        if k and k not in kws and 4 <= len(k) <= 6:
            kws.append(k)
    # 3) 去重、去含通用词的项
    drop = ("我没有", "我今天", "我没有", "我今年", "不")
    out = []
    for k in kws:
        if any(d in k for d in drop):
            continue
        out.append(k)
    # 纯叙事梗（无数字/无强规则）不做确定性实体校验——中文分词/后缀法对
    # 复合实体（祭剑大典、天生剑骨）提取不可靠，已多次误报（"剑架"/"骨本"）。
    # 这类梗的"扣题"由 direct_write 提示词（梗即契约，写手自查）+ 终审 reviewer
    # 语义判断负责，比关键词逐字命中更准确，也避免无意义的重写。
    return out[:8]


def assemble(project: Project) -> tuple[str, list[dict]]:
    """按章序拼接全稿正文；小节头统一为 01/02/03 两位数字编号（不含书名头部）。"""
    parts = []
    total = 0
    stats = []
    outline = project.load_json("outline.json") or {}
    titles = {c.get("no"): c.get("title", "") for c in outline.get("chapters", [])}
    for ch in project.read_chapters():
        w = count_words_zh(ch["text"])
        total += w
        stats.append({"no": ch["no"], "words": w})
        parts.append(_normalize_section_head(ch["text"], ch["no"], titles.get(ch["no"], "")))
        parts.append("")
    return "\n".join(parts).strip(), stats


def _apply_patches(full: str, patches: list[dict]) -> tuple[str, int, list[dict]]:
    """在 full 上确定性应用 search/replace 补丁（未涉及的文字零改动）。

    每个补丁的 find 必须在当前稿中**唯一出现**才替换；找不到（已被前序补丁覆盖）
    或出现多次（不唯一）则跳过并记录。返回 (新稿, 已应用数, 跳过明细)。
    """
    cur = full
    applied = 0
    skipped: list[dict] = []
    for p in patches:
        # 模型偶尔把 patches 写成字符串数组（"旧句→新句"），而不是 {find, replace} 对象。
        # 旧实现直接 p.get(...) → AttributeError 'str' object has no attribute 'get'，
        # 整条润色体检阶段崩掉（e2e5 实测，request_id=511c4205-186448）。
        if isinstance(p, str):
            for sep in ("→", "->", "=>"):
                if sep in p:
                    a, b = p.split(sep, 1)
                    p = {"find": a.strip(), "replace": b.strip()}
                    break
            else:
                skipped.append({"find": p[:30], "reason": "字符串补丁不是「旧→新」格式"})
                continue
        if not isinstance(p, dict):
            skipped.append({"find": str(p)[:30], "reason": f"补丁类型异常 {type(p).__name__}"})
            continue
        find_raw = p.get("find") or ""
        repl = p.get("replace")
        if not isinstance(find_raw, str):
            find_raw = str(find_raw)
        if repl is not None and not isinstance(repl, str):
            repl = str(repl)
        if not find_raw or repl is None:
            skipped.append({"find": find_raw[:30], "reason": "字段不完整"})
            continue
        done = False
        for cand in (find_raw, find_raw.strip()):  # 优先原样，再退 strip 版
            if cur.count(cand) == 1:
                cur = cur.replace(cand, repl, 1)
                applied += 1
                done = True
                break
        if not done:
            skipped.append({"find": find_raw.strip()[:30], "reason": f"count={cur.count(find_raw.strip())}"})
    return cur, applied, skipped


@runlog.staged("润色体检")
def stage_polish(cfg: dict, llm: BaseLLM, project: Project, skip_final_llm: bool = False, force: bool = False) -> dict:
    if project.has("report.json") and not force:
        print("[stage] report.json 已存在，跳过润色体检（--refresh-polish 可强制重跑）")
        return project.load_json("report.json")
    full, stats = assemble(project)
    # 直接写模式：没有 outline.json，ch01.md 就是完整成稿（导语+正文），
    # 不经过 _normalize_section_head 规范化小节头（否则会把导语改成 "01 "）
    if not project.has("outline.json") and project.chapter_file(1).exists():
        full = project.chapter_file(1).read_text(encoding="utf-8")
        stats = [{"no": 1, "words": count_words_zh(full)}]
    if not full.strip():
        raise SystemExit("没有章节内容可润色")
    pitch = project.load_json("pitch.json")
    bible = project.load_json("bible.json")
    title = pitch.get("title", "无题")

    # 字数门：8000-12000 区间，不足/超标先打回扩写/压缩（每轮记录运行日志）
    full, wg = word_gate(cfg, llm, project, full)
    stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
              for s in _split_full_sections(full)] or stats)
    # 语言打磨：先把"句子病"改掉，再交给体检官评分。
    # 为什么放在字数门之后、体检之前：
    #   · 字数门要保证长度在区间内，语言打磨是按节等长改写（±12%），放前面会被字数门再拉一次；
    #   · 体检官（polish_fix）与全稿读者团评的是"读起来怎么样"，
    #     如果先让它看一堆碎句/欧化句，它会把这些缺陷写进 issues，
    #     后续的定点修复反而在"修风格"而不是"修故事"，浪费整轮预算。
    # 关闭方式：config.review.lang_polish_enabled = false 或 CLI --no-lang-polish。
    _lang_rep = None
    _repair_rep = None
    _lang_err = ""
    # 确定性返修（零 token）：合并「一句话拆成几个短句」的同主语句链、切超长句、
    # 切超长无对话段、去罗列重复、改叠词口癖。
    # 为什么必须在 polish/converge 之前：这些是**机械**改法，规则唯一，
    # 模型改整节时反而会引入等量新病（实测 e2e5 节02 chain 8→4 而总分 11→21；
    # 整稿 converge 三轮 3 改 8 拒，缺陷密度 1.84 → 1.95）。
    # 先确定性清零，模型只需要处理"欧化句/重复表述/详略"这些真要判断的部分。
    if (cfg.get("review") or {}).get("lang_repair_enabled", True):
        try:
            from .language import deterministic_repair as _lang_repair
            from .language import language_fails as _lang_fails

            _fails_before = _lang_fails(full)
            full, _repair_rep = _lang_repair(full)
            _fails_after = _lang_fails(full)
            _repair_rep["fails_before"] = _fails_before
            _repair_rep["fails_after"] = _fails_after
            _repair_rep = {"pass": not _fails_after, **_repair_rep}
            stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                      for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001
            _lang_rep = {"pass": False, "error": f"{type(e).__name__}: {e}"}
            print(f"[repair] ⚠ 确定性返修跳过（{type(e).__name__}: {e}）")
    if (cfg.get("review") or {}).get("lang_polish_enabled", True) and not isinstance(llm, MockLLM):
        try:
            from .language import polish as _lang_polish

            _direction_lp = (project.load_json("concept.json") or {}).get("direction", "")
            full, _lang_rep = _lang_polish(
                cfg, llm, project, full,
                rounds=int((cfg.get("review") or {}).get("lang_polish_rounds", 3)),
                style_sample=_style_sample_of(cfg, project),
                genre_guide=load_genre_guide(cfg, _direction_lp),
                kbcard=_lang_kbcard(cfg, _direction_lp),
            )
            stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                      for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001 —— 语言打磨失败不该让整篇成稿丢掉
            # 但**绝不能静默**：历史上这里异常被吞掉后，语言打磨等于从未生效，
            # 成稿照样入库、照样评分，问题只能靠人肉读稿发现。必须留下痕迹。
            _lang_err = f"{type(e).__name__}: {e}"
            print(f"[lang] ⚠ 语言打磨跳过（{_lang_err}），继续体检——成稿可能带四类语言病")
    # 投稿线收敛：polish 判的是「这一节自身有没有病」（节内相对口径），
    # 而平台卡的是整稿**密度**（0.97/千字 这种整稿超标，单节看是正常的）。
    # 少这一层时 e2e5 两轮 polish「4 改 4 回滚」，chain/detail 卡在 0.97 原地不动。
    _conv_rep = None
    if (cfg.get("review") or {}).get("lang_converge_enabled", True) and not isinstance(llm, MockLLM):
        try:
            from .language import converge as _lang_converge

            full, _conv_rep = _lang_converge(
                cfg, llm, project, full,
                max_rounds=int((cfg.get("review") or {}).get("lang_converge_rounds", 3)),
                per_round=int((cfg.get("review") or {}).get("lang_converge_per_round", 4)),
                style_sample=_style_sample_of(cfg, project),
            )
            stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                      for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001 —— 收敛失败不该毁掉成稿，但必须留痕
            _conv_rep = {"enabled": True, "error": f"{type(e).__name__}: {e}"}
            print(f"[converge] ⚠ 投稿线收敛跳过（{type(e).__name__}: {e}）")
    # 节长均衡：语言打磨只管句内/段内，管不到「某节水、某节赶」这种全篇结构病。
    # 判据与投稿体检同源（MARKET_TARGET.section_ratio_max），改不动就原样返回。
    _bal_rep = None
    if (cfg.get("review") or {}).get("lang_balance_enabled", True) and not isinstance(llm, MockLLM):
        try:
            from .language import balance as _lang_balance

            full, _bal_rep = _lang_balance(
                cfg, llm, project, full,
                max_edits=int((cfg.get("review") or {}).get("lang_balance_max_edits", 4)),
                style_sample=_style_sample_of(cfg, project),
            )
            stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                      for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001 —— 均衡失败不该毁掉成稿，但必须留痕
            _bal_rep = {"enabled": True, "error": f"{type(e).__name__}: {e}"}
            print(f"[balance] ⚠ 节长均衡跳过（{type(e).__name__}: {e}）")
    # 交付前补对白（2026-10-26 古言实测）：章级对白门按章放行，终审整体重写会把对白
    # 改成概述——全稿 0.233 < 0.25，同时制造「>200 字无对话段」，一次踩三条线
    # （C节奏/对话段占比、C节奏/超长无对话段、B语言/详略）。三条同源，合成一个门。
    _dia_rep = None
    if (cfg.get("review") or {}).get("lang_dialog_enabled", True) and not isinstance(llm, MockLLM):
        try:
            from .language import dialog_raise as _lang_dialog

            full, _dia_rep = _lang_dialog(
                cfg, llm, project, full,
                max_edits=int((cfg.get("review") or {}).get("lang_dialog_max_edits", 3)),
                style_sample=_style_sample_of(cfg, project),
            )
            stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                      for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001 —— 补对白失败不该毁掉成稿，但必须留痕
            _dia_rep = {"enabled": True, "error": f"{type(e).__name__}: {e}"}
            print(f"[dialog] ⚠ 补对白跳过（{type(e).__name__}: {e}）")
    # 收尾确定性返修（零 token，第二轮）：repair → polish → converge → balance 的链条里，
    # 后三步都是整节重写，可能把碎句链/超长段重新写回来。出口处再扫一遍，
    # 保证落到磁盘的成稿是机械干净的——这一步不依赖模型，永远不会恶化。
    _repair_final = None
    if (cfg.get("review") or {}).get("lang_repair_enabled", True):
        try:
            from .language import deterministic_repair as _lang_repair2

            _before_w = count_words_zh(full)
            full, _rf = _lang_repair2(full)
            _rf["words_before"] = _before_w
            _rf["words_after"] = count_words_zh(full)
            _repair_final = _rf
            if any(int(_rf.get(k) or 0) for k in
                   ("merged_chains", "split_long_sents", "split_paras",
                    "deduped_paras", "deduped_enums", "varied_aa")):
                stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                          for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001
            _repair_final = {"error": f"{type(e).__name__}: {e}"}
            print(f"[repair] ⚠ 收尾确定性返修跳过（{type(e).__name__}: {e}）")
    # 风格范文缺省值：mock/跳过终审时 else 分支不执行、不会读 KB（style_samples 要扫全量
    # 单元），但后面的全稿读者循环仍会用到本变量——必须先绑定，否则 UnboundLocalError。
    _style_sample_f = STYLE_SAMPLE_EMPTY
    if skip_final_llm or isinstance(llm, MockLLM):
        # 本地统计体检（mock/离线模式）
        report = {
            "verdict": "pass",
            "score": 75,
            "issues": [],
            "mode": "local-stats",
        }
        print(f"[stage] 本地体检：全稿 {count_words_zh(full)} 字")
    else:
        editor_llm = resolve_llm(cfg, llm, "reviewer")
        # 评分专用低温度 LLM：评分是判断任务，用 0.85 会让同一稿子分数随机波动
        # （实测同稿回滚后 74→68），修复循环因此追逐噪声而非真正提升质量。
        # 用 temperature=0.1 单独构建一个稳定评分实例（不影响修复生成用的 editor_llm）。
        _rcfg = (cfg.get("llm_roles") or {}).get("reviewer") or {}
        _judge_cfg = {**cfg["llm"], **_rcfg, "temperature": 0.1}
        _judge_llm = _build_llm(_judge_cfg, "judge:stable")
        if ("127.0.0.1" in editor_llm.base_url or "localhost" in editor_llm.base_url) and len(full) > 12000:
            print(f"[warn] 本地模型全稿体检：正文 {len(full)} 字。若引擎上下文 <16K 会截断，"
                  "建议调大 OLLAMA_CONTEXT_LENGTH 或对 review 配置更强模型")
        _rvs_all = [float(v["review_score"]) for v in (project.meta.get("reviews") or {}).values()
                    if isinstance(v, dict) and isinstance(v.get("review_score"), (int, float))]
        if _rvs_all:
            _rm = round(sum(_rvs_all) / len(_rvs_all), 2)
            _rmin = round(min(_rvs_all), 2)
            # 终审分与逐章评审卡的分档必须同档（决策 11）。空口要求模型"别差一档"
            # 实测无效（e2e8/9/10 三篇终审 58/58/66，评审卡折算 77/80/74）。
            # 把评审卡结论**显式喂给**判官，让它只能二选一：解释自己看到了什么
            # 卡漏看的 high 级硬伤，或按同一档打分。
            _rubric_hint = (
                f"逐章评审卡（0-10 档）折算：均值 {_rm} 分（=百分制 {_rm * 10:.0f}），"
                f"最低一章 {_rmin} 分，共 {len(_rvs_all)} 章。\n"
                "你的百分制分数与它相差超过 10 分时，**必须**在 issues 里指出该卡漏看的"
                "high 级硬伤（第几节、哪一处：卡点失效/连续性断裂/人物动机缺失/烂尾），"
                "指不出来就按同一档打分——不要用 low 级瑕疵把它压到 70 以下。"
            )
        else:
            _rubric_hint = "（本次无逐章评审卡折算，按正文实际质量打分）"
        sys_p, user_p = render_prompt(
            "polish_fix",
            BIBLE=json.dumps(bible, ensure_ascii=False),
            FULL_TEXT=full,
            WORD_STATS=_word_stats_text(full),
            NUMBER_DENSITY=_number_density_text(full),
            RUBRIC_HINT=_rubric_hint,
            GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
        )
        print("[stage] 全稿体检（卡点/连续性/文风/安全，连评3次取中位数）…")
        try:
            _reps0 = []
            for _j in range(3):
                try:
                    _o0 = _judge_llm.chat_json(sys_p, user_p)
                    if isinstance(_o0, dict):
                        _reps0.append(_o0)
                except Exception:
                    continue
            if not _reps0:
                raise ValueError("体检输出非对象")
            _reps0.sort(key=lambda x: float(x.get("score") or 0))
            report = _reps0[len(_reps0) // 2]
        except Exception as e:
            print(f"[stage] 体检输出异常，降级为本地统计：{e}")
            report = {"verdict": "pass", "score": 70, "issues": [], "mode": "local-stats"}

        # 终审硬门槛：体检分 < final_score_floor（默认80）即修复→复评循环。
        # final_fix_rounds=0 表示不限制轮次（直到达标/守卫），>0 为上限。
        # mode=local-stats 是降级/离线体检，无 LLM 分可循环，跳过。
        floor = float(cfg["review"].get("final_score_floor", 80))
        fix_rounds_raw = str(cfg["review"].get("final_fix_rounds", 3)).strip().lower()
        fix_rounds = -1 if fix_rounds_raw in ("0", "unlimited", "inf", "-1", "none") else int(fix_rounds_raw)
        rnd_f = 0
        # 梗即契约：修复轮内保护梗核心设定（direct 模式梗在 concept.json 的 core_trope 里）。
        _idea_kws_f = _extract_idea_core_kws((project.load_json("concept.json") or {}).get("core_trope", ""))
        # 风格范文：整稿重写时注入，让模型模仿同题材真实语感（密度型问题走整稿重写）
        _direction_f = (project.load_json("concept.json") or {}).get("direction", "")
        try:
            from . import kb as _kbp
            _style_sample_f = (_kbp.style_samples(cfg, direction=_direction_f, n=2, max_chars=500)
                               or STYLE_SAMPLE_EMPTY)
        except Exception:  # noqa: BLE001
            _style_sample_f = STYLE_SAMPLE_EMPTY

        def _do_check(text: str) -> dict:
            """对指定稿做体检（确定性字数随文注入，避免 reviewer 估算字数）。
            评分稳定化：用低温度评分实例连评 3 次、取分数中位数那次，
            消除单次温度噪声（同一稿子不再 74/68 横跳）。"""
            c_sys, c_user = render_prompt(
                "polish_fix",
                BIBLE=json.dumps(bible, ensure_ascii=False),
                FULL_TEXT=text,
                WORD_STATS=_word_stats_text(text),
                NUMBER_DENSITY=_number_density_text(text),
                RUBRIC_HINT=_rubric_hint,
                GENRE_GUIDE=load_genre_guide(
                    cfg, (project.load_json("concept.json") or {}).get("direction", "")),
            )
            reps = []
            for _j in range(3):
                try:
                    out = _judge_llm.chat_json(c_sys, c_user)
                    if isinstance(out, dict):
                        reps.append(out)
                except Exception:
                    continue
            if not reps:
                raise ValueError("体检输出非对象")
            reps.sort(key=lambda x: float(x.get("score") or 0))
            return reps[len(reps) // 2]

        def _pad_words(text: str, floor_w: int) -> str:
            """补丁/精炼后字数被压到下限以下时，确定性逐节扩写补回（只加新对话/新动作/
            新信息，不注水），复用 expand 提示词；补不回则原样返回。"""
            writer_p = resolve_llm(cfg, llm, "writer")
            secs = _split_full_sections(text)
            if not secs:
                return text
            _title = (project.load_json("pitch.json") or {}).get("title", "无题")
            _gdir = load_genre_guide(
                cfg, (project.load_json("concept.json") or {}).get("direction", ""))
            for _ in range(3):
                total = count_words_zh(text)
                if total >= floor_w:
                    break
                need = floor_w - total
                target = sorted(secs, key=lambda s: count_words_zh(s["text"]))[0]
                now = count_words_zh(target["text"])
                tgt = min(now + need + 200, now * 2)
                # 本节边界：本节最后一句 + 下一节开头（让模型知道写到哪必须停）
                def _body_of(t):
                    bl = t["text"].splitlines()
                    if bl and _SEC_HEAD.match(bl[0].strip()):
                        bl = bl[1:]
                    return "\n".join(bl).strip()
                _tb = _body_of(target)
                _ss = [s for s in re.split(r"(?<=[。！？])", _tb) if s.strip()]
                _sec_end = _ss[-1] if _ss else _tb[-30:]
                _nxt = next((s for s in secs if s["no"] == target["no"] + 1), None)
                _nb = _body_of(_nxt) if _nxt else ""
                _nm = re.search(r".+?[。！？]", _nb)
                _next_start = _nm.group(0) if _nm else ("（本节是最后一节）" if not _nxt else _nb[:30])
                sys_p, user_p = render_prompt(
                    "expand", TITLE=_title, NO2=f"{target['no']:02d}", SEC_TITLE="",
                    MODE="扩写", NOW_WORDS=now, TARGET_TEXT=f"扩写到约 {tgt} 字",
                    REASON=(f"前一步精炼后全稿 {total} 字，需补回 {need} 字。只增加新对话、新动作、"
                            "新信息、新物件，情节骨架与节末钩子不变，禁止注水重复。"
                            f"\n\n**字数铁律：输出必须超过原文（当前 {now} 字，目标 {tgt} 字），只准加不准减。**"),
                    SECTION_TEXT=target["text"], GENRE_GUIDE=_gdir,
                    SECTION_END=_sec_end, NEXT_START=_next_start)
                try:
                    new_text = writer_p.chat(sys_p, user_p).strip()
                except Exception:  # noqa: BLE001
                    break
                if count_words_zh(new_text) > now:
                    target["text"] = new_text  # 就地改目标节，避免按 no 回写污染同号节
                    text = "\n\n".join(s["text"] for s in secs)
                else:
                    break
            return text

        # 进入多目标修复循环前，先做确定性、单一目标的数字定向替换
        # （只改含重复数字的句子，其他零改动），避免数字问题在"整节去味/多目标
        # 补丁、总分不提升就回滚"的循环里反复。修复后重新体检。
        if _number_density(full):
            try:
                _nf = _number_fix_once(cfg, llm, project, full)
                if _nf != full and count_words_zh(_nf) >= count_words_zh(full) * 0.9:
                    full = _nf
                    report = _do_check(full)
                    print(f"[stage] 数字定向修复后复评 {report.get('score')} 分")
            except Exception as e:  # noqa: BLE001
                print(f"[stage] 数字定向修复异常（{type(e).__name__}），继续")

        # 历史最佳版本：修复轮只保留分数不低于当前的版本，防"越修越差"。
        best_full, best_score = full, float(report.get("score") or 0)
        best_report = report  # 与 best_full 同时刻的体检结论（见下方"留优回填"）
        fail_streak = 0  # 连续"补丁后未提升/未达标"的轮数（≥2 即收敛终止）
        _rewrite_n = 0  # 密度型整稿重写次数（独立上限4次，防无限循环）
        while (report.get("mode") != "local-stats"
               and (report.get("verdict") == "need_fix"
                    or float(report.get("score") or 0) < floor)
               and (fix_rounds < 0 or rnd_f < fix_rounds)):
            rnd_f += 1
            issues = list(report.get("issues") or [])
            if not issues:
                issues = [{"severity": "mid", "type": "文风", "location": "全稿",
                           "note": f"体检分 {report.get('score')} 低于放行线 {floor}",
                           "fix": "逐节自查升质：卡点密度（每500字一卡点）/连续性（人名金额时间线）/"
                                  "文风（短句口语、细节具体到钱与物件、对话过半、去AI腔）"}]
            # 梗核心缺失必须进修复问题清单（模型重写时常把梗设定改丢）——仅数字核心词强制
            if _idea_kws_f:
                _num_f = [k for k in _idea_kws_f if any(c.isdigit() or c in "一二三四五六七八九十百千万两" for c in k)]
                miss_f = [k for k in _num_f if k not in full]
                if miss_f and len(miss_f) >= max(1, len(_num_f) // 2):
                    issues.insert(0, {"severity": "high", "type": "梗即契约",
                                      "location": "全稿",
                                      "note": f"故事梗核心设定缺失：{'、'.join(miss_f)}",
                                      "fix": "把这些设定逐条放回正文，成为情节引擎（时间/关系/矛盾/道具/规则一个都不能少）"})
            # 最小改动：每轮只修最严重的 6 处（补丁过多会累积引入新问题，实测26补丁→分数反降）。
            _sev_rank = {"high": 0, "mid": 1, "low": 2}
            issues = sorted(issues, key=lambda x: _sev_rank.get(x.get("severity"), 1))[:6]
            _why_f = []
            if float(report.get("score") or 0) < floor:
                _why_f.append(f"{report.get('score')} 分 < 门槛 {floor}")
            if report.get("verdict") == "need_fix":
                _hi_f = sum(1 for i in (issues or []) if str(i.get("severity")) == "high")
                _why_f.append(f"判官判 need_fix（high 级硬伤 {_hi_f} 处）")
            print(f"[stage] 终审不通过：{'；'.join(_why_f) or '未达门槛'}，"
                  f"第 {rnd_f}/{'∞' if fix_rounds < 0 else fix_rounds} 轮修复（本批 {len(issues)} 处）…")
            # ---- 修复方式：补丁优先（reviewer 输出定点 search/replace，确定性应用），
            # 补丁无法生成/无改动/坍缩时降级为整稿重写（writer，polish_final）----
            _concept = project.load_json("concept.json") or {}
            _genre = load_genre_guide(cfg, _concept.get("direction", ""))
            new_full = None
            # 密度型问题判定：数字/碎拍/AI腔是全文统计性问题，补丁修8处不改变整体密度
            # （实测第1轮补丁反而把68分降到64），≥2 条这类文风问题 → 直接整稿重写。
            def _is_density(iss_list: list) -> bool:
                n = 0
                for iss in iss_list:
                    note = str(iss.get("note", ""))
                    if str(iss.get("type", "")) == "文风" and any(
                            k in note for k in ("出现", "密度", "口癖", "AI", "堆叠", "超标")):
                        n += 1
                return n >= 2
            _density_issue = _is_density(issues)
            # 整节重写是高方差操作：能救烂稿（62→72），但初稿较好时（70）反而破坏（→68）。
            # 只在分数<65（确实烂）时才整节重写；分数≥65 走定点补丁（低风险、不毁好稿）。
            _cur_score = float(report.get("score") or 0)
            _allow_rewrite = _cur_score < 65
            if _density_issue and not _allow_rewrite:
                print(f"[guard] 当前 {_cur_score} 分（≥65），整节重写风险高，改走定点补丁")
                _density_issue = False
            if _density_issue:
                _rewrite_n += 1
                # 密度型问题走"逐节定向去味"（只改表达、不整稿重写，实测整稿
                # 重写连续3轮把稿子改差）；重置连续失败计数，最多4次
                fail_streak = 0
                if _rewrite_n > 4:
                    print("[guard] 密度型修复已达4次上限，收敛终止")
                    break
                try:
                    _den_full = _rewrite_density_sections(
                        cfg, llm, project, full, _style_sample_f, max_secs=3)
                    if (_den_full != full
                            and count_words_zh(_den_full) >= count_words_zh(full) * 0.9):
                        new_full = _den_full.strip()
                except Exception as e:  # noqa: BLE001
                    print(f"[density] 逐节去味异常（{e}），降级整稿重写")
            if cfg["review"].get("patch_fix", True) and not _density_issue:
                try:
                    psys, puser = render_prompt(
                        "polish_patch",
                        IDEA=_concept.get("core_trope", ""),
                        ISSUES=json.dumps(issues, ensure_ascii=False, indent=1),
                        FULL_TEXT=full,
                        TOTAL_WORDS=count_words_zh(full),
                        NUMBER_DENSITY=_number_density_text(full),
                        GENRE_GUIDE=_genre,
                    )
                    pout = editor_llm.chat_json(psys, puser)
                    plist = pout.get("patches", []) if isinstance(pout, dict) else []
                    plist = plist[:8]  # 硬限制每轮最多 8 补丁
                    if plist:
                        patched, n_applied, n_skip = _apply_patches(full, plist)
                        print(f"[patch] 生成 {len(plist)} 个补丁，应用 {n_applied} 个"
                              f"（跳过 {len(n_skip)}）")
                        if n_applied > 0:
                            new_full = patched.strip()
                except Exception as e:  # noqa: BLE001
                    print(f"[patch] 补丁生成/应用失败（{e}），降级整稿重写")
            # 兜底：整稿重写（writer）
            if new_full is None:
                writer_llm = resolve_llm(cfg, llm, "writer")
                sys_f, user_f = render_prompt(
                    "polish_final",
                    IDEA=_concept.get("core_trope", ""),
                    ISSUES=json.dumps(issues, ensure_ascii=False, indent=1),
                    FULL_TEXT=full,
                    GENRE_GUIDE=_genre,
                    STYLE_SAMPLE=_style_sample_f,
                )
                # 字数铁律：修复不是重写，吸收意见修问题，但全稿必须保住原稿 90% 以上篇幅
                user_f += (f"\n\n**字数铁律：当前全稿 {count_words_zh(full)} 字（平台下限 "
                           f"{int(cfg['review'].get('word_floor', 8000))} 字）。修问题可以改句子、换细节，"
                           f"但不准整节删除或概括压缩——输出不得低于原稿 90% 篇幅。**")
                new_full = writer_llm.chat(sys_f, user_f).strip()
            # 完整性守卫：残缺（小节头缺失/结尾不成句/篇幅大幅缩短）→ 停止
            _floor_w = int(cfg["review"].get("word_floor", 8000))
            if not _polish_intact(new_full, full) or len(new_full) < 0.85 * len(full):
                print("[guard] 修复残缺（小节头缺失/结尾不成句/篇幅大幅缩短），停止循环")
                break
            full = new_full
            try:
                report = _do_check(full)
                cur = float(report.get("score") or 0)
                # 梗核心缺失不记为提升（模型把设定改丢了）——仅数字核心词强制
                if _idea_kws_f:
                    _num_f2 = [k for k in _idea_kws_f
                               if any(c.isdigit() or c in "一二三四五六七八九十百千万两" for c in k)]
                    miss_f2 = [k for k in _num_f2 if k not in full]
                    if miss_f2 and len(miss_f2) >= max(1, len(_num_f2) // 2):
                        cur = min(cur, best_score)  # 缺核心设定不记为提升
                if cur > best_score:
                    # 提升：先接受；若精炼把字数压到下限以下，确定性扩写补回（不再回滚精炼）
                    if count_words_zh(full) < _floor_w:
                        print(f"[stage] 补丁提升到 {cur} 分但仅 {count_words_zh(full)} 字"
                              f" < {_floor_w}，扩写补回字数…")
                        full = _pad_words(full, _floor_w)
                        report = _do_check(full)
                        cur = float(report.get("score") or 0)
                    best_full, best_score = full, max(cur, best_score)
                    best_report = report
                    fail_streak = 0
                    print(f"[stage] 复评 {cur} 分（接受，best {best_score}，"
                          f"{count_words_zh(full)} 字）")
                else:
                    fail_streak += 1
                    print(f"[stage] 复评 {cur} 分未提升（best {best_score}），回滚；"
                          f"连续 {fail_streak} 次")
                    full = best_full
                    report = _do_check(full)  # 重新体检：保证下一轮 issues 与回滚版本匹配
                    if fail_streak >= 2:
                        print("[guard] 连续2轮未提升，收敛到头，终止")
                        break
            except Exception as e:  # noqa: BLE001
                print(f"[stage] 复评异常，终止修复循环：{e}")
                break
        if float(report.get("score") or 0) < best_score:
            # 留优回填：`full` 换回最佳版本时，**体检结论必须一起换回来**。
            # 旧实现只回填 `full`，`report` 仍是「回滚前那一刻、对较差版本」的
            # 体检结论 → e2e11 实测 best 已达 72（达标线），报告却写
            # `score 70 / reached false`，4 轮修复与达标结论全部白做。
            full = best_full
            report = best_report
        if (report.get("mode") != "local-stats"
                and float(report.get("score") or 0) < floor):
            print(f"[warn] 终审 {report.get('score')} 分未达 {floor}"
                  f"（{'轮次用尽' if fix_rounds > 0 else '守卫终止'}，放行留痕）")
        report.setdefault("final_rounds", rnd_f)
        report.setdefault("score_floor", floor)
        # 终审结论必须落盘且写清"这是什么分"。以前只在日志里打一行"未达 72"，
        # report.json 完全没有终审分数（`final_review` 永远是 None），于是
        # 看板/复盘/交付验收读不到，人只能凭日志猜——而终审分是**内部质检分**，
        # 不是投稿线（投稿线看 market_lang / manuscript_audit），两者口径不同，
        # 混在一起会让"A 可投"的稿子看起来像"没通过"。
        _fs = float(report.get("score") or 0)
        report["final_review"] = {
            "score": report.get("score"),
            "score_best": round(best_score, 1) if best_score else None,
            "threshold": floor,
            "reached": bool(_fs >= floor),
            "rounds": rnd_f,
            "note": "内部质检分（卡点/连续性/文风/安全），非投稿线；投稿判定见 market_lang",
        }

    report["word_stats"] = {"total": count_words_zh(full), "chapters": stats}
    report["word_gate"] = wg

    # --- 全稿读者评分团：低于阈值打回整体重写（最多 max_reader_rounds 轮）---
    full_review = None
    if cfg["review"]["enabled"] and cfg["review"].get("reader_enabled", True):
        direction = (project.load_json("concept.json") or {}).get("direction", "")
        panel = agents.ReaderPanel(resolve_llm(cfg, llm, "panel"), direction, int(cfg["review"].get("reader_panel", 3)))
        threshold = float(cfg["review"].get("score_threshold", 7.5))
        max_r = int(cfg["review"].get("max_reader_rounds", 2))
        fctx = {"TITLE": title, "SCOPE": "（全稿）"}
        print(f"[stage] 全稿读者评分团（{panel.names}，阈值 {threshold}）…")
        scores = panel.score(fctx, full, 1)
        avg = statistics.mean(s["score"] for s in scores)
        rnd = 1
        while avg < threshold and rnd < max_r:
            print(f"[stage] 读者均分 {avg:.2f} < {threshold}，整稿打回重写（{rnd}/{max_r}）…")
            writer_llm2 = resolve_llm(cfg, llm, "writer")
            sys_f, user_f = render_prompt(
                "polish_final",
                IDEA=(project.load_json("concept.json") or {}).get("core_trope", ""),
                ISSUES="读者评分团反馈（逐条吸收，保持情节骨架）：\n" + agents.fmt_reader_feedback(scores) +
                "\n\n**字数铁律：重写后全稿不得低于原稿 90%（当前 " + str(count_words_zh(full)) + " 字），吸收意见的同时必须补足等价内容，严禁越改越短。**",
                FULL_TEXT=full,
                GENRE_GUIDE=load_genre_guide(cfg, (project.load_json("concept.json") or {}).get("direction", "")),
                STYLE_SAMPLE=_style_sample_f,
            )
            new_full = writer_llm2.chat(sys_f, user_f).strip()
            if count_words_zh(new_full) < 0.9 * count_words_zh(full) or not _polish_intact(new_full, full):
                print("[guard] 整稿重写长度不足原稿 90%，疑似坍缩，保留原稿")
            else:
                full = new_full
            rnd += 1
            scores = panel.score(fctx, full, rnd)
            avg = statistics.mean(s["score"] for s in scores)
        full_review = {
            "reader_rounds": rnd,
            "reader_avg": round(avg, 2),
            "threshold": threshold,
            "scores": {s.get("persona", "?"): s["score"] for s in scores},
            "gate": "pass" if avg >= threshold else "maxed_reader",
        }
        print(f"[stage] 全稿读者终评：均分 {avg:.1f}（{full_review['gate']}）")
    # 终审修复/读者循环每轮允许 -10% 的压缩，多轮累计会把全文压回字数线下
    # （实测 8199→7986、8070→7533）——装配前再跑一次字数门兜底。
    # 只在下限方向兜底（防缩水）；超标方向不再重跑，否则两次 word_gate 各跑满
    # 轮次会把写手预算翻倍烧掉（实测 8 章 + 16 轮空转 = 2/3 预算）。
    _pre_fw = count_words_zh(full)
    _floor_w = int(cfg["review"].get("word_floor", 8000))
    # 预留下游清理损耗：`_final_clean` 会吃掉 0.3%–1.5% 的字（实测 24/72/40/93 字），
    # 若这里只按 floor 兜底，会出现"字数门 8021 判 pass → 清理后 7997 → 平台检查 fail"
    # （e2e5 实测差 3 个字被淘汰）。所以按 floor + 预留 补，判据仍是真 floor。
    _reserve = max(120, int(_floor_w * 0.02))
    if _pre_fw < _floor_w + _reserve:
        full, wg = word_gate(cfg, llm, project, full)
    # 装配前最终全局清理。是否保留小节标题取决于平台排版需要：
    # 知乎盐选要求纯数字节头（不加标题）；番茄/七猫的投稿排版稿要「NN 标题」，
    # 否则 typeset 切节后标题为空、排版稿退化（联动断点）。
    _plat_final = load_platform(pitch.get("platform", "zhihu"))
    _keep_titles = _plat_final.get("format") == "lead_numbered" and _plat_final.get("key") != "zhihu"
    _n_chapters = max(1, len(stats))
    _before_clean = count_words_zh(full)
    _n_sections_raw = len(_split_full_sections(full))
    full = _final_clean(full, keep_titles=_keep_titles, max_sections=_n_chapters + 2)
    # 清理后仍不达下限：清理本身吃掉的字只能靠补写拿回来。此处不再重新 _final_clean
    # （节头已规范化），word_gate 只会扩写既有小节，不会新增节头。
    _post_clean_w = count_words_zh(full)
    if _post_clean_w < _floor_w:
        print(f"[warn] 装配清理后字数不足：{_post_clean_w} < {_floor_w}，再补一轮字数")
        full, wg = word_gate(cfg, llm, project, full)
        _post_clean_w = count_words_zh(full)
    stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
              for s in _split_full_sections(full)] or stats)
    # 清理吃字告警：_final_clean 静默丢正文是历史事故（48.6% 字数蒸发）的根因，
    # 明细已落 report["word_flow"]，这里只在异常时打一行日志让跑批的人立刻看见。
    _lost_clean = int(_before_clean) - count_words_zh(full)
    if _lost_clean > max(200, int(_before_clean * 0.05)):
        print(f"[warn] 装配清理丢字偏多：{_before_clean} → {count_words_zh(full)}"
              f"（-{_lost_clean}，原始节数 {_n_sections_raw}）")
    report["review"] = {
        "chapters": project.meta.get("reviews", {}),
        "full_story": full_review,
    }

    plat = load_platform(pitch.get("platform", "zhihu"))
    # 对话引号全平台统一为「」——必须作用在 full 上（md 与 txt 共用同一份正文），
    # 否则投稿用的 成稿.txt 会保留写手原始的 “”，出现"导语用「」、正文用 “”"
    # 一份稿子两种引号（实测 146 对未转换）。
    from .publish import normalize_quotes

    full = normalize_quotes(full)

    # ---- 终审修复后确定性门禁复查（最关键的一处联动断点）----
    # 章级三道确定性门（思考泄漏/禁用道具/语言质检）在写章阶段就跑完了，但
    # 之后 stage_polish 还有两条**整稿重写**路径：终审修复循环（polish_final 整稿
    # 重写 + 补丁）和全稿读者团打回重写。它们都是 LLM 生成，会把已经清掉的禁用
    # 道具、英文思维链原样写回来，而全流程没有任何门再检查。
    # 实测：20261025_233236 项目章级 ch08 复查已清干净，成稿里仍有「欠条」；
    # 20261025_211149 项目 8 章 12 万字混入 912 行英文思维链，report 里毫无记录。
    # 因此在装配落盘前，对 full 再跑一次全套确定性门禁并串联修复。
    _rc_final: dict = {}
    try:
        _wfix = agents.WriterAgent(resolve_llm(cfg, llm, "writer"))
        _fctx = {"TITLE": title, "DIRECTION": (project.load_json("concept.json") or {}).get("direction", "")}
        full, _rc_final = agents.deterministic_recheck(cfg, _wfix, _fctx, full, full_scope=True)
        if _rc_final.get("rounds", 1) > 1:
            print(f"[stage] 成稿确定性复查：修复 {_rc_final['rounds'] - 1} 轮"
                  f"（{'通过' if _rc_final.get('pass') else '仍有残留'}）")
    except Exception as e:  # noqa: BLE001
        print(f"[stage] 成稿确定性复查失败（{type(e).__name__}），继续落盘并留痕：{e}")
        _rc_final = {"pass": False, "error": f"{type(e).__name__}: {e}"}
    report["deterministic_recheck_final"] = _rc_final

    total_words = count_words_zh(full)
    # 全稿确定性兜底复查：润色/补丁/读者重写都可能把道具或思考泄漏写回来。
    # 章级已在落盘前复查过；这里再兜一层并落 report，避免"成稿带禁用道具却无人知晓"。
    from .langqa import scan_banned as _scan_banned
    from .langqa import think_leak as _think_leak

    _bf = _scan_banned(full, cfg)
    if not _bf["pass"]:
        report["banned_props_final"] = [str(h.get("term")) for h in _bf["hits"]]
        print(f"[warn] 成稿仍含用户禁用道具：{'、'.join(report['banned_props_final'])}"
              "（已记入 report.json，建议人工替换后再投稿）")
    # 思考泄漏全稿兜底：历史事故里 8 章 12 万字混入 912 行英文思维链，章级没拦住时
    # 必须在成稿落盘前至少留痕（不静默交付一篇"分析稿"）。
    if _think_leak(full):
        report["think_leak_final"] = True
        print("[warn] 成稿疑似思考泄漏（英文思维链/提纲混入正文），已记入 report.json")
    concept = project.load_json("concept.json") or {}
    direction = concept.get("direction", "")
    lead_rec = project.load_json("lead.json") or {}
    lead_block = f"**导语**：{lead_rec.get('text', '')}\n\n" if lead_rec.get("text") else ""
    final_md = (
        f"# {title}\n\n"
        f"> 平台：{plat['name']} | 字数：{total_words} | 题材：{direction}"
        + (f" | 读者团均分：{full_review['reader_avg']}" if full_review else "")
        + f"\n\n**一句话故事**：{pitch.get('logline', '')}\n\n{lead_block}---\n\n{full}\n"
    )
    # 交付前最后一次节长均衡：`balance`（上面那次）跑在**全稿读者团重写之前**，
    # 而读者团打回时会整稿重写（"不得低于原稿 90%"），把已经调好的节长比例重新
    # 拉歪——e2e14 实测 balance 报 ratio_after 1.79，而落到 成稿.txt 里是 2.08
    # （01 节被重写撑到 1700 字），体检直接判 C 不可投。
    # 这一步只在**超线时**动手（达标则零消耗），判据仍是 MARKET_TARGET.section_ratio_max。
    _bal2_rep = None
    if (cfg.get("review") or {}).get("lang_balance_enabled", True) and not isinstance(llm, MockLLM):
        try:
            from .language import MARKET_TARGET as _MT2
            from .language import balance as _lang_balance2
            from .language import section_lengths as _sec_lens

            _lens2 = [w for _no, w in _sec_lens(full) if w > 0]
            _r2 = round(max(_lens2) / max(min(_lens2), 1), 2) if _lens2 else 1.0
            if _r2 > _MT2["section_ratio_max"]:
                print(f"[balance] 交付前节长极差 {_r2}× 超线，再校一次…")
                full, _bal2_rep = _lang_balance2(
                    cfg, llm, project, full,
                    max_edits=int((cfg.get("review") or {}).get("lang_balance_max_edits", 4)),
                    style_sample=_style_sample_of(cfg, project),
                )
                if _bal2_rep is not None:
                    _bal2_rep["ratio_before_all"] = _r2
                stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                          for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001 —— 与上一次同策略：失败留痕不毁稿
            _bal2_rep = {"enabled": True, "error": f"{type(e).__name__}: {e}"}
            print(f"[balance] ⚠ 交付前节长均衡跳过（{type(e).__name__}: {e}）")

    # 交付前最后一次补对白：与节长同理——终审整体重写/上一轮 balance 都可能把对白
    # 稀释回阈值以下。只在**低于阈值时**动手，达标零消耗。
    _dia2_rep = None
    if (cfg.get("review") or {}).get("lang_dialog_enabled", True) and not isinstance(llm, MockLLM):
        try:
            from .language import MARKET_TARGET as _MT3
            from .language import dialog_raise as _lang_dialog2
            from .language import dialog_ratio as _dratio

            if _dratio(full) < float(_MT3["dialog_ratio_min"]):
                print(f"[dialog] 交付前对白占比 {_dratio(full):.3f} 低于线，再补一次…")
                full, _dia2_rep = _lang_dialog2(
                    cfg, llm, project, full,
                    max_edits=int((cfg.get("review") or {}).get("lang_dialog_max_edits", 3)),
                    style_sample=_style_sample_of(cfg, project),
                )
                stats = ([{"no": s["no"], "words": count_words_zh(s["text"])}
                          for s in _split_full_sections(full)] or stats)
        except Exception as e:  # noqa: BLE001
            _dia2_rep = {"enabled": True, "error": f"{type(e).__name__}: {e}"}
            print(f"[dialog] ⚠ 交付前补对白跳过（{type(e).__name__}: {e}）")

    # 平台成稿格式：导语 + 01/02/03 编号小节（assemble 已统一小节头为 NN 标题）
    plat_fmt = plat.get("format", "chapters")
    if plat_fmt == "lead_numbered" and lead_rec.get("text"):
        sections = [f"{title}", "", "导语：", lead_rec["text"], ""]
        sections.append(full)   # full 来自 assemble：每节首行已是「NN 标题」
        submit_text = "\n\n".join(sections)
    else:
        submit_text = f"{title}\n\n{full}"
    # 导语来自 lead.json、正文来自 full，两段分别拼装；统一在这里收口，
    # 保证 md/txt 全文（含导语）引号一致，不再出现"导语「」、正文 “”"。
    final_md = normalize_quotes(final_md)
    submit_text = normalize_quotes(submit_text)
    if plat.get("key") == "zhihu":
        from .publish import zhihu_format

        final_md = zhihu_format(final_md)
    project.save_text("成稿.md", final_md, subdir="final")
    project.save_text("成稿.txt", submit_text, subdir="final")
    report["final_words"] = total_words
    # 平台字数判据必须用**投稿全文**（标题 + 导语 + 正文），不能用正文净字数：
    # 实测 e2e5 正文 7997 字被判 fail，而落到 成稿.txt 里是 8430 字（导语 403 字），
    # 平台看到的稿子明明达标。两个口径混用会让成稿在字数边缘被误淘汰。
    submit_words = count_words_zh(submit_text)
    report["submit_words"] = submit_words
    # 落盘字数 vs 装配字数：两者差异过大说明清理环节吃掉了正文（历史事故 48.6%），
    # 显式落 report 供看板/复盘发现，而不是靠人肉比对。
    report["word_flow"] = {
        "assembled": int(_pre_fw),
        "before_final_clean": int(_before_clean),
        "final": total_words,
        "lost_in_clean": max(0, int(_before_clean) - int(total_words)),
        "n_sections_raw": _n_sections_raw,
        "n_sections_after_clean": len(stats),
    }
    report["platform_check"] = {
        "min_words": plat["min_words"],
        "max_words": int(cfg["review"].get("word_ceiling", 12000)),
        "pass": (plat["min_words"] <= submit_words
                 <= int(cfg["review"].get("word_ceiling", 12000))),
        "submit_words": submit_words,
    }
    # 禁用道具放行留痕：选题自带被禁道具时本篇临时放行，必须在 report 里能查到，
    # 否则复盘会误以为"这稿违反了禁用表"。
    if project.meta.get("banned_exempt"):
        report["banned_exempt"] = list(project.meta.get("banned_exempt") or [])
    # 语言打磨留痕：改了几节、回滚几节、缺陷分从多少降到多少、确定性删了几段重复。
    # 落 report 而不是只 print —— 复盘要看的是"这一篇的语言病到底治没治"，
    # 而看板/复盘都只读 report.json。
    if _lang_rep is not None:
        report["lang_polish"] = {
            "enabled": True,
            "before": int((_lang_rep.get("before") or {}).get("score", 0)),
            "after": int((_lang_rep.get("after") or {}).get("score", 0)),
            "sections_rewritten": _lang_rep.get("sections_rewritten", 0),
            "sections_rejected": _lang_rep.get("sections_rejected", 0),
            "deduped_paras": _lang_rep.get("deduped", 0),
            "rounds": _lang_rep.get("rounds", []),
        }
    elif _lang_err:
        # 开了却没跑成：必须让 report 里看得出"语言打磨失效"，否则下游
        # （看板/复盘/淘汰）会把"没打磨过"当成"打磨通过"。
        report["lang_polish"] = {"enabled": True, "pass": False, "error": _lang_err}
    else:
        report["lang_polish"] = {"enabled": False}
    if _repair_rep is not None:
        report["lang_repair"] = _repair_rep
    if _repair_final is not None:
        report["lang_repair_final"] = _repair_final
    if _conv_rep is not None:
        report["lang_converge"] = _conv_rep
    if _bal_rep is not None:
        report["lang_balance"] = _bal_rep
    if _bal2_rep is not None:
        report["lang_balance_final"] = _bal2_rep
    if _dia_rep is not None:
        report["lang_dialog"] = _dia_rep
    if _dia2_rep is not None:
        report["lang_dialog_final"] = _dia2_rep
    # 市场语言门槛终检（确定性，零 token）：用与投稿体检同一套阈值
    # （studio/language.py:MARKET_TARGET）判这一篇能不能投，不再有"流水线认为达标、
    # 体检判不可投"的分裂。不通过不淘汰稿子，但必须留痕——下游 CLI/看板以此
    # 标 gate_status=below_market，避免 C 级稿静悄悄躺在工作台里。
    try:
        from . import language as _L

        _mf = _L.language_fails(full)
        _md = _L.dense(_L.defect_score(full), len(full))
        report["market_lang"] = {"pass": not _mf, "fails": _mf,
                                 "dense": {k: round(v, 2) for k, v in _md.items()}}
        if _mf:
            print(f"[market] 语言未达投稿线：{'；'.join(_mf)}")
    except Exception as _me:  # noqa: BLE001 —— 门槛自检失败不该毁掉整篇成稿
        report["market_lang"] = {"pass": False, "error": f"{type(_me).__name__}: {_me}"}
    from .langqa import genre_tags as _genre_tags
    from .langqa import scan as lang_scan

    # 扫描**投稿全文**（标题+导语+正文）而不是 `full`（正文）：平台看到、读者打开
    # 的是 成稿.txt，体检工具 `tools/manuscript_audit.py` 扫的也是它。两处扫不同
    # 文本时会出现同篇稿子"report 里 langqa_full.pass=true、体检报 AI 味警告"
    # 的口径分裂（e2e8/e2e9/e2e11 实测）。导语里的口癖同样是读者的第一印象。
    # 题材标签必须先落 report：`audit_text()` / `verdict` / 看板读 report 拿题材，
    # 缺了它古言规则（现代词混入=high）在生产里从未生效（2026-10-26 古言实测发现
    # `report["direction"]` 一直是 None）。
    _lq_dir = (project.load_json("concept.json") or {}).get("direction", "")
    report["direction"] = _lq_dir
    lq_full = lang_scan(submit_text, genres=_genre_tags(_lq_dir))
    report["langqa_full"] = {"pass": lq_full["pass"], "hits": len(lq_full["hits"]),
                             "top": [h["label"] for h in lq_full["hits"][:8]]}
    if not lq_full["pass"]:
        print(f"[langqa] 全稿语言扫描：{len(lq_full['hits'])} 处命中（详见 report.json）")
    # 伏笔账本体检：bible.foreshadows 以前只进提示词、从不校验，收没收无人查。
    # 这里按「超期未收 / 埋而未收 / 回收章无痕迹」三条出账，warn 级不阻断发布
    # （短篇伏笔常靠意象回收，硬门禁会逼作者写死"关键词"，反而伤文）。
    try:
        _fs_written = {int(s["no"]): s.get("text", "") for s in _split_full_sections(full)}
        _fs = _foreshadow.audit(bible, _fs_written, total=len(stats))
        report["foreshadow_audit"] = {
            "total": _fs["total"], "open": _fs["open"], "paid": _fs["paid"],
            "overdue": _fs["overdue"], "missing_trace": _fs["missing_trace"],
            "ok": _fs["ok"], "issues": _fs["issues"][:8],
        }
        if _fs["issues"]:
            print(f"[foreshadow] 伏笔账本：{len(_fs['issues'])} 项待处理"
                  f"（超期 {len(_fs['overdue'])} / 无回收痕迹 {len(_fs['missing_trace'])}）")
    except Exception as e:  # noqa: BLE001
        print(f"[foreshadow] 伏笔体检跳过：{type(e).__name__}: {e}")
    report["usage"] = usage_summary()
    # 评审卡全稿汇总：逐章 rubric 只在 reviews/chNN.json 里，报告层看不见。
    # 这里把各章汇总成一份"交付视图"——看板详情页与复盘都读 report.json，
    # 缺了这段就等于"评审卡只写给开发者看"，业务侧仍然只看到单一分数。
    try:
        _rub = review_mod.rollup(cfg, project)
        report["rubric_rollup"] = _rub
        if _rub.get("chapters"):
            print(f"[review] 评审卡汇总：{_rub['chapters']} 章 · "
                  f"平均覆盖 {_rub['coverage']:.0%} · 交付 "
                  f"{_rub['delivery_counts']}")
    except Exception as e:  # noqa: BLE001
        report["rubric_rollup_error"] = f"{type(e).__name__}: {e}"[:200]
    # 成稿账务词密度留痕（大纲阶段已硬拦，此处兜底统计，warn 级别）。
    # 必须在 save_json **之​前**算并写进 report——旧实现先落盘再补字段，
    # 磁盘上的 report.json 永远读不到这个告警（缺陷 P1-9）。
    _acc = _trope_reuse_check(full)
    if _acc:
        print(f"[warn] 成稿账务词族密度偏高（{_acc}）——下轮 run 将在大纲阶段拦截，建议写手改道具")
        report["account_trope_warn"] = _acc
    if not report["platform_check"]["pass"]:
        print(f"[warn] 成稿字数越界（正文 {total_words} 字 / 投稿全文 {submit_words} 字，"
              f"平台下限 {plat['min_words']}，"
              f"配置上限 {report['platform_check']['max_words']}）")
    # 生产模式必须落盘：之前 report 里根本没有 mode 字段，看板/复盘读出来永远是 null，
    # 事后无法分辨"这稿是 agent 写的还是 hybrid 拼的"。
    report["mode"] = (cfg.get("pipeline") or {}).get("mode") or "agent"
    # 主编评分的**项目级**汇总：逐章 review_score 只写在 reviews/chNN.json 与
    # meta.reviews[*] 里，report.json 从来没有顶层分数。后果是验收工具/看板
    # 站在项目层读 report 时永远拿到 None（体检报告里那行"主编评分 实测 None"
    # 就是这么来的，跟模型分数无关，纯属口径缺失）。
    try:
        _rvs = [float(v["review_score"]) for v in (project.meta.get("reviews") or {}).values()
                if isinstance(v, dict) and isinstance(v.get("review_score"), (int, float))]
        _ravg = round(sum(_rvs) / len(_rvs), 2) if _rvs else None
        report["review_score"] = _ravg
        report["review_score_min"] = round(min(_rvs), 2) if _rvs else None
        report["review_score_src"] = "rubric_mean" if _rvs else ""
        report["review_score_n"] = len(_rvs)
        _rub = report.get("rubric_rollup") or {}
        if _ravg is not None:
            print(f"[review] 主编评分（评分卡折算）章节均值 {_ravg} / 最低 "
                  f"{report['review_score_min']}（{len(_rvs)} 章）")
    except Exception as e:  # noqa: BLE001
        report["review_score_error"] = f"{type(e).__name__}: {e}"[:200]
    # 投稿判定（单一权威源 studio/verdict.py）：把并列的 platform_check /
    # market_lang / review_score 合成一个 {ready, grade, reasons}，看板与 CLI
    # 都读它，避免"看板说通过、体检说 C 不可投、主编给 7.8"三个口径打架。
    try:
        from . import verdict as _vd
        from . import audit as _au

        # 措辞级瑕疵（改得动、不该拦投稿）：AI 味口癖/黑名单命中的 mid 项、
        # 成稿残留的禁用道具。它们不进"硬线"，但必须让结论从 A 降到 B——
        # 否则体检说 B、投稿判定说 A，用户又要在两个结论之间猜。
        _soft: list[str] = []
        _lq = report.get("langqa_full") or {}
        if _lq.get("pass") is False:
            _soft.append("AI味/黑名单：" + "、".join(_lq.get("top") or ["（未列出）"]))
        if report.get("banned_props_final"):
            _soft.append("成稿残留禁用道具："
                         + "、".join(str(x) for x in report["banned_props_final"]))
        # 体检（与 `tools/manuscript_audit.py` 完全同一份实现）：它的硬失败项
        # 直接进投稿判定的硬线，级别/失败项/警告项落 `report["audit"]`。
        # 这一步是"检"与"判"合流的唯一点，别再在别处各算一套。
        _aud = _au.audit_text(submit_text, report, direction=_lq_dir)
        report["audit"] = {"grade": _aud["grade"], "fails": _aud["fails"],
                           "warns": _aud["warns"]}
        # 去重：字数、语言密度、平台检查三项已经分别由 platform_check / market_lang
        # 报过，体检再报一次会让 `reasons` 变成同一句话两遍（实测 mock 跑出 7 条
        # 原因里 3 条是重复的）。体检独有的是结构/节奏/规范三类。
        _dup = ("A结构/字数", "E门禁/平台检查")
        _audit_fails = [f for f in _aud["fails"]
                        if f not in _dup and not f.startswith("B语言/")]
        # 书名卖点分：签约第一道门槛，随 report 落盘并进投稿结论。
        _pitch_r = project.load_json("pitch.json") or {}
        _title = str(_pitch_r.get("title") or "")
        report["title"] = _title
        report["title_score"] = int(_pitch_r.get("title_score")
                                    if _pitch_r.get("title_score") is not None
                                    else _TG.score(_title))
        report["title_family"] = (_pitch_r.get("title_family")
                                  or _TG.family(_title))
        _sub = _vd.submission(
            platform_check=report.get("platform_check"),
            market_lang=report.get("market_lang"),
            review_score=report.get("review_score"),
            words=report.get("final_words"),
            audit_fails=_audit_fails,
            soft_issues=_soft,
            title_score=report["title_score"],
        )
        report["submission"] = _sub
        print(f"[audit] {_aud['grade']}"
              + (f" | 失败项 {'、'.join(_aud['fails'])}" if _aud["fails"] else "")
              + (f" | 警告项 {'、'.join(_aud['warns'])}" if _aud["warns"] else ""))
        print(f"[submission] {_vd.brief(_sub)}")
    except Exception as e:  # noqa: BLE001
        report["submission_error"] = f"{type(e).__name__}: {e}"[:200]
    project.save_json("report.json", report)

    # --- 自动沉淀素材入库（mock 产出不入库）---
    # 默认关（与 config.DEFAULT_CONFIG 一致）：成稿回灌素材库会把机器生成的稿子
    # 再检索回来（自回声），下一篇越写越像自己。要反哺必须显式开启。
    if cfg.get("materials", {}).get("auto_deposit", False) and not isinstance(llm, MockLLM):
        try:
            from . import material as material_mod

            conv_llm = (resolve_llm(cfg, llm, "light")
                        if cfg.get("materials", {}).get("convert_hotspots", True) else None)
            dep = material_mod.deposit_from_project(cfg, project, convert_llm=conv_llm)
            print(f"[material] 素材沉淀：新增 {dep['added']} 条（跳过重复 {dep['skipped']}）")
            # 自产内容不进知识库（用户决策:防自回声污染——库只收外部爆款拆解与人工精选素材）
            if dep["added"] > 0 and cfg.get("materials", {}).get("auto_curate", True):
                c_llm = resolve_llm(cfg, llm, "light")
                cres = material_mod.curate(
                    cfg, c_llm, batch_size=int(cfg.get("materials", {}).get("curate_batch", 15))
                )
                print(f"[material] 整理 Agent：评估 {cres['checked']} 条，"
                      f"high {cres.get('high', 0)} / mid {cres.get('mid', 0)} / "
                      f"low {cres.get('low', 0)}（删除 {cres['removed']}）")
            # 反思：归纳重复模式生成下一篇的差异化守则（Generative Agents 反思机制）
            try:
                from . import memory as memory_mod

                rr = memory_mod.reflect(cfg, resolve_llm(cfg, llm, "light"),
                                        tropes=recent_tropes(cfg, 8))
                print(f"[memory] 反思守则更新：{rr.get('n_rules', 0)} 条")
            except Exception as e:  # noqa: BLE001
                print(f"[memory] 反思失败（不影响成稿）：{e}")
        except Exception as e:  # noqa: BLE001
            print(f"[material] 素材沉淀失败（不影响成稿）：{e}")
    project.set_stage("final", f"{total_words}字")
    print(f"[stage] 成稿 {total_words} 字 → final/成稿.md（体检报告 report.json）")
    return report
