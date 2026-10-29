"""题材正文提示卡（genre prose cards）。

数据来源：`data/genre_cards.json`，由本地开源项目
`oh-story-research/skills/story-long-write/references/genre-prose-cards/` 的
32 张同题材长篇小说抽样卡提炼而成（字段：开场抓手 / 冲突发动机 / 爽点与情绪释放 /
对话与声线 / 章尾钩子 / 场景颗粒 / 前中后期打法 / 节奏密度 / 禁止漂移 / 样本统计）。

与 `prompts/genre_*.md` 的分工：
- `prompts/genre_*.md` 是**斜杠级**的完整题材指导（买股文模板、修仙境界表），
  由 `load_genre_guide()` 按 direction 注入整份；
- 本模块是**每章都要看的短约束**（节奏密度 + 禁止漂移 + 样本统计），
  拼进 `_genre_note()` 的每章提示，随手可用、不占大预算。

为什么需要：`prompts/genre_hougong.md` 写的是现代买股文（贵族学院/娱乐圈），
而用户点名的「女主后宫向古言」被映射到它，等于给宫廷稿注入现代赛道模板。
题材卡按「宫斗宅斗 / 古言脑洞 / 历史古代」召回，能把每章约束拉回同一个世界。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data" / "genre_cards.json"

# 方向名/选题 → 卡片名的直接映射（方向名往往不含题材词，例如「换嫁冲喜」「宫廷权谋」）
_DIRECT = {
    "宫廷权谋": "宫斗宅斗",
    "女主后宫": "宫斗宅斗",
    "换嫁冲喜": "宅斗",
    "宅斗": "宫斗宅斗",
    "侯门": "宫斗宅斗",
    "种田": "都市种田",
    "年代": "年代",
    "战神赘婿": "战神赘婿",
    "豪门": "豪门总裁",
    "职场婚恋": "职场婚恋",
    "甜宠": "青春甜宠",
    "民国": "民国言情",
    "玄幻": "传统玄幻",
    "仙侠": "东方仙侠",
    "快穿": "快穿",
    "悬疑": "悬疑脑洞",
    "科幻": "科幻末世",
    "游戏": "游戏体育",
}

# 卡片名里带这些字但方向命中时属于「同一世界」的补充召回
_ANCIENT_ORDER = ("宫斗宅斗", "古言脑洞", "古风世情", "历史古代")


@lru_cache(maxsize=1)
def load_cards() -> dict:
    try:
        return json.loads(_DATA.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 卡片是可选增强，缺文件不能让流水线挂
        return {}


def _alias_hit(direction: str, cards: dict) -> list[str]:
    hits = []
    for name, card in cards.items():
        for alias in card.get("aliases") or []:
            if alias and alias in direction:
                hits.append(name)
                break
    return hits


def match(direction: str, *, ancient: bool = False) -> tuple[str, dict] | tuple[None, None]:
    """按方向名召回一张题材卡。返回 (卡名, 卡内容)，召回不到返回 (None, None)。"""
    cards = load_cards()
    if not cards or not direction:
        return None, None
    for key, name in _DIRECT.items():
        if key in direction and name in cards:
            return name, cards[name]
    hits = _alias_hit(direction, cards)
    if hits:
        # 古装优先取「宫斗宅斗」，现代优先取第一个别名命中项
        if ancient:
            for name in _ANCIENT_ORDER:
                if name in hits:
                    return name, cards[name]
        return hits[0], cards[hits[0]]
    if ancient:
        for name in _ANCIENT_ORDER:
            if name in cards:
                return name, cards[name]
    return None, None


def note(card: dict, *, budget: int = 420) -> str:
    """把题材卡压成一段可塞进每章提示的约束（节奏密度 + 禁止漂移 + 样本统计）。"""
    if not card:
        return ""
    parts = []
    tempo = (card.get("tempo") or "").strip()
    drift = (card.get("drift") or "").strip()
    if tempo:
        parts.append(f"节奏密度：{tempo}")
    if drift:
        parts.append(f"禁止漂移：{drift}")
    para = card.get("para_chars")
    dia = card.get("dialog_ratio")
    if para or dia:
        stat = []
        if para:
            stat.append(f"段落中位约 {para:g} 字")
        if dia:
            stat.append(f"对话约占 {dia:g}%")
        parts.append("同题材长篇抽样（1626 本样本，24 本抽样）：" + "，".join(stat))
    text = "；".join(parts)
    if len(text) > budget:
        text = text[:budget].rstrip("；， ") + "…"
    return text
