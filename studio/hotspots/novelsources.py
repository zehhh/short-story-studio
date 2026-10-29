# -*- coding: utf-8 -*-
"""小说平台/故事向热点源。

组成（全部真实可跑，无字体加密、无搜索引擎拆词问题）：
  1. 七猫排行榜直连（s-book-title）；
  2. 知乎故事类提问（热榜按"经历/体验/故事/怎么看待"特征过滤）；
  3. 贴吧热议（家庭伦理浓度高）。
按梗库题材关键词对条目打分排序，实现"各题材定向"：素材池大、题材相关度优先。
产出 HotItem.source 前缀 novel:，进素材库前仍需过"素材转换门"。
"""
from __future__ import annotations

import json
import re

import requests

from .base import HotItem, parse_hot_value

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
STORY_HINT = ("经历", "体验", "故事", "怎么看待", "如何评价", "真实", "亲戚", "婆媳", "彩礼", "离婚", "养老")


def fetch_qimao(n: int = 10) -> list[HotItem]:
    """七猫排行榜：解析 paihang 页 s-book-title 节点。"""
    r = requests.get("https://www.qimao.com/paihang/", headers=_UA, timeout=12)
    r.raise_for_status()
    titles = re.findall(r'class="s-book-title"[^>]*>([^<]{2,30})<', r.text)
    out = []
    for i, t in enumerate(dict.fromkeys(x.strip() for x in titles if x.strip())):
        out.append(HotItem(source="novel:qimao", rank=i + 1, title=t,
                           hot_value=float(max(1, 1000 - i * 40)),
                           url="https://www.qimao.com/paihang/",
                           extra={"platform": "七猫排行榜"}))
        if len(out) >= n:
            break
    return out


def fetch_zhihu_story(n: int = 10) -> list[HotItem]:
    """知乎热榜 → 故事类提问过滤（经历/体验/怎么看待…），这类问题天然是素材池。"""
    r = requests.get("https://api.zhihu.com/topstory/hot-list",
                     params={"limit": 50}, headers=_UA, timeout=12)
    data = r.json().get("data", [])
    out = []
    for row in data:
        target = row.get("target", {}) or {}
        title = str(target.get("title", "")).strip()
        if not title:
            continue
        if not any(h in title for h in STORY_HINT):
            continue
        api_url = target.get("url", "") or ""
        qid = api_url.rstrip("/").split("/")[-1]
        out.append(HotItem(
            source="novel:zhihu_story", rank=len(out) + 1, title=title,
            hot_value=parse_hot_value(row.get("detail_text", "")),
            url=f"https://www.zhihu.com/question/{qid}" if qid.isdigit() else "",
            extra={"platform": "知乎故事类"}))
        if len(out) >= n:
            break
    return out


def fetch_tieba_story(n: int = 10) -> list[HotItem]:
    """贴吧热议（家庭伦理/社会人情浓度高）。"""
    r = requests.get("https://tieba.baidu.com/hottopic/browse/topicList",
                     headers=_UA, timeout=12)
    rows = (r.json().get("data", {}).get("bang_topic", {}) or {}).get("topic_list", [])
    out = []
    for row in rows[:n]:
        t = row.get("topic_name", "").strip("# ")
        if t:
            out.append(HotItem(source="novel:tieba", rank=len(out) + 1, title=t,
                               hot_value=parse_hot_value(row.get("topic_discuss", "")),
                               url=row.get("topic_url", "") or "",
                               extra={"platform": "贴吧热议"}))
    return out


def _direction_keywords(direction: str) -> list[str]:
    from ..config import data_path

    try:
        for d in json.loads(data_path("tropes.json").read_text(encoding="utf-8"))["directions"]:
            if direction in (d["name"], d["id"]):
                return [k for k in d.get("keywords", []) if len(k.strip()) >= 2]
    except Exception:  # noqa: BLE001
        pass
    return []


def _relevance(item: HotItem, directions: list[str]) -> float:
    """题材相关度：命中梗库关键词 +1/个；无指定方向时用故事特征词兜底。"""
    score = 0.0
    text = item.title
    for d in directions or []:
        for kw in _direction_keywords(d) or [d]:
            if kw and kw in text:
                score += 1.0
    return score


def fetch_novel_hot(cfg_or_directions, directions: list[str] | None = None, n: int = 12) -> list[HotItem]:
    """聚合三源，按题材相关度重排（相关度优先，热度次之）。"""
    import concurrent.futures as cf

    dirs = directions or []
    items: list[HotItem] = []
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(f, n): f.__name__ for f in (fetch_qimao, fetch_zhihu_story, fetch_tieba_story)}
        for fut in cf.as_completed(futs):
            try:
                items.extend(fut.result())
            except Exception:  # noqa: BLE001
                continue
    for it in items:
        it.extra["relevance"] = _relevance(it, dirs)
    items.sort(key=lambda x: (-x.extra.get("relevance", 0.0), -x.hot_value))
    for i, it in enumerate(items):
        it.rank = i + 1
    return items[: n * 2]
