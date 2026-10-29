# -*- coding: utf-8 -*-
"""题材知识库：按梗库方向自动搜集公开资料，蒸馏成"题材知识卡"。

- 搜索引擎：Bing（cn/com 双通道，无需 Key）；百度做兜底，全部失败可降级为 LLM 内化生成
- 产物：data/knowledge/<direction_id>.json，供 pitch/draft 注入真实细节素材
- 命令：python -m studio kb collect 彩礼纠纷 / kb list / kb show 彩礼纠纷
"""
from __future__ import annotations

import html as _html
import json
import re
import time
from pathlib import Path

import requests

from .config import ROOT, data_path
from . import kb_lint as _L
from .llm import BaseLLM
from .prompts import render_prompt
from .util import now_ts

KB_DIR = ROOT / "data" / "knowledge"
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ---------------------------------------------------------------------------
# 梗库方向元数据 / 卡片存取
# ---------------------------------------------------------------------------

def direction_meta(direction: str) -> dict | None:
    """按名称或 id 取梗库方向元数据；复合方向（A×B，如 修仙×短剧改编）取第一个合法部分。

    风格层（如"短剧改编"，非梗库方向）会被跳过，使知识卡基于主题材（修仙）。
    未知方向返回 **None**（不再 SystemExit）：CLI 允许 `--direction` 传自由文本
    （如「修仙」），变成 SystemExit 会在选题后直接崩掉整条 run（缺陷 P1-4）。
    调用方按"无卡 + LLM 内化/联网兜底"降级处理。
    """
    tropes = json.loads(data_path("tropes.json").read_text(encoding="utf-8"))["directions"]
    candidates = [direction] + [p.strip() for p in re.split(r"[×·+*]", direction or "") if p.strip()]
    for c in candidates:
        for d in tropes:
            if c in (d["name"], d["id"]):
                return d
    return None


def card_path(direction: str) -> Path | None:
    meta = direction_meta(direction)
    return KB_DIR / f"{meta['id']}.json" if meta else None


_CARD_CACHE: dict[str, tuple[float, dict]] = {}


def load_card(direction: str) -> dict | None:
    """读题材知识卡（带 mtime 缓存）。

    缓存是必需的不是优化：语言打磨会按小节反复取卡（每节一次），不缓存就是
    每节重新解析十几 KB JSON。用 mtime 做键，`kb collect` 重写卡片后自动失效。
    """
    p = card_path(direction)
    if p is None or not p.exists():
        return None
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _CARD_CACHE.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    card = json.loads(p.read_text(encoding="utf-8"))
    _CARD_CACHE[str(p)] = (mt, card)
    return card


def list_cards() -> list[dict]:
    if not KB_DIR.exists():
        return []
    out = []
    for p in sorted(KB_DIR.glob("*.json")):
        # 下划线开头的是元数据/范例库（如 _craft.json 语言技法范例），不是题材卡
        if p.name.startswith("_"):
            continue
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
            out.append(
                {
                    "id": card.get("id", p.stem),
                    "direction": card.get("direction", p.stem),
                    "collected_at": card.get("collected_at", ""),
                    "sources": len(card.get("cases", [])),
                    "facts": len(card.get("facts", [])),
                }
            )
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# 搜索与抓取
# ---------------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s).strip()


def _clean_title(t: str, url: str) -> str:
    t = _strip_tags(t or "")
    t = re.sub(r"https?://\S+", "", t).strip(" |·-")
    return t[:80] or url[:60]


def search_bing(query: str, n: int = 8) -> list[dict]:
    """Bing 网页搜索（cn/com 双通道），返回 [{title,url,snippet}]。"""
    for host in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            r = requests.get(
                host, params={"q": query, "count": n + 2}, headers=_UA, timeout=12
            )
            blocks = re.split(r'<li class="b_algo"', r.text)[1:]
            out = []
            for b in blocks:
                m = re.search(r'href="(https?://[^"]+)"', b)
                if not m:
                    continue
                url = m.group(1)
                if any(s in url for s in ("bing.com", "microsoft.com", "go.micro")):
                    continue
                h2 = re.search(r"<h2[^>]*>(.*?)</h2>", b, re.S)
                title = _clean_title(h2.group(1) if h2 else "", url)
                if not title:
                    am = re.search(r"<a[^>]*>(.*?)</a>", b, re.S)
                    title = _clean_title(am.group(1) if am else "", url)
                pm = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
                snippet = _strip_tags(pm.group(1))[:280] if pm else ""
                out.append({"title": title, "url": url, "snippet": snippet})
                if len(out) >= n:
                    break
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return []


def search_baidu(query: str, n: int = 8) -> list[dict]:
    """百度网页搜索兜底（风控严格，失败返回空）。"""
    try:
        r = requests.get(
            "https://www.baidu.com/s",
            params={"wd": query, "rn": n + 2},
            headers={**_UA, "Cookie": "BAIDUID=0"},
            timeout=12,
        )
        items = re.findall(
            r'<h3[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', r.text, re.S
        )
        out = []
        for url, title in items[:n]:
            out.append({"title": _clean_title(title, url), "url": url, "snippet": ""})
        return out
    except Exception:  # noqa: BLE001
        return []


def fetch_text(url: str, limit: int = 1500) -> str:
    """抓取网页正文文本（粗提取，失败返回空串）。"""
    try:
        r = requests.get(url, headers=_UA, timeout=12)
        ct = r.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct:
            return ""
        t = r.text
        t = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = _html.unescape(t)
        t = re.sub(r"\s+", " ", t).strip()
        return t[:limit]
    except Exception:  # noqa: BLE001
        return ""


def gen_queries(direction: str) -> list[str]:
    """按方向元数据生成搜索问句（本地模板，不耗 LLM）。

    未知方向（非梗库方向，如自由文本「修仙」）直接返回空表——
    调用方走"无卡 + 核心梗联网兜底"，而不是在这里崩（缺陷 P1-4）。
    """
    meta = direction_meta(direction)
    if not meta:
        return []
    kws = [k for k in meta.get("keywords", []) if len(k.strip()) >= 2][:3]
    title_ex = (meta.get("title_examples") or [""])[0][:18]
    qs = [
        f"{meta['name']} 真实案例",
        f"{meta['name']} 小说 爆款 拆解",
        f"{meta['name']} 写作 素材 细节",
    ]
    if len(kws) >= 2:
        qs.append(f"{kws[0]} {kws[1]} 真实故事")
    if title_ex:
        qs.append(f"{title_ex} 同类故事 套路")
    return qs


def collect_material(cfg: dict, direction: str) -> list[dict]:
    """搜索 + 抓取正文，返回素材块列表 [{title,url,text}]。"""
    kcfg = cfg["knowledge"]
    max_pages = int(kcfg.get("max_pages", 8))
    page_chars = int(kcfg.get("page_chars", 1500))
    seen_urls: set[str] = set()
    results: list[dict] = []
    for q in gen_queries(direction):
        hits = search_bing(q) or search_baidu(q)
        print(f"[kb] 搜索「{q}」→ {len(hits)} 条")
        for h in hits:
            if h["url"] in seen_urls:
                continue
            seen_urls.add(h["url"])
            text = h.get("snippet", "")
            body = fetch_text(h["url"], page_chars)
            if body:
                text = (text + " " + body).strip()
            if len(text) < 60:  # 无效页面
                continue
            results.append({"title": h["title"], "url": h["url"], "text": text[: page_chars + 300]})
            if len(results) >= max_pages:
                return results
        time.sleep(0.5)  # 克制抓取频率
    return results


# ---------------------------------------------------------------------------
# 蒸馏成知识卡
# ---------------------------------------------------------------------------

def build_card(cfg: dict, llm: BaseLLM, direction: str, force: bool = False) -> dict:
    """为某题材方向构建知识卡：搜索蒸馏，失败降级为 LLM 内化生成。"""
    if not force:
        existing = load_card(direction)
        if existing:
            return existing
    meta = direction_meta(direction)
    if not meta:
        # 自由文本方向（如「修仙」/「校园悬疑」）：梗库里没有元数据，
        # 不能在这里 SystemExit（会崩掉整条 run）。返回一张"未收录"卡，
        # 由 stage_kb 的「核心梗联网兜底」路径补真实细节。
        print(f"[kb] 方向「{direction}」不在梗库中（非 42 个预设方向），跳过题材知识卡，"
              "改用核心梗联网兜底")
        return {"id": "", "direction": direction, "summary": "", "facts": [], "cases": [],
                "collected_at": now_ts(), "source_count": 0, "fallback": True,
                "unlisted_direction": True}
    print(f"[kb] 构建「{meta['name']}」题材知识卡 …")
    material = collect_material(cfg, direction)
    if material:
        blocks = "\n\n".join(
            f"【{i+1}】{m['title']} | {m['url']}\n{m['text']}"
            for i, m in enumerate(material)
        )
        print(f"[kb] 搜集到 {len(material)} 页资料，共 {len(blocks)} 字，蒸馏中 …")
    else:
        blocks = "（网络搜集失败，请基于你掌握的真实生活常识生成，涉及数字标注'行情参考'）"
        print("[kb] 网络搜集失败，降级为 LLM 内化生成")
        if not cfg["knowledge"].get("allow_llm_fallback", True):
            raise SystemExit("知识搜集失败且 allow_llm_fallback=False")

    system, user = render_prompt(
        "kb_distill",
        DIRECTION=meta["name"],
        CONFLICT=meta["core_conflict"],
        EMOTION=meta["emotion"],
        MATERIAL=blocks[:16000],
    )
    card = llm.chat_json(system, user)
    card["id"] = meta["id"]
    card["direction"] = meta["name"]
    card["collected_at"] = now_ts()
    card["source_count"] = len(material)
    card["fallback"] = len(material) == 0
    KB_DIR.mkdir(parents=True, exist_ok=True)
    card_path(direction).write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[kb] 已保存 → {card_path(direction)}")
    return card


def digest(card: dict | None, budget: int = 2200) -> str:
    """无章节上下文时的通用摘录（选题 / pitch 阶段用）。

    保留这个入口是因为 pitch 阶段还没有章节，拿不到"本章要写什么"做相关性检索。
    内部直接复用 `digest_for`，所以 `hooks` / `cases` 两个字段在这里也终于能被读到
    （旧实现只拼 summary/facts/scenes/dialogues/pitfalls/redlines，把花钱蒸馏出来的
    钩子和爆款案例整段丢掉，是实打实的死数据）。
    """
    text, _keys = digest_for(card, query="", budget=budget)
    return text


def _card_items(card: dict) -> list[dict]:
    """把知识卡摊平成条目列表 [{sec, text, key}]。

    key 用于跨章去重（同一章用过的事实，下一章不再喂）。
    """
    out: list[dict] = []
    summary = str(card.get("summary") or "").strip()
    if summary:
        out.append({"sec": "要诀", "text": summary, "key": "summary"})
    plan = [
        ("真实细节/钱数行情", "facts"),
        ("可写场景", "scenes"),
        ("对话语气参考", "dialogues"),
        ("开头钩子", "hooks"),
        ("读者雷点", "pitfalls"),
        ("安全红线", "redlines"),
    ]
    for head, field in plan:
        for i, it in enumerate(card.get(field) or []):
            t = str(it).strip()
            if t:
                out.append({"sec": head, "text": t, "key": f"{field}:{i}"})
    # cases 一直是死字段（digest 从没读过）：卡片花了 token 蒸馏出的爆款案例
    # 与「可借鉴点」从来没进过任何提示词。这里纳入，格式「标题（可借鉴点）」。
    for i, c in enumerate(card.get("cases") or []):
        if not isinstance(c, dict):
            # 纯字符串案例（人工补写时常这么写）也要能用，不能因为格式就丢掉
            t = str(c).strip()
            if t:
                out.append({"sec": "同类爆款案例", "text": t, "key": f"cases:{i}"})
            continue
        # 键名容错：LLM 蒸馏出的卡用 title/takeaway，人工补写的常用
        # pattern/beat/why 或 name/idea。只认一种写法等于把另一种当空。
        def _pick(*names: str) -> str:
            for n in names:
                v = c.get(n)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""
        title = _pick("title", "name", "pattern", "tag")
        take = _pick("takeaway", "why", "beat", "note")
        if not (title or take):
            continue
        out.append({"sec": "同类爆款案例", "text": f"{title}：{take}".strip("："), "key": f"cases:{i}"})
    return out


_KB_BIGRAM_CACHE: dict[str, frozenset] = {}


def _bg(s: str) -> frozenset:
    s = re.sub(r"[\s\u3000。，、；：？！…—·「」『』“”‘’\"'（）()]", "", s)
    v = _KB_BIGRAM_CACHE.get(s)
    if v is None:
        v = frozenset(s[i : i + 2] for i in range(max(0, len(s) - 1)))
        _KB_BIGRAM_CACHE[s] = v
    return v


def _relevance(text: str, query_bg: frozenset) -> float:
    if not query_bg:
        return 0.0
    bg = _bg(text)
    if not bg:
        return 0.0
    # 归一化用「查询词被覆盖的比例」而不是「条目自身 bigram 数的比例」。
    # 旧写法 len(bg & query_bg) / len(bg) 会系统性打压长条目：一条 30 个 bigram 的
    # 具体场景命中 2 个只得 0.07，而一条 6 个 bigram 的空泛概括（"深夜喂奶时婆媳
    # 育儿理念冲突"）命中 2 个得 0.33 —— 每章灌进提示词的就总是最短最空的那批。
    return len(bg & query_bg) / max(1, len(query_bg))


_REL_TIER_W = 0.2


def _rel_tier(score: float) -> float:
    """相关性主分（×10），与具体度加权合成一个可比较的排序分。

    为什么不能纯分档：分档（哪怕是 0.2 一档）会让具体度完全压过相关性，
    两章查询词不同却选出同一批条目；而纯相关性又会被"标题式空壳条目"刷满
    （它们与查询词字面重合度最高）。所以按「相关性 10 分制 + 具体度 1.2 分/级」
    加权：相关性的量级（10）足以主导，具体度足以在同档内翻盘。
    """
    return score * 10.0


def digest_for(
    card: dict | None,
    query: str = "",
    budget: int = 2200,
    exclude: set[str] | None = None,
    per_sec_cap: int = 3,
    max_items: int = 8,
) -> tuple[str, list[str]]:
    """按"本章要写什么"挑相关条目注入，而不是每章都灌同一份摘录。

    为什么要这么改（实测驱动）：
    1. 旧 `digest()` 每章返回**同一段**截断文本，8 章重复注入同一批 15 条 facts，
       写手被反复锚定，直接助长成稿里的"同一信息写三遍"；
    2. 旧 `digest()` 按固定顺序截断，`block[:budget-used]` 会从句子中间切断；
    3. `hooks` / `cases` 两个字段**从未进过任何提示词**（花钱蒸馏出来的数据白扔）。

    本函数用「章节关键词 × 条目 bigram 重叠」做相关性排序，同一节数上限
    `per_sec_cap` 保证覆盖面，`exclude` 里是前面章节已用过的 key（跨章不重复）。
    返回 (文本, 本次实际用掉的 key 列表)。
    """
    if not card:
        return "（无，按常识写作）", []
    exclude = exclude or set()
    items = _card_items(card)
    if not items:
        return "（无）", []

    query_bg = _bg(query or "")
    order = {h: i for i, h in enumerate(
        ["要诀", "真实细节/钱数行情", "可写场景", "对话语气参考", "开头钩子",
         "读者雷点", "安全红线", "同类爆款案例"])}

    # 无查询词（选题/pitch 阶段）：要的是"全景"而不是"高亮"。
    # 此时若按条目长度排序，钩子和爆款案例（最长）会被整类挤掉——
    # 旧 digest 就是这么把这两类字段丢掉的。改为按分区轮流各取一条。
    if not query_bg:
        buckets: dict[str, list[dict]] = {}
        for it in items:
            if it["key"] not in exclude:
                buckets.setdefault(it["sec"], []).append(it)
        # 同类内先按具体度排（能拍成戏的在前），再按原顺序，保证"轮流取一条"取到的是好料
        for _h in buckets:
            buckets[_h].sort(key=lambda it: -_L.score_item(it["text"], it["sec"]))
        chosen = []
        idx = 0
        while len(chosen) < max_items:
            added = False
            for head in sorted(buckets, key=lambda h: order.get(h, 99)):
                lst = buckets[head]
                if idx < len(lst) and len(chosen) < max_items:
                    chosen.append(lst[idx])
                    added = True
            if not added:
                break
            idx += 1
        return _render_digest(card, chosen, budget, order)

    pool = []
    for it in items:
        if it["key"] in exclude:
            continue
        score = _relevance(it["text"], query_bg)
        # 相关性同分时不再"短条目优先"：扩写进来的条目长而具体，短条目是抽掉壳的
        # 概括（"深夜喂奶时婆媳育儿理念冲突"）。改用具体度打分裁决，长度只做末位兜底。
        # 相关性 + 0.5×具体度：纯相关性会被"标题式条目"刷满（它们和查询词字面重合高），
        # 加具体度权重后，能拍成戏的条目才排得上来。
        conc = _L.score_item(it["text"], it["sec"])
        # 排序分层见 _rel_tier 的说明：相关性 10 分制 + 具体度 1.2 分/级。
        # 不再"短条目优先"——扩写进来的条目长而具体，短条目是抽掉壳的概括
        # （"深夜喂奶时婆媳育儿理念冲突"），长度只做末位兜底。
        pool.append((_rel_tier(score) + conc * 1.2, conc, round(score, 4),
                     -len(it["text"]), it))
    if not pool:
        # 全被前面章节用完：本轮从全部条目里挑（宁可重复也不能空手）
        pool = [(0.0, _L.score_item(it["text"], it["sec"]), 0.0,
                 -len(it["text"]), it) for it in items]
    pool.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3]))

    chosen: list[dict] = []
    seen_bg: list[frozenset] = []
    sec_cnt: dict[str, int] = {}
    for _tier, _concrete, _score, _neg, it in pool:
        if len(chosen) >= max_items:
            break
        if sec_cnt.get(it["sec"], 0) >= per_sec_cap:
            continue
        bg = _bg(it["text"])
        # 条目之间近乎重复的（同一件事换说法）只留一条
        if bg and any(len(bg & s) > 0.7 * len(bg | s) for s in seen_bg):
            continue
        chosen.append(it)
        seen_bg.append(bg)
        sec_cnt[it["sec"]] = sec_cnt.get(it["sec"], 0) + 1

    # 硬排除会耗尽：卡片只有 40-60 条，8 章 × 每章 8 条就抽干了，后几章只能拿到
    # 一两条残料（实测第 4 章只剩 193 字）。所以新鲜条目不足时，允许回收已用过的，
    # 但排在最后、且降权——仍以"本章相关"为优先。
    if len(chosen) < max(3, max_items // 2) and exclude:
        picked = {it["key"] for it in chosen}
        for it in sorted(items, key=lambda x: -_L.score_item(x["text"], x["sec"])):
            if it["key"] in picked:
                continue
            bg = _bg(it["text"])
            if bg and any(len(bg & s) > 0.7 * len(bg | s) for s in seen_bg):
                continue
            chosen.append(it)
            seen_bg.append(bg)
            if len(chosen) >= max_items:
                break

    # 按原始分区顺序重新组织（要诀 → 细节 → 场景 → 对话 → 钩子 → 雷点 → 红线 → 案例）
    chosen.sort(key=lambda x: order.get(x["sec"], 99))
    return _render_digest(card, chosen, budget, order)


def _render_digest(card: dict, chosen: list[dict], budget: int,
                   order: dict) -> tuple[str, list[str]]:
    """把选中的条目拼成提示词文本，严格按预算切（超预算的条目整条丢弃）。"""
    lines: list[str] = []
    used = 0
    used_keys: list[str] = []
    by_sec: dict[str, list[str]] = {}
    for it in chosen:
        block_len = len(it["text"]) + 1
        if used + block_len > budget:
            break
        by_sec.setdefault(it["sec"], []).append(it["text"])
        used += block_len
        used_keys.append(it["key"])
    for head in sorted(order, key=lambda h: order[h]):
        vals = by_sec.get(head) or []
        if not vals:
            continue
        lines.append(f"【{head}】" + "；".join(vals))
    if card.get("fallback"):
        lines.append("【注意】本卡为 LLM 内化生成（未联网核实），具体数字写成'行情参考'口径")
    return ("\n".join(lines) or "（无）"), used_keys

