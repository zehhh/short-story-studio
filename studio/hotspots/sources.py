# -*- coding: utf-8 -*-
"""各大平台热点榜适配器。

轻量直连各平台公开榜单接口，失败自动跳过不影响整体；若本地部署了热榜聚合服务，
配置 dailyhot_base 即可走代理通道获得更稳定的数据（兼容 `/api?type=xxx` 这类
返回 {title,url,hot} 列表的通用结构）。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .base import HotItem, parse_hot_value

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 12

# 各平台自带话题页跳转的搜索链接
_S_WEIBO = "https://s.weibo.com/weibo?q=%23{q}%23"
_ZHIHU_Q = "https://www.zhihu.com/question/{qid}"
_BV = "https://www.bilibili.com/video/{bvid}"


# ---------------------------------------------------------------------------
# 各平台适配器：每个返回 List[HotItem]，失败抛异常由 safe 包裹
# ---------------------------------------------------------------------------

def fetch_weibo(n: int) -> list[HotItem]:
    """微博热搜：优先 ajax/side/hotSearch（data.realtime），降级 statuses/hot_band
    （data.band_list / data.band.list 兼容）。

    微博对无 cookie 请求风控较严，结构偶发缺 data；配置 studio.weibo_cookie
    （浏览器 SUB=... 串）可显著提高成功率。
    """
    errors = []
    endpoints = [
        ("https://weibo.com/ajax/side/hotSearch", None),
        ("https://weibo.com/ajax/statuses/hot_band", None),
    ]
    for url, _ in endpoints:
        try:
            h = dict(HEADERS)
            if WEIBO_COOKIE:
                h["Cookie"] = WEIBO_COOKIE
            r = requests.get(url, headers=h, timeout=TIMEOUT)
            j = r.json()
            data = j.get("data")
            if not isinstance(data, dict):
                raise KeyError("data")
            if url.endswith("hotSearch"):
                realtime = data.get("realtime")
                if not isinstance(realtime, list):
                    raise KeyError("realtime")
            else:
                # 新结构 data.band_list；旧结构 data.band.list
                band_list = data.get("band_list")
                if isinstance(band_list, list):
                    realtime = band_list
                else:
                    band = data.get("band")
                    realtime = band.get("list") if isinstance(band, dict) else None
                    if not isinstance(realtime, list):
                        raise KeyError("band_list")
            items = []
            for i, row in enumerate(realtime[:n]):
                word = row.get("word", "") or row.get("word_scheme", "")
                items.append(
                    HotItem(
                        source="weibo",
                        rank=i + 1,
                        title=word.strip("#"),
                        hot_value=float(row.get("num", row.get("raw_hot", 0)) or 0),
                        url=_S_WEIBO.format(q=requests.utils.quote(f"#{word}#")),
                        extra={"label": row.get("label_name", "")},
                    )
                )
            return items
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url.split('/')[-1]}: {e}")
    raise RuntimeError(
        "微博热搜接口不可用（" + " | ".join(errors) + "）。"
        "多为无 cookie 触发风控：请在 config 的 studio.weibo_cookie 配置浏览器 SUB=... 串，"
        "或改用 dailyhot 通道（配置 studio.dailyhot_base）"
    )


WEIBO_COOKIE = ""  # 运行时由 config studio.weibo_cookie 注入（fetch_all / cmd_check）


def fetch_zhihu(n: int) -> list[HotItem]:
    """知乎热榜：api.zhihu.com/topstory/hot-list"""
    r = requests.get(
        "https://api.zhihu.com/topstory/hot-list",
        params={"limit": max(n, 10)},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    data = r.json().get("data", [])
    items = []
    for i, row in enumerate(data[:n]):
        target = row.get("target", {}) or {}
        title = str(target.get("title", "")).strip()
        api_url = target.get("url", "") or ""
        qid = api_url.rstrip("/").split("/")[-1]
        items.append(
            HotItem(
                source="zhihu",
                rank=i + 1,
                title=title,
                hot_value=parse_hot_value(row.get("detail_text", "")),
                url=_ZHIHU_Q.format(qid=qid) if qid.isdigit() else "",
                extra={"excerpt": (target.get("excerpt") or "")[:120]},
            )
        )
    return items


def fetch_baidu(n: int) -> list[HotItem]:
    """百度热搜：wise 移动端接口，无需 cookie。"""
    r = requests.get(
        "https://top.baidu.com/api/board",
        params={"platform": "wise", "tab": "realtime"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    cards = r.json()["data"]["cards"]
    rows: list[dict] = []

    def _collect(obj):
        if isinstance(obj, dict):
            if "word" in obj:
                rows.append(obj)
            else:
                for v in obj.values():
                    _collect(v)
        elif isinstance(obj, list):
            for v in obj:
                _collect(v)

    _collect(cards)
    items = []
    for i, row in enumerate(rows[:n]):
        items.append(
            HotItem(
                source="baidu",
                rank=i + 1,
                title=row.get("word", ""),
                hot_value=parse_hot_value(row.get("hotScore", 0) or 0) or max(0, 1000 - i),
                url=row.get("url", ""),
                extra={"desc": (row.get("desc") or "")[:120]},
            )
        )
    return items


def fetch_douyin(n: int) -> list[HotItem]:
    """抖音热点榜（旧接口，可能随平台调整失效）"""
    r = requests.get(
        "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    rows = r.json().get("word_list", [])
    items = []
    for i, row in enumerate(rows[:n]):
        items.append(
            HotItem(
                source="douyin",
                rank=i + 1,
                title=row.get("word", ""),
                hot_value=float(row.get("hot_value", 0) or 0),
                url="",
            )
        )
    return items


def fetch_toutiao(n: int) -> list[HotItem]:
    """头条热榜"""
    r = requests.get(
        "https://www.toutiao.com/hot-event/hot-board/",
        params={"origin": "toutiao_pc"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    rows = r.json().get("data", [])
    items = []
    for i, row in enumerate(rows[:n]):
        if not isinstance(row, dict) or not row.get("Title"):
            continue
        items.append(
            HotItem(
                source="toutiao",
                rank=len(items) + 1,
                title=row.get("Title", ""),
                hot_value=parse_hot_value(row.get("HotValue", "")),
                url=row.get("Url", ""),
            )
        )
    return items


def fetch_bilibili(n: int) -> list[HotItem]:
    """B站热门榜（popular 接口无需签名）"""
    r = requests.get(
        "https://api.bilibili.com/x/web-interface/popular",
        params={"ps": 50, "pn": 1},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    rows = r.json()["data"]["list"]
    items = []
    for i, row in enumerate(rows[:n]):
        items.append(
            HotItem(
                source="bilibili",
                rank=i + 1,
                title=row.get("title", ""),
                hot_value=float((row.get("stat") or {}).get("view", 0) or 0),
                url=_BV.format(bvid=row.get("bvid", "")),
                extra={"owner": row.get("owner", {}).get("name", "")},
            )
        )
    return items


def fetch_tieba(n: int) -> list[HotItem]:
    """贴吧热议榜"""
    r = requests.get(
        "https://tieba.baidu.com/hottopic/browse/topicList",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    rows = r.json()["data"]["bang_topic"]["topic_list"]
    items = []
    for i, row in enumerate(rows[:n]):
        items.append(
            HotItem(
                source="tieba",
                rank=i + 1,
                title=row.get("topic_name", ""),
                hot_value=parse_hot_value(row.get("topic_discuss", "")),
                url=row.get("topic_url", "") or row.get("topic_img", ""),
            )
        )
    return items


ADAPTERS = {
    "weibo": fetch_weibo,
    "zhihu": fetch_zhihu,
    "baidu": fetch_baidu,
    "douyin": fetch_douyin,
    "toutiao": fetch_toutiao,
    "bilibili": fetch_bilibili,
    "tieba": fetch_tieba,
}

SOURCE_NAMES = {
    "weibo": "微博热搜",
    "zhihu": "知乎热榜",
    "baidu": "百度热搜",
    "douyin": "抖音热点",
    "toutiao": "头条热榜",
    "bilibili": "B站热门",
    "tieba": "贴吧热议",
    "dailyhot": "热榜聚合(自建代理)",
    "mock": "Mock演示数据",
    "custom": "自定义清单",
}


# ---------------------------------------------------------------------------
# 聚合代理通道 + mock + 自定义文件
# ---------------------------------------------------------------------------

def fetch_dailyhot(base: str, route: str, n: int) -> list[HotItem]:
    """走自建热榜聚合服务（兼容通用 /api?type=xxx 结构），GET {base}/{route}"""
    r = requests.get(f"{base.rstrip('/')}/{route}", headers=HEADERS, timeout=TIMEOUT)
    rows = (r.json() or {}).get("data", []) or []
    items = []
    for i, row in enumerate(rows[:n]):
        items.append(
            HotItem(
                source=f"dailyhot:{route}",
                rank=i + 1,
                title=row.get("title", ""),
                hot_value=parse_hot_value(row.get("hot", 0)),
                url=row.get("url", "") or row.get("mobileUrl", ""),
            )
        )
    return items


def fetch_mock(n: int) -> list[HotItem]:
    """离线演示数据：下沉市场味道的热点样本，用于无网络/自测。"""
    samples = [
        ("女子退婚被索88万彩礼，村委会介入调解", 4823000),
        ("婆婆把儿媳陪嫁房给小叔子结婚用", 4510000),
        ("扶弟魔十年攒的30万被弟弟拿去赌博", 4400000),
        ("县城赘婿被赶出家门后，公司上市了", 3980000),
        ("65岁大爷再婚遭三个子女联合抵制", 3610000),
        ("爸妈养老钱被骗买保健品，儿子卧底取证", 3350000),
        ("村里拆迁分了4套房，兄弟俩大打出手", 3100000),
        ("重生回到1998年，我先把彩礼攒够了", 2960000),
        ("陪读妈妈在县中学旁租房的十年", 2720000),
        ("丈夫出轨后我净身出户，三年后他跪着回来", 2580000),
    ]
    return [
        HotItem(source="mock", rank=i + 1, title=t, hot_value=v, url="")
        for i, (t, v) in enumerate(samples[:n])
    ]


def fetch_custom(n: int, path: str | None = None) -> list[HotItem]:
    """自定义热点清单：每行 `标题 | 热度 | 链接`（后两项可省略）。"""
    import pathlib

    p = pathlib.Path(path) if path else pathlib.Path("hotspots_custom.txt")
    items = []
    if not p.exists():
        print(f"[hot] 自定义清单不存在：{p}")
        return items
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        title = parts[0].strip()
        hot = parse_hot_value(parts[1]) if len(parts) > 1 else (len(parts[0]) * 1000)
        url = parts[2].strip() if len(parts) > 2 else ""
        items.append(HotItem(source="custom", rank=i + 1, title=title, hot_value=hot, url=url))
        if len(items) >= n:
            break
    return items


# ---------------------------------------------------------------------------
# 聚合抓取
# ---------------------------------------------------------------------------

def safe_fetch(fn, n: int, name=None) -> list[HotItem]:
    name = str(name or getattr(fn, "__name__", "source"))
    try:
        from .. import runlog
    except Exception:  # noqa: BLE001
        runlog = None
    try:
        items = fn(n)
        print(f"[hot] {name:<14} 抓到 {len(items)} 条")
        if runlog: runlog.event("hotspots", f"{name} 抓到 {len(items)} 条")
        return items
    except Exception as e:  # noqa: BLE001
        print(f"[hot] {name:<14} 失败：{type(e).__name__}: {e}")
        if runlog: runlog.event("hotspots", f"{name} 抓取失败", level="error")
        return []


def fetch_all(cfg: dict, sources: list[str] | None = None, top: int | None = None,
              directions: list[str] | None = None) -> list[HotItem]:
    """并发抓取多个平台，聚合去重后按热度排序返回。directions 供小说源做题材聚合。"""
    global WEIBO_COOKIE
    if cfg["studio"].get("weibo_cookie"):
        WEIBO_COOKIE = cfg["studio"]["weibo_cookie"]
    scfg = cfg["studio"]
    s_extra = {"directions": directions or []}
    sources = sources or scfg.get("sources") or list(ADAPTERS)
    n = top or scfg.get("hot_top", 30)
    all_items: list[HotItem] = []

    direct, dailyhot_routes, novel_dirs = [], [], []
    for s in sources:
        if s == "novel":
            # 小说平台/故事向聚合源：题材来自 config 或全局 directions 参数
            novel_dirs = (s_extra.get("directions") if isinstance(s_extra, dict) else None) or []
            continue
        if s in ADAPTERS:
            direct.append(s)
        elif s.startswith("dailyhot:"):
            dailyhot_routes.append(s.split(":", 1)[1])
        elif s == "dailyhot" and scfg.get("dailyhot_base"):
            dailyhot_routes.extend(["weibo", "zhihu", "baidu", "douyin", "toutiao", "bilibili", "tieba"])
        elif s == "mock":
            all_items.extend(safe_fetch(fetch_mock, n, SOURCE_NAMES["mock"]))
        elif s == "custom":
            all_items.extend(safe_fetch(fetch_custom, n, "自定义清单"))
        else:
            print(f"[hot] 未知来源，已跳过：{s}")

    if novel_dirs:
        try:
            from .novelsources import fetch_novel_hot

            all_items.extend(
                safe_fetch(lambda _n: fetch_novel_hot(novel_dirs, n=max(4, n // 3)), n,
                           "小说平台聚合")
            )
        except Exception as e:  # noqa: BLE001
            print(f"[hot] 小说平台聚合 失败：{e}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(safe_fetch, ADAPTERS[s], n, SOURCE_NAMES.get(s, s)): s for s in direct}
        base = scfg.get("dailyhot_base", "")
        for route in dailyhot_routes:
            futures[pool.submit(safe_fetch, lambda r=route: fetch_dailyhot(base, r, n), n, f"{SOURCE_NAMES['dailyhot']}:{route}")] = route
        for fut in as_completed(futures):
            all_items.extend(fut.result())

    return dedupe(all_items)


def _normalize(title: str) -> str:
    import re

    t = re.sub(r"【|】|\[|\]|\(|\)|（|）|#|话题|热搜|热榜", "", title)
    return re.sub(r"[\s，。！？：；、,.!?::]+", "", t)


def dedupe(items: list[HotItem], threshold: float = 0.72) -> list[HotItem]:
    """近似去重（不同平台同一事件），保留热度更高的一条。"""
    import difflib

    items = sorted(items, key=lambda x: -x.hot_value)
    kept: list[HotItem] = []
    keys: list[str] = []
    for it in items:
        norm = _normalize(it.title)
        if not norm:
            continue
        dup = False
        for k in keys:
            if difflib.SequenceMatcher(None, norm, k).ratio() >= threshold or norm[:10] == k[:10]:
                dup = True
                break
        if not dup:
            kept.append(it)
            keys.append(norm)
    for i, it in enumerate(kept):
        it.rank = i + 1
    return kept
