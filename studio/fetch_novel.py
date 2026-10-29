# -*- coding: utf-8 -*-
"""热门小说自动抓取 → 清洗 → 深拆入库。

平台源可自定义：config.yaml 的 novel_fetch.sources 列表，内置三种适配器——
  biquge   笔趣阁系模板（base/list/encoding/content_re 可配，新站填几个字段即可接入）
  qidian   起点短篇站（duanpian.bookresource.qq.com，静态正文 .content）
  onehu    onehu.xyz（知乎故事短篇镜像，WordPress 文章页）

流程：榜单/列表页发现书目 → 取 top N → 抓正文 → 剥广告清洗 →
      workspace/fetched/<slug>.txt → deconstruct 深拆入库（deposit_material=False，
      与批量拆书同款，不向素材库淹没写入）。

站点情报来源：dataset-forge 2026-09 实测选型（xbiquge.bz JS 渲染不可用已弃；
番茄 web 有字体反爬且锁章，未接入）。换 IP/换时间站点可能失效，失败时换源。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 内置默认源（config novel_fetch.sources 为空时兜底；用户可在 config 覆盖/新增）
DEFAULT_SOURCES = [
    {"name": "笔尖中文·完本榜", "type": "biquge",
     "base": "http://www.xbiquwk.com", "list": "/wanjiexiaoshuo/",
     "encoding": "gbk", "max_chapters": 40},
    {"name": "起点短篇", "type": "qidian",
     "base": "https://duanpian.bookresource.qq.com", "max_chapters": 30},
    {"name": "onehu故事", "type": "onehu",
     "base": "https://onehu.xyz"},
]


def sources_of(cfg: dict) -> list[dict]:
    srcs = (cfg.get("novel_fetch") or {}).get("sources") or []
    return srcs or DEFAULT_SOURCES


def _get(url: str, encoding: str = "utf-8", retries: int = 2) -> str | None:
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=_UA, timeout=15)
            r.raise_for_status()
            if encoding and encoding.lower() != "auto":
                r.encoding = encoding
            else:
                r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:  # noqa: BLE001
            if i >= retries:
                print(f"    [fetch] 失败 {url}：{e}")
                return None
            time.sleep(2 + i * 2)
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style).*?</\1>")


def _strip_html(html: str) -> str:
    html = _SCRIPT_RE.sub("", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    txt = _TAG_RE.sub("", html)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
              .replace("&#39;", "'").replace("&mdash;", "——"))
    return txt


_AD_RE = re.compile(
    r"(笔趣阁|biquge|www\.[a-z0-9]+\.com|最新网址|请收藏|记住本站|"
    r"手机用户浏览|阅读页|章节错误|点此举报|加入书签|推荐票|月票|手机版)")


def clean_lines(text: str, min_len: int = 12) -> str:
    """剥广告行/裸页码/超短残行。保守策略：只删明显广告形态，宁漏勿错。"""
    lines2 = [ln for ln in text.splitlines()]
    cleaned = []
    for ln in lines2:
        ln = ln.strip()
        if not ln:
            cleaned.append("")
            continue
        if re.fullmatch(r"[\d\s.。,，\-—_]+", ln):          # 裸页码行
            continue
        if len(ln) <= 30 and _AD_RE.search(ln):             # 短广告行
            continue
        if re.fullmatch(r"#[\w一-龥]+", ln):                # #标签 行
            continue
        if re.fullmatch(r"[A-Za-z0-9_ ]{1,20}", ln):        # 纯英文残行（zhihu 等）
            continue
        if re.match(r"^(第?\s*[0-9一二三四五六七八九十百千]+\s*[章节回])", ln) and len(ln) < 40:
            continue                                        # 章节标题行（拆书自己切段）
        cleaned.append(ln)
    # 开头重复标题（《X》/X/副标题 形态，可能隔空行，只留首个）
    nz = [i for i, l in enumerate(cleaned) if l.strip()][:3]
    if len(nz) >= 2:
        a = cleaned[nz[0]].strip("《》 ")
        b = cleaned[nz[1]].strip("《》 ")
        if a and a == b and len(cleaned[nz[1]]) < 40:
            del cleaned[nz[1]]
    # 尾部元数据（备案号/编辑于…禁止转载/纯符号行）从末尾连续剥
    tail_re = re.compile(r"(备案号|禁止转载|著作权|本文首发|原文地址|编辑于\s*\d{4})")
    while cleaned and (not cleaned[-1].strip()
                       or tail_re.search(cleaned[-1])
                       or re.fullmatch(r"[A-Za-z0-9: ·]+", cleaned[-1].strip())):
        cleaned.pop()
    txt = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned))
    return txt.strip()


# ---------------------------------------------------------------------------
# 三种适配器：list_books() → [(title, url)]；fetch_book() → 全文
# ---------------------------------------------------------------------------

def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', html)


def _norm_path(url: str, base: str) -> str:
    """href 值 → 去域名的纯路径（供 fullmatch 判形态，杜绝子串误匹配）。"""
    return re.sub(r"^[a-z]+://[^/]+", "", urljoin(base, url))


def _list_biquge(src: dict, limit: int) -> list[tuple[str, str]]:
    base = src["base"].rstrip("/")
    html = _get(base + src.get("list", "/wanjiexiaoshuo/"), src.get("encoding", "utf-8"))
    if not html:
        return []
    seen, out = set(), []
    for href, title in re.findall(r'href="([^"]+)"[^>]*>([^<]{2,40})<', html):
        if not re.fullmatch(r"/\d+_\d+/?", _norm_path(href, base)):
            continue
        u = urljoin(base + "/", href)
        if u in seen:
            continue
        seen.add(u)
        out.append((title.strip(), u))
        if len(out) >= limit:
            break
    return out


def _fetch_biquge(src: dict, url: str, max_chapters: int) -> str:
    html = _get(url, src.get("encoding", "gbk"))
    if not html:
        return ""
    m = re.search(r"<h1[^>]*>([^<]{2,60})</h1>", html)
    title = m.group(1).strip() if m else url.rstrip("/").split("/")[-1]
    chapter_urls: list[str] = []
    seen_ch: set[str] = set()
    for href in _hrefs(html):
        if not re.fullmatch(r"(?:/\d+_\d+/)?\d+\.html", _norm_path(href, url)):
            continue
        u = urljoin(url, href)
        if u not in seen_ch:
            seen_ch.add(u)
            chapter_urls.append(u)
    parts = [f"《{title}》\n"]
    for u in chapter_urls[:max_chapters]:
        c = _get(u, src.get("encoding", "gbk"))
        if not c:
            continue
        m = re.search(r'(?s)<div[^>]+id="content"[^>]*>(.*?)</div>', c) \
            or re.search(r'(?s)<div[^>]+class="[^"]*content[^"]*"[^>]*>(.*?)</div>', c)
        body = _strip_html(m.group(1)) if m else ""
        if len(body) > 200:
            parts.append(body)
        time.sleep(0.4)          # 温和限速
    return "\n\n".join(parts)


def _list_qidian(src: dict, limit: int) -> list[tuple[str, str]]:
    base = src["base"].rstrip("/")
    html = _get(base + "/", "utf-8")
    if not html:
        return []
    seen, out = set(), []
    for href, title in re.findall(r'href="(/book/\d+/?)"[^>]*>([^<]{2,40})<', html):
        u = urljoin(base + "/", href)
        if u in seen or not title.strip():
            continue
        seen.add(u)
        out.append((title.strip(), u))
        if len(out) >= limit:
            break
    return out


def _fetch_qidian(src: dict, url: str, max_chapters: int) -> str:
    html = _get(url, "utf-8")
    if not html:
        return ""
    m = re.search(r"<h1[^>]*>([^<]{2,60})</h1>", html)
    title = m.group(1).strip() if m else url.rstrip("/").split("/")[-1]
    chaps = []
    for href in re.findall(r'href="(/chapter/\d+/\d+/?)"', html):
        u = urljoin(url, href)
        if u not in chaps:
            chaps.append(u)
    prefer = int(src.get("prefer_max_chapters") or 90)
    if len(chaps) > prefer:       # 默认只要中短篇；config 里调 prefer_max_chapters 可放开
        print(f"    [fetch] 《{title}》{len(chaps)} 章超上限（{prefer}），跳过")
        return ""
    parts = [f"《{title}》\n"]
    for u in chaps[:max_chapters]:
        c = _get(u, "utf-8")
        if not c:
            continue
        m = re.search(r'(?is)<div[^>]+class="[^"]*content[^"]*"[^>]*>(.*?)</div>', c)
        body = _strip_html(m.group(1)) if m else ""
        if len(body) > 200:
            parts.append(body)
        time.sleep(0.4)
    return "\n\n".join(parts)


def _list_onehu(src: dict, limit: int) -> list[tuple[str, str]]:
    """onehu 首页即最新文章列表（/20YY/MM/DD/slug，相对链接，slug 是 URL 编码标题）。"""
    from urllib.parse import unquote

    base = src["base"].rstrip("/")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in ("", "page/2/"):
        html = _get(f"{base}/{page}", "utf-8")
        if not html:
            continue
        for href, title in re.findall(
                r'href="((?:https?://[^"]*)?/20\d\d/\d\d/\d\d/[^"#?]+)"[^>]*>([^<]{0,80})<', html):
            u = urljoin(base + "/", href)
            if u in seen:
                continue
            t = title.strip()
            if len(t) < 3:
                t = unquote(href.rstrip("/").split("/")[-1])[:50]
            if len(t) < 3:
                continue
            seen.add(u)
            out.append((t, u))
            if len(out) >= limit:
                return out
        time.sleep(0.5)
    return out


def _fetch_onehu(src: dict, url: str, max_chapters: int) -> str:
    html = _get(url, "utf-8")
    if not html:
        return ""
    m = re.search(r"<h1[^>]*class=\"[^\"]*entry-title[^\"]*\"[^>]*>(.*?)</h1>", html, re.S) \
        or re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    title = _strip_html(m.group(1)).strip() if m else url.rstrip("/").split("/")[-1]
    m = re.search(r'(?is)<div[^>]+class="[^"]*entry-content[^"]*"[^>]*>(.*?)</div>\s*(?:<footer|<div class="entry-footer)', html) \
        or re.search(r'(?is)<article[^>]*>(.*?)</article>', html)
    if not m:
        return ""
    body = _strip_html(m.group(1))
    return f"《{title}》\n\n{body}"


_ADAPTERS = {
    "biquge": (_list_biquge, _fetch_biquge),
    "qidian": (_list_qidian, _fetch_qidian),
    "onehu": (_list_onehu, _fetch_onehu),
}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def fetched_dir(cfg: dict) -> Path:
    p = Path(cfg["studio"]["workspace_abs"]) / "fetched"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return (s[:40] or "untitled")


def fetch_and_decon(cfg: dict, llm, source_name: str = "", top: int = 2,
                    max_chapters: int | None = None, do_decon: bool = True) -> dict:
    """抓取指定源（name 匹配 config novel_fetch.sources；空=第一个源）并深拆入库。"""
    srcs = sources_of(cfg)
    src = next((s for s in srcs if s.get("name") == source_name), None) if source_name else None
    if src is None:
        src = srcs[0] if srcs else DEFAULT_SOURCES[0]
    stype = src.get("type", "biquge")
    list_fn, fetch_fn = _ADAPTERS.get(stype, _ADAPTERS["biquge"])
    mc = int(max_chapters or src.get("max_chapters") or 40)

    print(f"[fetch] 源「{src.get('name')}」（{stype}）拉取列表 …")
    books = list_fn(src, top * 4)
    if not books:
        return {"ok": False, "error": f"源 {src.get('name')} 列表为空（站点可能失效，换源试试）"}
    print(f"[fetch] 发现 {len(books)} 本候选")

    outdir = fetched_dir(cfg)
    done = []
    for title, url in books:
        if len(done) >= top:
            break
        print(f"[fetch] 下载《{title}》 …")
        raw = fetch_fn(src, url, mc)
        if len(raw) < 3000:
            print(f"    正文过短（{len(raw)} 字），跳过")
            continue
        text = clean_lines(raw)
        if len(text) < 2000:
            print(f"    清洗后过短（{len(text)} 字），跳过")
            continue
        p = outdir / f"{_slug(title)}.txt"
        p.write_text(text, encoding="utf-8")
        print(f"    落盘 {p.name}（{len(text)} 字）")
        done.append({"title": title, "path": str(p), "chars": len(text)})
        if not do_decon:
            continue
        try:
            from . import deconstruct as dec

            dec.deconstruct_file(cfg, llm, p, title=title, deposit_material=False)
            print(f"    [decon] 《{title}》深拆入库完成")
        except Exception as e:  # noqa: BLE001
            print(f"    [decon] 《{title}》深拆失败：{e}")
    return {"ok": bool(done), "source": src.get("name"), "books": done}
