# -*- coding: utf-8 -*-
"""RAG 知识库检索层（轻量方案：BM25 + 中文字符 bigram，零嵌入模型、纯 CPU）。

统一索引四类知识源：
  1. 题材知识卡  data/knowledge/*.json
  2. 素材库      workspace/materials.jsonl
  3. 教训库      workspace/feedback_lessons.json
  4. 拆书结果    workspace/decon/*.json
索引落盘 workspace/rag_index.json；源文件 mtime 变化时自动重建。

用法：
  rag.ensure(cfg)                       # 需要时重建索引
  rag.search(cfg, "婆婆 彩礼 借条", top_k=8)
  rag.context_for(cfg, query, direction="彩礼纠纷", max_chars=700)  # 供 prompt 注入
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .config import ROOT, data_path

K1, B = 1.5, 0.75


def _workspace(cfg: dict) -> Path:
    return Path(cfg["studio"]["workspace_abs"])


def index_path(cfg: dict) -> Path:
    return _workspace(cfg) / "rag_index.json"


# ---------------------------------------------------------------------------
# 分词：中文字符 bigram + 英数词
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9]{2,}", text):
        tokens.append(word)
    cjk = re.findall(r"[\u4e00-\u9fa5]", text)
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    if len(tokens) == 0 and cjk:
        tokens = cjk
    return tokens


# ---------------------------------------------------------------------------
# 文档收集
# ---------------------------------------------------------------------------

def iter_docs(cfg: dict) -> list[dict]:
    docs: list[dict] = []
    ws = _workspace(cfg)

    # 1) 题材知识卡
    kdir = ROOT / "data" / "knowledge"
    if kdir.exists():
        for f in kdir.glob("*.json"):
            try:
                card = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            d = card.get("direction", f.stem)
            for k in ("facts", "scenes", "dialogues", "hooks", "pitfalls", "redlines"):
                for x in card.get(k, []) or []:
                    docs.append({"source": "知识卡", "type": k, "direction": d, "text": str(x)})
            for c in card.get("cases", []) or []:
                docs.append({"source": "知识卡", "type": "案例", "direction": d,
                             "text": f"{c.get('title', '')}：{c.get('takeaway', '')}"})

    # 2) 素材库
    mfile = ws / "materials.jsonl"
    if mfile.exists():
        for line in mfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                x = json.loads(line)
            except json.JSONDecodeError:
                continue
            if x.get("quality") == "low":
                continue
            docs.append({"source": "素材库", "type": x.get("type", ""),
                         "direction": x.get("direction", ""), "text": x.get("text", ""),
                         "score": x.get("score"), "mid": x.get("id")})

    # 3) 教训库
    lf = ws / "feedback_lessons.json"
    if lf.exists():
        try:
            for e in json.loads(lf.read_text(encoding="utf-8")):
                for l in e.get("lessons", []) or []:
                    docs.append({"source": "教训库", "type": l.get("dimension", ""),
                                 "direction": "", "text": f"{l.get('lesson', '')}→{l.get('action', '')}"})
        except json.JSONDecodeError:
            pass

    # 4) 拆书结果——默认不进 rag 语料：拆书内容已由知识库单元（kb.py）更好地覆盖，
    #    而 3757 篇的 decon 语料会让 rag 索引膨胀到 250MB、每次重建 30s+，拖垮工作台。
    #    需要时置 config knowledge.rag_include_decon: true
    if (cfg.get("knowledge") or {}).get("rag_include_decon", False):
        ddir = ws / "decon"
        if ddir.exists():
            for f in ddir.glob("*.json"):
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                title = r.get("title", f.stem)
                for i, sec in enumerate(r.get("sections", [])):
                    docs.append({"source": "拆书", "type": "章节拆解",
                                 "direction": r.get("direction", ""),
                                 "text": f"《{title}》第{i + 1}段：{sec.get('goal', '')}｜钩子：{sec.get('hook', '')}｜技法：{sec.get('technique', '')}"})
                for t in r.get("book", {}).get("techniques", []) or []:
                    docs.append({"source": "拆书", "type": "写作技法", "direction": r.get("direction", ""),
                                 "text": f"《{title}》：{t}"})
                for q in r.get("book", {}).get("golden_quotes", []) or []:
                    docs.append({"source": "拆书", "type": "金句", "direction": r.get("direction", ""),
                                 "text": f"《{title}》：{q}"})
    return docs


def _sources_stamp(cfg: dict) -> str:
    """语料指纹：目录级 mtime（文件增删会更新父目录 mtime，免遍历上千文件）。

    注：文件内容原地修改不更新父目录 mtime——素材/教训走追加或原子替换（都会
    变父目录 mtime 或文件自身 mtime），decon 文件只增不改，安全。
    """
    import os

    times = []
    for p in [_workspace(cfg) / "materials.jsonl", _workspace(cfg) / "feedback_lessons.json",
              ROOT / "data" / "knowledge", _workspace(cfg) / "decon"]:
        if p.exists():
            times.append(os.path.getmtime(p))
            if p.is_dir():
                for x in p.iterdir():
                    try:
                        times.append(os.path.getmtime(x))
                    except OSError:
                        continue
    return str(max(times)) if times else "0"


# ---------------------------------------------------------------------------
# 建索引 / 检索（BM25）
# ---------------------------------------------------------------------------

def build(cfg: dict, force: bool = False) -> int:
    p = index_path(cfg)
    stamp = _sources_stamp(cfg)
    if not force:
        cached = _IDX_CACHE["idx"]
        if cached is not None and cached.get("stamp") == stamp and p.exists():
            return cached.get("N", 0)
        if p.exists():
            try:
                old = _load_index_cached(p)
                if old.get("stamp") == stamp:
                    return old.get("N", 0)
            except Exception:  # noqa: BLE001
                pass
    docs = iter_docs(cfg)
    doc_tokens = [tokenize(d["text"]) for d in docs]
    N = len(docs)
    df: dict[str, int] = {}
    tf_per_doc = []
    for toks in doc_tokens:
        tf = Counter(toks)
        tf_per_doc.append(tf)
        for t in tf:
            df[t] = df.get(t, 0) + 1
    avgdl = (sum(len(t) for t in doc_tokens) / N) if N else 0
    postings: dict[str, dict[str, list]] = {}
    for i, tf in enumerate(tf_per_doc):
        dl = len(doc_tokens[i]) or 1
        for t, f in tf.items():
            idf = math.log(1 + (N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            postings.setdefault(t, {"df": df.get(t, 0), "docs": []})["docs"].append(
                {"i": i, "tf": f, "idf": round(idf, 4), "norm": f / (1 - B + B * dl / (avgdl or 1))}
            )
    p.parent.mkdir(parents=True, exist_ok=True)
    from .util import atomic_write_text

    atomic_write_text(p, json.dumps({"stamp": stamp, "N": N, "docs": docs, "postings": postings},
                                    ensure_ascii=False))
    return N


from collections import Counter  # noqa: E402  (置于使用后亦可，模块加载即绑定)


def ensure(cfg: dict) -> int:
    return build(cfg)


# 索引进程内缓存：55MB 级索引文件每次查询重新 parse 曾占 1.5s/次。
# 以 (mtime_ns, size) 为签名，build 重写索引后自动失效重载。
_IDX_CACHE: dict = {"sig": None, "idx": None}


def _load_index_cached(p):
    sig = (p.stat().st_mtime_ns, p.stat().st_size)
    if _IDX_CACHE["sig"] != sig or _IDX_CACHE["idx"] is None:
        _IDX_CACHE["idx"] = json.loads(p.read_text(encoding="utf-8"))
        _IDX_CACHE["sig"] = sig
    return _IDX_CACHE["idx"]


def search(cfg: dict, query: str, top_k: int = 8, source: str = "") -> list[dict]:
    build(cfg)
    p = index_path(cfg)
    try:
        idx = _load_index_cached(p)
    except Exception:  # noqa: BLE001
        return []
    N = idx.get("N", 0)
    if not N:
        return []
    q_tokens = tokenize(query)
    scores: dict[int, float] = {}
    for t in set(q_tokens):
        post = idx["postings"].get(t)
        if not post:
            continue
        for d in post["docs"]:
            scores[d["i"]] = scores.get(d["i"], 0.0) + d["idf"] * d["tf"] * (K1 + 1) / (d["tf"] + K1 * d["norm"])
    ranked = sorted(scores.items(), key=lambda x: -x[1])[: top_k * 2]
    out = []
    for i, sc in ranked:
        d = idx["docs"][i]
        if source and d.get("source") != source:
            continue
        d2 = {**d, "score": round(sc, 3)}
        out.append(d2)
        if len(out) >= top_k:
            break
    return out


def context_for(cfg: dict, query: str, direction: str = "", max_chars: int = 700) -> str:
    """检索并组装可注入 prompt 的素材块（方向加权 + 字符预算 + 禁用道具过滤）。

    过滤是必需的：这条路径是 kb.recall / memory.recall 都失败时的兜底，
    直接读 RAG 索引（知识卡/拆书/教训），里面有大量账本类桥段；不过滤就会
    把用户明确禁用的道具注入提示词（实测该路径会注入含「账本」条目）。
    """
    from .kb import _is_negative, _negative_terms

    neg_terms = _negative_terms(cfg)
    hits = search(cfg, f"{query} {direction}", top_k=24)
    if direction:
        hits.sort(key=lambda d: (d.get("direction") != direction, -d.get("score", 0)))
    lines, used = [], 0
    seen = set()
    for d in hits:
        text = d.get("text", "") or ""
        if _is_negative(text, neg_terms):
            continue
        key = (d.get("type"), text[:50])
        if key in seen:
            continue
        seen.add(key)
        line = f"- [{d.get('source')}·{d.get('type')}] {text[:100]}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
