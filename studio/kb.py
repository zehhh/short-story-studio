# -*- coding: utf-8 -*-
"""知识系统：结构化拆解单元 + 向量索引 + PPMI 共现矩阵联想 + 三路融合检索。

四层架构（设计文档 docs/knowledge_system_plan.md）：
  ①结构拆解层（arc/lead_pattern 标注，在 deconstruct.py 完成）
  ②向量层      units.jsonl + vectors.npy（qwen3.5-embedding MRL 512 维，numpy 余弦）
  ③共现关联层  tag-tag PPMI 稀疏矩阵（离线预计算）+ 扩散激活（在线查表，钟型阻尼）
  ④引力检索层  BM25 种子 + 向量 KNN + 共现扩散 三路 RRF 融合，按叙事弧位定向加权

存储全部在 <workspace>/kb/ 下（E 盘），零第三方依赖（numpy 用于矩阵运算）。
"""
from __future__ import annotations

import itertools
import json
import math
import os
import re
import threading
from collections import Counter
from pathlib import Path

EMBED_MODEL = "qwen3.5-embedding"
EMBED_DIM = 512          # MRL 裁剪维度
DAMP = 0.5               # 扩散激活钟型阻尼
HOPS = 2                 # 扩散步数
RRF_K = 60
PPMI_MIN = 0.0           # PPMI 下限（负值截断）

# ---------------------------------------------------------------------------
# 召回负面词黑名单：命中即丢弃该条目，防止套路化道具反复注入。
# 参考 RAGuard / InfoGain-RAG 的"检索后过滤"思路：词法黑名单是最便宜的
# 第一道过滤层，在 rerank 之前就把不想要的套路条目剔除。
# 词表唯一事实源是 config.kb.negative_terms（见 _negative_terms 委托 langqa）。
# ---------------------------------------------------------------------------


def _negative_terms(cfg: dict | None) -> list[str]:
    """禁用道具词表（唯一事实源 = config.kb.negative_terms）。

    历史缺陷：这里曾内置一份 DEFAULT_NEGATIVE_TERMS 兜底，与 langqa.banned_terms
    各自成表，导致「小账本/掏小本/翻开账本/记账」只在召回侧被过滤、成稿扫描扫不到，
    而「账单/流水账/账页/存折/存单/汇款单/借条」只在成稿侧生效、召回不拦。
    现在两侧同源：langqa 是唯一词表持有者，这里只做委托（langqa 不反向依赖 kb，
    无循环导入）。
    """
    from .langqa import banned_terms

    return banned_terms(cfg)


def _is_negative(text: str, terms: list[str]) -> bool:
    """召回侧负面词判定。与成稿侧同源：复用 langqa 的成语白名单，
    否则「如意算盘」会被判成禁用道具，同题材素材被整条误杀。"""
    if not text:
        return False
    from .langqa import _mask_idioms

    masked = _mask_idioms(text)
    return any(t in masked for t in terms)


# ---------------------------------------------------------------------------
# 存储路径
# ---------------------------------------------------------------------------

def kb_dir(cfg: dict) -> Path:
    d = Path(cfg["studio"]["workspace_abs"]) / "kb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def units_path(cfg: dict) -> Path:
    return kb_dir(cfg) / "units.jsonl"


def vectors_path(cfg: dict) -> Path:
    return kb_dir(cfg) / "vectors.npy"


def reduced_vectors_path(cfg: dict, dim: int) -> Path:
    """MRL 截断后的检索矩阵缓存（默认 512 维，体积/延迟降 8 倍）。"""
    return kb_dir(cfg) / f"vectors_mrl{dim}.npy"


def offsets_path(cfg: dict) -> Path:
    return kb_dir(cfg) / "offsets.json"


def coco_path(cfg: dict) -> Path:
    return kb_dir(cfg) / "cocomatrix.json"


# ---------------------------------------------------------------------------
# embedding（qwen3.5-embedding，MRL 裁剪）
# ---------------------------------------------------------------------------

EMBED_NUM_GPU = 0         # 0.8B 向量模型跑 CPU：16GB 卡上省出 2.75GB 显存给主模型
                          # （实测 CPU 热调用 0.46s/4条短文本，GPU 仅 0.066s 但会把大模型挤出显存）


def _emb_cfg(cfg: dict | None) -> dict:
    """硅基流动 embedding/rerank 配置（config.yaml kb.embedding）。"""
    kb = ((cfg or {}).get("kb") or {})
    return dict(kb.get("embedding", {}) or {})


def _embed_texts(texts: list[str], cfg: dict | None = None) -> list[list[float]] | None:
    """批量 embedding：优先硅基 Qwen3-Embedding-8B API（config kb.embedding）；未配置走本地 Ollama。

    失败返回 None（调用方降级为纯词法/共现检索）。
    """
    import requests

    ec = _emb_cfg(cfg)
    if ec.get("base_url") and ec.get("api_key"):
        try:
            r = requests.post(
                f"{ec['base_url'].rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {ec['api_key']}",
                         "Content-Type": "application/json"},
                json={"model": ec.get("model", "Qwen/Qwen3-Embedding-8B"), "input": texts},
                timeout=120,
            )
            r.raise_for_status()
            embs = [d["embedding"] for d in r.json().get("data", [])]
            out = []
            for v in embs:
                v = v[: int(ec.get("dim", 4096))]
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / n for x in v])
            return out
        except Exception as e:  # noqa: BLE001
            print(f"[kb] 硅基 embedding 失败：{type(e).__name__} {str(e)[:80]}")
            return None
    # 本地 Ollama 兜底（旧路径）
    try:
        r = requests.post("http://127.0.0.1:11434/api/embed",
                          json={"model": EMBED_MODEL, "input": texts, "keep_alive": "10m",
                                "options": {"num_gpu": EMBED_NUM_GPU}},
                          timeout=300)
        r.raise_for_status()
        embs = r.json().get("embeddings") or []
        out = []
        for v in embs:
            v = v[:EMBED_DIM]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[kb] embedding 失败：{type(e).__name__} {str(e)[:80]}")
        return None


def _rerank(query: str, docs: list[str], cfg: dict | None = None,
            top_n: int | None = None) -> list[tuple[int, float]] | None:
    """硅基 Qwen3-Reranker 精排：返回 [(doc_index, score)] 按分降序。未配置返回 None。
    doc_index 为传入 docs 的原始索引（空文档已过滤，索引仍映射回原位置）。"""
    import requests

    ec = _emb_cfg(cfg)
    if not (ec.get("base_url") and ec.get("api_key") and ec.get("rerank_model")):
        return None
    if not query or not query.strip():
        return None
    # 硅基 rerank 要求每个文档字符串 ≥1 字符，混入空文档会整批 400（code 20015），故过滤。
    valid = [(i, d) for i, d in enumerate(docs) if d and str(d).strip()]
    if not valid:
        return None
    vdocs = [d for _, d in valid]
    try:
        r = requests.post(
            f"{ec['base_url'].rstrip('/')}/rerank",
            headers={"Authorization": f"Bearer {ec['api_key']}",
                     "Content-Type": "application/json"},
            json={"model": ec["rerank_model"], "query": query, "documents": vdocs,
                  "top_n": top_n or len(vdocs)},
            timeout=60,
        )
        r.raise_for_status()
        return [(valid[x["index"]][0], float(x.get("relevance_score", 0.0)))
                for x in r.json().get("results", [])]
    except Exception as e:  # noqa: BLE001
        print(f"[kb] rerank 失败：{type(e).__name__} {str(e)[:80]}")
        return None


# ---------------------------------------------------------------------------
# 知识单元：增删查
# ---------------------------------------------------------------------------

# 进程内读缓存（mtime+size 签名，任何写入都会改签名自动失效）：
# units.jsonl 12MB 全量解析 105ms/次、vectors.npy 43MB 加载、cocomatrix 解析——
# recall/stats/knn 每次调用都曾重复付出，深拆到几万单元时成为主要卡顿源。
_UNITS_CACHE: dict = {"sig": None, "units": None}


def load_units(cfg: dict) -> list[dict]:
    """读取全部知识单元（mtime 缓存；返回只读约定，写入方必须走 add_unit/apply）。"""
    p = units_path(cfg)
    if not p.exists():
        return []
    sig = (p.stat().st_mtime_ns, p.stat().st_size)
    if _UNITS_CACHE["sig"] != sig or _UNITS_CACHE["units"] is None:
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        _UNITS_CACHE["units"] = out
        _UNITS_CACHE["sig"] = sig
    return _UNITS_CACHE["units"]


def _fp(text: str) -> str:
    import hashlib

    return hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 题材标签体系：正交于 direction（叙事套路）的"世界观/背景"维度。
# 一条内容可命中多个题材（复合题材），recall 时任一命中即召回。
# 标签表 data/genres.json，纯词法匹配（快、可复现、无 LLM 成本）。
# ---------------------------------------------------------------------------

_GENRES_CACHE: dict = {"sig": None, "items": []}


def load_genres() -> list[dict]:
    """加载题材标签表（data/genres.json），mtime 签名缓存。"""
    p = Path(__file__).resolve().parent.parent / "data" / "genres.json"
    try:
        sig = (p.stat().st_mtime_ns, p.stat().st_size)
    except OSError:
        return []
    if _GENRES_CACHE["sig"] != sig:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _GENRES_CACHE["items"] = data.get("genres", [])
            _GENRES_CACHE["sig"] = sig
        except (json.JSONDecodeError, OSError):
            _GENRES_CACHE["items"] = []
            _GENRES_CACHE["sig"] = None
    return _GENRES_CACHE["items"]


def tag_genres(text: str, direction: str = "", max_tags: int = 4) -> list[str]:
    """给一段内容打题材标签：别名词法命中（文本+方向串），返回题材 id 列表。

    - 方向串里已含明确题材词（如'怪诞修仙脑洞'→修仙）时直接命中
    - 文本关键词兜底（如'灵根''筑基'→修仙；'王府''圣旨'→古言）
    - 最多 max_tags 个（取命中优先级：方向命中 > 文本多词命中 > 文本单词命中）
    """
    text = (text or "")[:400]
    dir_str = str(direction or "")[:200]
    if not text and not dir_str:
        return []
    # 方向命中优先（权重 +2），文本单词命中 +1
    scores: dict[str, int] = {}
    for g in load_genres():
        aliases = g.get("aliases") or []
        d_hit = sum(1 for a in aliases if a and a in dir_str)
        if d_hit:
            scores[g["id"]] = scores.get(g["id"], 0) + 2 * min(d_hit, 3)
        t_hit = sum(1 for a in aliases if a and len(a) >= 2 and a in text)
        if t_hit:
            scores[g["id"]] = scores.get(g["id"], 0) + min(t_hit, 3)
    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [gid for gid, _ in ranked[:max_tags]]


def unit_genres(u: dict) -> list[str]:
    """读单元已有 tags 中的题材标签；若为空则现打标（不落库，仅检索期使用）。"""
    tags = u.get("tags") or []
    gids = [t for t in tags if t in {g["id"] for g in load_genres()}]
    if gids:
        return gids
    return tag_genres(u.get("text", ""), u.get("direction", ""))


# 知识库写锁：多线程批量拆书时 add_unit / index_pending / rebuild_cocomatrix 都可能
# 并发进来；RLock 允许 ingest_decon 内层重入。文件写入配套 append-only / 原子替换。
_IO_LOCK = threading.RLock()
_FP_SEEN: dict[str, set] = {}      # kb目录 → 已入库指纹集合（kind|text，进程内缓存）
_NORM_SEEN: dict[str, set] = {}    # kb目录 → 已入库正则化文本集合（跨 kind 同文拦截）


def _seen_fps(cfg: dict) -> set:
    key = str(units_path(cfg))
    if key not in _FP_SEEN:
        _FP_SEEN[key] = {u.get("fp") or "" for u in load_units(cfg)}
    return _FP_SEEN[key]


def _max_id_num(cfg: dict) -> int:
    """已入库的最大 id 序号（`U%06d`）。

    历史缺陷（docs/audit/2026-10-25_代码审计.md P1-5）：id 曾用
    `"U" + len(seen)+1` 生成。seen 是「当前去重集合大小」，一旦中间有单元被删
    （kb_dedup apply 压实、人工清理脏数据），长度就不再等于最大序号，新 id 会
    直接撞上仍存在的旧 id。而向量行是按 id 对齐的（`offsets.json.rows`），撞号
    等于两条知识共用一行向量，检索召回串味且无法察觉。
    改为「取现有 id 的最大序号 + 1」，与集合大小解耦。
    """
    best = 0
    for u in load_units(cfg):
        uid = str(u.get("id") or "")
        if uid.startswith("U") and uid[1:].isdigit():
            best = max(best, int(uid[1:]))
    return best


def _seen_norms(cfg: dict) -> set:
    key = str(units_path(cfg))
    if key not in _NORM_SEEN:
        _NORM_SEEN[key] = {re.sub(r"[^一-龥a-z0-9]", "", (u.get("text") or "").lower())
                           for u in load_units(cfg)}
    return _NORM_SEEN[key]


def add_unit(cfg: dict, kind: str, text: str, arc: str = "", direction: str = "",
             emotion: str = "", tags: list[str] | None = None,
             source: str = "", quality: str = "mid") -> dict | None:
    """写入一条知识单元（指纹+跨kind同文双重去重，返回 None 表示重复）。append-only，O(1) 写。"""
    text = (text or "").strip()
    if len(text) < 8:
        return None
    fp = _fp(f"{kind}|{text}")
    norm_text = re.sub(r"[^一-龥a-z0-9]", "", text.lower())
    with _IO_LOCK:
        seen = _seen_fps(cfg)
        if fp in seen:
            return None
        # 跨 kind 同文拦截（指纹含 kind，同一句话挂 hook/quote 两处会漏）
        if len(norm_text) >= 12 and norm_text in _seen_norms(cfg):
            return None
        unit = {
            "id": "U" + f"{_max_id_num(cfg) + 1:06d}",
            "kind": kind, "arc": arc, "direction": direction or "未分类",
            "emotion": emotion, "text": text[:400],
            "tags": sorted(set(tags or []) | set(tag_genres(text, direction))),
            "source": source[:40], "quality": quality,
            "heat": 0, "fp": fp,
        }
        p = units_path(cfg)
        needs_nl = False
        if p.exists() and p.stat().st_size > 0:
            with open(p, "rb") as f:          # 上次进程被杀可能留下无换行符的撕裂行
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"
        with open(p, "a", encoding="utf-8") as f:
            if needs_nl:
                f.write("\n")                 # 隔离撕裂行（load_units 会跳过它）
            f.write(json.dumps(unit, ensure_ascii=False) + "\n")
        seen.add(fp)
        if len(norm_text) >= 12:
            _seen_norms(cfg).add(norm_text)
    return unit


def unit_text_for_embed(u: dict) -> str:
    """向量化文本模板：结构信息编进文本，同弧位情节在向量空间聚拢。"""
    return f"[{u.get('direction', '')}|{u.get('arc', '')}|{u.get('emotion', '')}] {u.get('text', '')}"


# ---------------------------------------------------------------------------
# 向量索引：增量编码 + 全量重建 + KNN
# ---------------------------------------------------------------------------

def _atomic_save_npy(p: Path, mat) -> None:
    """写临时文件后原子替换，避免并发读（knn）撞上写一半的矩阵。"""
    import numpy as np

    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "wb") as f:
        np.save(f, mat)
    os.replace(tmp, p)


def index_pending(cfg: dict, batch_size: int = 32) -> int:
    """为还没有向量的单元编码并追加到矩阵。返回新编码条数。

    offsets 里维护 lines（已索引的原始行数）：append-only 之下只需解析尾部新行，
    不必每次全量 load_units（几万单元规模下这里曾是每篇一次的 O(n) 解析热点）。
    双保险：无论游标是否因外部重写而失准，都按 rows 里已有的 id 过滤，
    保证绝不给同一单元建两条向量。
    """
    import numpy as np

    with _IO_LOCK:
        done = offsets_path(cfg)
        off = json.loads(done.read_text(encoding="utf-8")) if done.exists() else {"rows": [], "count": 0}
        rows: list[str] = off.get("rows", [])
        known = int(off.get("lines", -1))
        rowset = set(rows)
        up = units_path(cfg)
        try:
            mat = np.load(vectors_path(cfg))
        except Exception:  # noqa: BLE001
            init_dim = int(_emb_cfg(cfg).get("dim", EMBED_DIM))
            mat = np.zeros((0, init_dim), dtype=np.float32)
        all_lines = up.read_text(encoding="utf-8").splitlines() if up.exists() else []
        if known >= 0:
            pending = []
            for line in all_lines[known:]:
                if not line.strip():
                    continue
                try:
                    pending.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        else:
            pending = []
            for line in all_lines:
                if not line.strip():
                    continue
                try:
                    u = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if u.get("id") not in rowset:
                    pending.append(u)
        pending = [u for u in pending if u.get("id") and u["id"] not in rowset]
        if not pending:
            return 0
        added = 0
        new_rows = []
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            embs = _embed_texts([unit_text_for_embed(u) for u in batch], cfg)
            if embs is None:
                break
            mat = np.vstack([mat, np.array(embs, dtype=np.float32)])
            new_rows.extend(u["id"] for u in batch)
            added += len(batch)
        if added:
            _atomic_save_npy(vectors_path(cfg), mat)
            off["rows"] = rows + new_rows
            off["count"] = int(mat.shape[0])
            off["lines"] = len(all_lines)   # 原始行游标；并发追加只会让下次多解析几行，rows 过滤兜底
            done.write_text(json.dumps(off), encoding="utf-8")
        return added


def repair_index(cfg: dict) -> dict:
    """向量索引修复：按现存单元重建 rows（id 去重、按单元顺序对齐），缺失的补编码。

    用于治理历史遗留的重复向量行/游标失准；不重新编码已有向量，秒级完成。
    注意：多进程同时写库时请先停其他写入方（同 kb dedup --apply 的前提）。
    """
    import numpy as np

    with _IO_LOCK:
        units = load_units(cfg)
        try:
            mat = np.load(vectors_path(cfg))
            rows = json.loads(offsets_path(cfg).read_text(encoding="utf-8")).get("rows", [])
        except Exception:  # noqa: BLE001
            mat, rows = None, []
        id2row: dict[str, int] = {}
        for r, uid in enumerate(rows):
            id2row.setdefault(uid, r)
        keep_rows, keep_vecs = [], []
        for u in units:
            r = id2row.get(u.get("id", ""))
            if r is not None and mat is not None and r < mat.shape[0]:
                keep_rows.append(u["id"])
                keep_vecs.append(mat[r])
        if keep_vecs:
            m = np.vstack(keep_vecs).astype(np.float32)
            _atomic_save_npy(vectors_path(cfg), m)
            # 不写 lines 游标：强制 index_pending 走全量 id 扫描，补齐所有缺失向量
            offsets_path(cfg).write_text(
                json.dumps({"rows": keep_rows, "count": int(m.shape[0])}),
                encoding="utf-8")
        _FP_SEEN.clear()
        _NORM_SEEN.clear()
        n = index_pending(cfg)
    return {"units": len(units), "rows": len(keep_rows), "reindexed": n}


def rebuild(cfg: dict) -> int:
    """全量重建向量索引。"""
    for f in (vectors_path(cfg), offsets_path(cfg)):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    return index_pending(cfg)


_VEC_CACHE: dict = {"sig": None, "mat": None, "rows": []}


def _search_dim(cfg: dict) -> int:
    """检索维度：0=全维；配置 kb.embedding.search_dim 可用 MRL 截断（Qwen3 支持）。"""
    try:
        return int((_emb_cfg(cfg).get("search_dim") or 0) or 0)
    except (TypeError, ValueError):
        return 0


def _build_reduced_vectors(vp: Path, rp: Path, dim: int) -> None:
    """把全维向量按 MRL 截断到前 dim 维并逐行重归一化（分块写 memmap，峰值内存低）。"""
    import numpy as np

    full = np.load(vp, mmap_mode="r")
    n = int(full.shape[0])
    out = np.lib.format.open_memmap(rp, mode="w+", dtype=np.float32, shape=(n, dim))
    step = 8192
    for i in range(0, n, step):
        blk = np.asarray(full[i: i + step, :dim], dtype=np.float32)
        norms = np.linalg.norm(blk, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        out[i: i + step] = blk / norms
    out.flush()
    del out


def _load_vectors_cached(cfg: dict):
    """向量矩阵 + 行表（mtime 签名缓存；mmap 加载，避免每次查询拷贝 GB 级内存）。

    kb.embedding.search_dim>0 时使用 MRL 截断矩阵（默认 512 维）：
    体积/扫描耗时降 8 倍，Qwen3-Embedding 的 MRL 特性保证余弦相关度仍 >0.94。
    """
    import numpy as np

    vp, op = vectors_path(cfg), offsets_path(cfg)
    if not vp.exists() or not op.exists():
        return None, []
    dim = _search_dim(cfg)
    target = vp
    if dim and dim < int(np.load(vp, mmap_mode="r").shape[1]):
        rp = reduced_vectors_path(cfg, dim)
        try:
            if (not rp.exists()) or rp.stat().st_mtime_ns < vp.stat().st_mtime_ns:
                _build_reduced_vectors(vp, rp, dim)
            target = rp
        except Exception:  # noqa: BLE001  截断失败则退回全维，不影响功能
            target = vp
    sig = (target.stat().st_mtime_ns, target.stat().st_size,
           op.stat().st_mtime_ns, op.stat().st_size)
    if _VEC_CACHE["sig"] != sig or _VEC_CACHE["mat"] is None:
        try:
            mat = np.load(target, mmap_mode="r")
            rows = json.loads(op.read_text(encoding="utf-8")).get("rows", [])
        except Exception:  # noqa: BLE001
            return None, []
        _VEC_CACHE.update({"sig": sig, "mat": mat, "rows": rows})
    return _VEC_CACHE["mat"], _VEC_CACHE["rows"]


def _dir_hit(unit_dir: str, q_dir: str, unit_tags: list | None = None) -> bool:
    """方向命中判断：精确相等或包含匹配（单元标签为复合写法时仍可召回）。

    单元库早期入库把修仙/仙侠等内容标成几十种复合标签（如
    '重生复仇（修仙界/高维打脸）'、'县城职场反杀 / 婚恋博弈 / 网文修真'），
    查询方向为'修仙'时精确匹配会全部落空——这里改为：查询方向词
    出现在单元方向标签里即算命中；若单元挂了题材标签（tags），题材标签
    与查询方向同名也命中（复合题材内容可被多个题材召回）。
    """
    if not q_dir:
        return True
    if not unit_dir:
        return bool(unit_tags and q_dir in unit_tags)
    if unit_dir == q_dir:
        return True
    if q_dir in unit_dir:
        return True
    if unit_tags and q_dir in set(unit_tags):
        return True
    return False


def knn(cfg: dict, query: str, top_k: int = 20, arc: str = "",
        direction: str = "") -> list[dict]:
    """向量 KNN。返回 [{unit, score}]，弧位/方向命中加成。"""
    import numpy as np

    units = load_units(cfg)
    off_f = offsets_path(cfg)
    if not off_f.exists() or not units:
        return []
    mat, rows = _load_vectors_cached(cfg)
    if mat is None:
        return []
    q = _embed_texts([query], cfg)
    if not q:
        return []
    qv = np.array(q, dtype=np.float32)[0]
    if mat.shape[0] == 0:
        return []
    # MRL 截断矩阵：查询向量同维截断并重归一化
    if qv.shape[0] > mat.shape[1]:
        qv = qv[: mat.shape[1]]
        nrm = float(np.linalg.norm(qv)) or 1.0
        qv = qv / nrm
    sims = mat @ qv
    order = np.argsort(-sims)[: top_k * 2]
    id2unit = {u["id"]: u for u in units}
    out = []
    for idx in order:
        if idx >= len(rows):
            continue
        u = id2unit.get(rows[idx])
        if not u:
            continue
        sc = float(sims[idx])
        if arc and u.get("arc") == arc:
            sc += 0.06
        if direction and _dir_hit(u.get("direction", ""), direction, u.get("tags")):
            sc += 0.08
        out.append({"unit": u, "score": round(sc, 4)})
    out.sort(key=lambda x: -x["score"])
    return out[:top_k]


# ---------------------------------------------------------------------------
# PPMI 共现矩阵 + 扩散激活（VCP 河道算法）
# ---------------------------------------------------------------------------

def _nodes_of(u: dict) -> list[str]:
    """单元的河道节点：tags + arc + emotion + direction。"""
    ns = [t for t in (u.get("tags") or []) if t]
    for k in ("arc", "emotion", "direction"):
        if u.get(k):
            ns.append(str(u[k]))
    return sorted(set(ns))


def rebuild_cocomatrix(cfg: dict) -> dict:
    """离线预计算 tag-tag PPMI 稀疏矩阵。返回统计。"""
    with _IO_LOCK:
        return _rebuild_cocomatrix_locked(cfg)


def _rebuild_cocomatrix_locked(cfg: dict) -> dict:
    units = load_units(cfg)
    co: Counter = Counter()      # (a,b) 共现次数，a<b 有序对
    freq: Counter = Counter()    # 节点出现次数（按单元）
    n = 0
    for u in units:
        ns = _nodes_of(u)
        n += 1
        for x in ns:
            freq[x] += 1
        for i, a in enumerate(ns):
            for b in ns[i + 1:]:
                key = (a, b) if a < b else (b, a)
                co[key] += 1
    # PPMI
    matrix: dict[str, dict[str, float]] = {}
    for (a, b), c in co.items():
        pmi = math.log((c * n) / max(freq[a] * freq[b], 1))
        w = max(PPMI_MIN, pmi)
        if w <= 0:
            continue
        matrix.setdefault(a, {})[b] = round(w, 3)
        matrix.setdefault(b, {})[a] = round(w, 3)
    coco_path(cfg).write_text(json.dumps(
        {"n_units": n, "matrix": matrix}, ensure_ascii=False), encoding="utf-8")
    return {"n_units": n, "nodes": len(freq), "edges": sum(len(v) for v in matrix.values()) // 2}


_COCO_CACHE: dict = {"sig": None, "matrix": None}


def _load_cocomatrix(cfg: dict) -> dict:
    p = coco_path(cfg)
    if not p.exists():
        try:
            rebuild_cocomatrix(cfg)
        except Exception:  # noqa: BLE001
            return {}
    sig = (p.stat().st_mtime_ns, p.stat().st_size)
    if _COCO_CACHE["sig"] == sig and _COCO_CACHE["matrix"] is not None:
        return _COCO_CACHE["matrix"]
    try:
        matrix = json.loads(p.read_text(encoding="utf-8")).get("matrix", {})
        _COCO_CACHE.update({"sig": sig, "matrix": matrix})
        return matrix
    except Exception:  # noqa: BLE001
        return {}


def spread_activation(cfg: dict, seed_nodes: list[str], top_m: int = 8) -> dict[str, float]:
    """扩散激活：种子节点沿 PPMI 河道传播，钟型阻尼，HOPS 跳截断。返回激活节点→值。"""
    matrix = _load_cocomatrix(cfg)
    act: dict[str, float] = {s: 1.0 for s in seed_nodes if s in matrix or True}
    frontier = dict(act)
    for hop in range(1, HOPS + 1):
        nxt: dict[str, float] = {}
        for u, a in frontier.items():
            # 扇出上限：hub 节点（起/高潮等泛化 tag）边数大，会把激活撒满全网
            # 【v2】从12降到8，减少过度扩散
            nbrs = sorted(matrix.get(u, {}).items(), key=lambda x: -x[1])[:8]
            for v, w in nbrs:
                gain = a * w * (DAMP ** hop)
                if gain > 0.05:
                    nxt[v] = nxt.get(v, 0.0) + gain
        frontier = {k: v for k, v in nxt.items() if k not in act}
        for k, v in frontier.items():
            act[k] = act.get(k, 0.0) + v
        if not frontier:
            break
    act = {k: v for k, v in act.items() if k not in seed_nodes}
    return dict(sorted(act.items(), key=lambda x: -x[1])[:top_m])


# ---------------------------------------------------------------------------
# 三路融合检索（BM25 + 向量 KNN + 共现扩散）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 库内词法索引：BM25 变体（int32 压缩倒排，进程内缓存）
# 旧的 BM25 腿借道 rag.search，语料只有素材库+知识卡（千余条），16 万单元不在
# 其中，命中还要靠 text 前 60 字回映射，清掉自产沉淀后这一路长期空转。
# ---------------------------------------------------------------------------
_LEX_CACHE: dict = {"sig": None, "post": None, "dl": None, "N": 0, "avg": 1.0}


def _bigrams(text: str) -> list[str]:
    text = (text or "").lower()
    cjk = re.findall(r"[一-龥]", text)
    out = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    out += re.findall(r"[a-z0-9]{2,}", text)
    return out or cjk


def _lex_index(cfg: dict) -> dict:
    import array

    up = units_path(cfg)
    sig = None
    if up.exists():
        st = up.stat()
        sig = (st.st_mtime_ns, st.st_size)
    if _LEX_CACHE["sig"] == sig and _LEX_CACHE["post"] is not None:
        return _LEX_CACHE
    units = load_units(cfg)
    from collections import Counter

    post: dict[str, "array.array"] = {}
    dl = array.array("i")
    for i, u in enumerate(units):
        toks = _bigrams(u.get("text", ""))
        dl.append(len(toks) or 1)
        # 一次 Counter 统计词频：原实现对每个 unique token 再 toks.count(t)，
        # 16 万单元下是 O(n²)，冷启动要 13s；改后约 2-3s。
        for t, c in Counter(toks).items():
            a = post.get(t)
            if a is None:
                a = post[t] = array.array("i")
            a.append(i * 8 + min(c - 1, 7))   # 低 3 位存 tf-1，其余位存行号
    _LEX_CACHE.update(sig=sig, post=post, dl=dl, N=len(units),
                      avg=(sum(dl) / len(dl)) if dl else 1.0)
    return _LEX_CACHE


def _lex_search(cfg: dict, query: str, top_k: int = 15) -> list[int]:
    """BM25(okapi, k1=1.2, b=0.75) 打分，返回命中的 units 下标（按分降序）。"""
    L = _lex_index(cfg)
    if not L["N"]:
        return []
    N, avg = L["N"], L["avg"]
    scores: dict[int, float] = {}
    for t in set(_bigrams(query)):
        a = L["post"].get(t)
        if not a:
            continue
        df = len(a)
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        for packed in a:
            i, tf = packed >> 3, (packed & 7) + 1
            dl = L["dl"][i]
            scores[i] = scores.get(i, 0.0) + idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * dl / avg))
    return [i for i, _ in sorted(scores.items(), key=lambda x: -x[1])][:top_k]


def recall(cfg: dict, query: str, arc: str = "", direction: str = "",
           top_k: int = 12, max_chars: int = 600) -> tuple[str, list[str]]:
    """知识引力检索。返回 (注入块, 命中单元 id)。三路 RRF 融合 + 弧位定向加权。"""
    units = load_units(cfg)
    if not units:
        return "", []
    id2unit = {u["id"]: u for u in units}

    # 路1：库内词法 BM25（压缩倒排，直接打分 16 万单元）
    bm_ids = [units[i]["id"] for i in _lex_search(cfg, f"{query} {direction}", top_k=15)]

    # 路2：向量 KNN
    kn = knn(cfg, f"{query} {direction} {arc}", top_k=15, arc=arc, direction=direction)

    # 路3：共现扩散——种子 = 查询命中的单元 tags + 弧/情绪节点
    seed_nodes = [n for n in (arc, direction) if n]
    for hit in kn[:5]:
        seed_nodes.extend(_nodes_of(hit["unit"]))
    seeds_top = [k for k, _ in Counter(seed_nodes).most_common(8)]
    activated = spread_activation(cfg, seeds_top)
    # 激活节点回查单元（节点命中数计分）
    sp: dict[str, float] = {}
    for u in units:
        overlap = len(set(_nodes_of(u)) & set(activated))
        if overlap:
            sp[u["id"]] = overlap * 1.0 + activated.get(u.get("direction", ""), 0) * 0.1
    # 3b 向量近邻二环：top 命中的 embedding 近邻也加分——纯 tag 共现只连
    # "同单元出现过的节点"，跨来源/跨批次的语义近似单元永远连不上（如手工
    # 桥段库与拆书语料），语义扩散补这个洞
    try:
        import numpy as np

        mat, vrows = _load_vectors_cached(cfg)
        row_of = {uid: r for r, uid in enumerate(vrows or [])}
        if mat is not None:
            for hit in kn[:3]:
                r = row_of.get(hit["unit"]["id"])
                if r is None or r >= mat.shape[0]:
                    continue
                sims = mat @ mat[r]
                sims[r] = -1.0
                for idx in np.argsort(-sims)[:6]:
                    if idx >= len(vrows) or sims[idx] <= 0.6:
                        break
                    sp[vrows[idx]] = max(sp.get(vrows[idx], 0.0), float(sims[idx]) * 1.2)
    except Exception:  # noqa: BLE001
        pass

    # RRF 融合
    rrf: dict[str, float] = {}
    neg_terms = _negative_terms(cfg)
    def _rrf_add(rank_list: list[str]):
        for rank, uid in enumerate(rank_list):
            rrf[uid] = rrf.get(uid, 0.0) + 1.0 / (RRF_K + rank + 1)

    def _rank_ids_by_scores(scores: dict[str, float]) -> list[str]:
        return [k for k, _ in sorted(scores.items(), key=lambda x: -x[1])]

    _rrf_add(bm_ids[:15])
    _rrf_add([h["unit"]["id"] for h in kn])
    _rrf_add(_rank_ids_by_scores(sp))
    # PRF 伪相关反馈（RM3）：首轮 top 命中的 tags/弧位并入查询再检一轮词法，
    # 解决口语查询与库内术语的词汇差（"闹分手"要能命中"分别/定情"桥段）
    # 【v2】PRF 召回的条目如果命中负面词黑名单，直接丢弃——否则首轮偶然命中
    # 一条账本条目，PRF 会把"账本/记账"tags 并入查询，放大召回更多账本条目。
    _first = _rank_ids_by_scores(rrf)[:3]
    if _first:
        prf_q = [query]
        for uid in _first:
            u0 = id2unit.get(uid) or {}
            prf_q.extend((u0.get("tags") or [])[:4])
            if u0.get("arc"):
                prf_q.append(str(u0["arc"]))
        prf_raw = [units[i]["id"] for i in _lex_search(cfg, " ".join(prf_q), top_k=10)]
        # PRF 二次负面词过滤
        prf_ids = []
        for uid in prf_raw:
            u0 = id2unit.get(uid) or {}
            if not _is_negative(u0.get("text", ""), neg_terms):
                prf_ids.append(uid)
        _rrf_add(prf_ids[:10])

    # 弧位定向加成
    for uid in list(rrf):
        u = id2unit.get(uid)
        if u and arc and u.get("arc") == arc:
            rrf[uid] *= 1.5

    ranked = _rank_ids_by_scores(rrf)[: top_k * 3]
    # 方向优先序：三路里只有向量 KNN 带方向加成，BM25 与 PRF 两路是纯词法，
    # 会把「分家赡养」这类小众方向（354 条）挤成 2/15，剩下全被泛化条目占满。
    # 这里在 MMR 之前做一次稳定分区：方向命中的排前面，非命中的留作兜底
    # （小众方向候选不足时仍能补满 top_k，不牺牲召回）。
    if direction:
        def _dir_rank(uid: str) -> int:
            u = id2unit.get(uid) or {}
            return 0 if _dir_hit(u.get("direction", ""), direction, u.get("tags")) else 1
        ranked = sorted(ranked, key=_dir_rank)   # sorted 稳定：同组内保持 RRF 原序
    # MMR 式多样性：拆书语料里同一段爆款的近似变体很多，字符二元组 Jaccard
    # 过高（>0.5）的只留分最高的一条，避免 top-k 被同质条目霸占
    sel: list[str] = []
    sel_bgs: list[frozenset] = []
    for uid in ranked:
        u = id2unit.get(uid, {})
        if _is_negative(u.get("text", ""), neg_terms):
            continue
        bgs = frozenset(_bigrams(u.get("text", "")))
        if bgs and any(len(bgs & s) > 0.5 * len(bgs | s) for s in sel_bgs):
            continue
        sel.append(uid)
        sel_bgs.append(bgs)
        if len(sel) >= top_k:
            break
    # Rerank 精排（硅基 Qwen3-Reranker-8B）：MMR 去重后的候选按相关性重排，
    # 干掉"词面命中但语义跑题"的条目（如查假公主却召回账本书）；失败保持 RRF 序。
    try:
        rr = _rerank(query, [id2unit.get(uid, {}).get("text", "")[:300] for uid in sel], cfg)
        if rr:
            order = {uid: i for i, (idx, _) in enumerate(rr) for uid in [sel[idx]]}
            sel = [sel[i] for i in sorted(range(len(sel)), key=lambda j: order.get(sel[j], 999))]
    except Exception:  # noqa: BLE001
        pass
    ranked = sel
    lines, used, used_ch = [], [], 0
    for uid in ranked:
        u = id2unit.get(uid)
        if not u:
            continue
        tag = f"[{u.get('arc', '')}·{u.get('kind', '')}]" if u.get("arc") else f"[{u.get('kind', '')}]"
        line = f"- {tag} {u.get('text', '')[:130]}"
        if used_ch + len(line) > max_chars:
            break
        lines.append(line)
        used_ch += len(line)
        used.append(uid)
        # 使用即强化
    if used:
        try:
            _touch_ids(cfg, used)
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines), used


def heat_path(cfg: dict) -> Path:
    return kb_dir(cfg) / "heat.json"


def _load_heat(cfg: dict) -> dict:
    try:
        return json.loads(heat_path(cfg).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _touch_ids(cfg: dict, ids: list[str]):
    """使用即强化：heat 记 sidecar 文件，绝不重写 units.jsonl——
    全量重写会破坏 append-only 行游标（导致向量重复索引）并丢弃坏行。"""
    if not ids:
        return
    with _IO_LOCK:
        d = _load_heat(cfg)
        for i in ids:
            d[i] = int(d.get(i, 0)) + 1
        tmp = heat_path(cfg).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, heat_path(cfg))


# ---------------------------------------------------------------------------
# 入库适配器：拆书结果 / 素材库迁移
# ---------------------------------------------------------------------------

_COCO_TICK = itertools.count()   # 入库节拍：共现矩阵节流重建计数


def semantic_filter_new(cfg: dict, texts: list[str], threshold: float = 0.93) -> list[bool]:
    """入库前语义防重：返回与 texts 等长的掩码（True=可入库）。

    与库内已索引单元比对（cos ≥ threshold 判近重复），并对本批内部互查。
    embedding 失败时全部放行（防重降级，不阻塞入库）。
    """
    import numpy as np

    keep = [True] * len(texts)
    to_check = [i for i, t in enumerate(texts) if len((t or "").strip()) >= 16]
    if not to_check:
        return keep
    embs = _embed_texts([texts[i] for i in to_check], cfg)
    if embs is None:
        return keep
    q = np.array(embs, dtype=np.float32)
    try:
        mat, rows = _load_vectors_cached(cfg)
        if mat is not None and mat.shape[0] and len(rows) >= mat.shape[0]:
            qq = q
            if qq.shape[1] > mat.shape[1]:
                qq = qq[:, : mat.shape[1]]
                qq = qq / np.maximum(np.linalg.norm(qq, axis=1, keepdims=True), 1e-9)
            sims = mat @ qq.T                    # (已索引数, 候选数)
            for ci, i in enumerate(to_check):
                if float(sims[:, ci].max()) >= threshold:
                    keep[i] = False
    except Exception:  # noqa: BLE001
        pass
    for a in range(len(to_check)):                # 本批内部互查
        if not keep[to_check[a]]:
            continue
        for b in range(a + 1, len(to_check)):
            if keep[to_check[b]] and float(q[a] @ q[b]) >= threshold:
                keep[to_check[b]] = False
    return keep


def ingest_decon(cfg: dict, result: dict) -> int:
    """把拆书结果转成知识单元（含 arc 标注）。返回新增条数。

    入库前做双重防重：语义近重复过滤（对库内+本批）→ add_unit 指纹/同文拦截。
    """
    direction = result.get("direction", "")
    src = f"拆书:{result.get('title', '')[:30]}"
    cands: list[tuple[str, str, str, str, list]] = []
    for s_ in result.get("sections", []):
        arc = s_.get("arc", "")
        htype = (s_.get("hook_type") or "").strip()
        stype = (s_.get("shuang_type") or "").strip()
        if s_.get("hook"):
            htags = ["拆书", arc] + ([f"钩:{htype}"] if htype else [])
            cands.append(("hook", str(s_["hook"])[:200], arc,
                          "虐" if s_.get("nue") else "爽", htags))
        if s_.get("conflict"):
            seg = f"{s_.get('goal', '')}｜冲突：{s_['conflict']}"
            if s_.get("escalation"):
                seg += f"｜升级：{s_['escalation']}"
            if s_.get("emotion"):
                seg += f"｜情绪：{s_['emotion']}"
            if s_.get("info_gap"):
                seg += f"｜信息差：{s_['info_gap']}"
            stag = ["拆书", arc] + ([f"爽:{stype}"] if stype and stype != "无" else [])
            cands.append(("arc_segment", seg[:300], arc, "", stag))
        if s_.get("persona"):
            cands.append(("arc_segment", f"人设：{s_['persona']}"[:200], arc, "", ["拆书", arc, "人设"]))
        if s_.get("technique"):
            cands.append(("technique", str(s_["technique"])[:200], arc, "", ["拆书", arc, "技法"]))
        if s_.get("quote"):
            cands.append(("quote", str(s_["quote"])[:120], arc, "", ["拆书", arc, "金句"]))
    book = result.get("book") or {}
    for q in book.get("golden_quotes", []) or []:
        cands.append(("quote", str(q)[:120], "", "", ["拆书", "金句"]))
    for t in book.get("techniques", []) or []:
        cands.append(("technique", str(t)[:200], "", "", ["拆书", "技法"]))
    if not cands:
        return 0
    keep = semantic_filter_new(cfg, [c[1] for c in cands])
    n = 0
    for (kind, text, arc, emo, tags), k in zip(cands, keep):
        if not k:
            continue
        u = add_unit(cfg, kind, text, arc=arc, direction=direction, emotion=emo,
                     tags=tags, source=src)
        n += bool(u)
    if n:
        index_pending(cfg)
        # 共现矩阵全量重建在大批量拆书下是 O(全部单元) 热点，节流为约每 20 篇一次；
        # 随时可用 `python -m studio kb rebuild` 强制重建
        if next(_COCO_TICK) % 20 == 0:
            rebuild_cocomatrix(cfg)
    return n


def ingest_materials(cfg: dict, limit: int = 500) -> int:
    """存量素材库迁移为知识单元（指纹+跨kind同文+语义近重复三重去重）。"""
    from . import material

    items = material.query(cfg, limit=limit)
    if not items:
        return 0
    keep = semantic_filter_new(cfg, [str(x.get("text", ""))[:300] for x in items])
    n = 0
    for x, k in zip(items, keep):
        if not k:
            continue
        u = add_unit(cfg, "material", str(x.get("text", ""))[:300],
                     arc="", direction=x.get("direction", ""),
                     tags=list(x.get("tags") or [])[:6], source=f"素材:{x.get('type', '')}",
                     quality={"high": "high", "mid": "mid", "low": "low"}.get(x.get("quality"), "mid"))
        n += bool(u)
    if n:
        index_pending(cfg)
        if next(_COCO_TICK) % 5 == 0:
            rebuild_cocomatrix(cfg)
    return n


def stats(cfg: dict) -> dict:
    units = load_units(cfg)
    by_arc = Counter(u.get("arc", "—") for u in units)
    by_kind = Counter(u.get("kind", "—") for u in units)
    by_dir = Counter(u.get("direction", "—") for u in units)
    coco = _load_cocomatrix(cfg)
    vec_ok = offsets_path(cfg).exists()
    return {
        "units": len(units), "by_arc": dict(by_arc), "by_kind": dict(by_kind),
        "top_directions": dict(by_dir.most_common(6)),
        "vectors_indexed": (json.loads(offsets_path(cfg).read_text(encoding="utf-8")).get("count", 0)
                            if vec_ok else 0),
        "coco_nodes": len(coco),
    }


def tag_all(cfg: dict) -> dict:
    """全量题材打标：单元库 + 素材库所有内容补齐 tags（题材标签）字段。

    规则词法打标（data/genres.json），零 LLM 成本。逐行流式重写，写前备份。
    不打乱已有 tags 与 direction；未分类单元即使打不上题材也保持原 direction。
    """
    import shutil

    stats = {"units": 0, "units_updated": 0, "units_tagged": 0,
             "materials": 0, "materials_updated": 0, "materials_tagged": 0}

    def _retag_lines(path: Path):
        lines = path.read_text(encoding="utf-8").splitlines()
        out = []
        updated = 0
        tagged = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            text = str(obj.get("text", "") or "")
            direction = obj.get("direction", "") or ""
            if isinstance(direction, list):
                direction = " / ".join(str(x) for x in direction)
            gids = tag_genres(text, direction)
            old_tags = set(obj.get("tags") or [])
            new_tags = set(gids) | old_tags
            if new_tags and new_tags != old_tags:
                obj = dict(obj)
                obj["tags"] = sorted(new_tags)
                updated += 1
                tagged += 1
            out.append(json.dumps(obj, ensure_ascii=False))
        if updated:
            bak = path.with_suffix(path.suffix + ".bak3")
            if not bak.exists():
                shutil.copy2(path, bak)
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return len(out), updated, tagged

    up = units_path(cfg)
    stats["units"], stats["units_updated"], stats["units_tagged"] = _retag_lines(up)
    mp = Path(cfg["studio"]["workspace_abs"]) / "materials.jsonl"
    if mp.exists():
        stats["materials"], stats["materials_updated"], stats["materials_tagged"] = _retag_lines(mp)
    return stats


def arc_skeleton(cfg: dict, direction: str = "") -> str:
    """结构骨架视图：同方向已拆书目的弧线分布 + 弧内功能，供大纲对照。"""
    units = [u for u in load_units(cfg)
             if u.get("kind") == "arc_segment" and (not direction or _dir_hit(u.get("direction", ""), direction, u.get("tags")))]
    if not units:
        return ""
    by_book: dict[str, list] = {}
    for u in units:
        by_book.setdefault(u.get("source", ""), []).append(u)
    lines = []
    for src, us in list(by_book.items())[:3]:
        seq = sorted(us, key=lambda x: x.get("id", ""))
        arc_seq = [u.get("arc", "?") for u in seq]
        roles = [f"{u.get('arc', '?')}:{u.get('text', '')[:26]}" for u in seq if u.get("arc") in ("起", "转")]
        lines.append(f"- {src}（{len(seq)} 段弧序 {'-'.join(arc_seq[:8])}）关键节点：{'；'.join(roles[:4])}")
    return "\n".join(lines)


def lead_patterns(cfg: dict, direction: str = "", top_k: int = 6) -> str:
    """导语模式库：同方向高分 lead/开头钩子单元（含 lead_pattern 标注）。

    多样性优先：不再按 heat（使用次数）降序（那会让同一导语套路反复插队），
    改为 MMR 去重（字符二元组 Jaccard 过高的相似导语只留一条）。
    """
    units = load_units(cfg)
    pool = [u for u in units
            if (u.get("kind") in ("lead", "hook") or u.get("lead_pattern"))
            and (not direction or _dir_hit(u.get("direction", ""), direction, u.get("tags")))]
    if not pool:
        return ""
    sel, sel_bgs = [], []
    for u in pool:
        bgs = frozenset(_bigrams(u.get("text", "")))
        if bgs and any(len(bgs & s) > 0.5 * len(bgs | s) for s in sel_bgs):
            continue
        sel.append(u)
        sel_bgs.append(bgs)
        if len(sel) >= top_k:
            break
    lines = [f"- {('【' + u['lead_pattern'] + '】' if u.get('lead_pattern') else '')}{u.get('text', '')[:90]}"
             for u in sel]
    return "\n".join(lines)


# 风格范文：套路/数量词过滤（这些片段不适合当"自然语感"样板）
_STYLE_FARM = ("灵田", "菜地", "喂鸡", "土鸡", "走地鸡", "种田", "白菜", "种地", "养殖场")
_STYLE_NUM_EXPR = re.compile(r"[0-9]+|[二三四五六七八九十百千万两][大小]?[年岁日次步根月周天遍回声个枚层炷盏件位名更包]")
_STYLE_PLACEHOLDER = re.compile(r"(?:配角|主角|男主|女主|男配|女配|反派)\s*[0-9０-９]")
# 解读式伪原文：拆书流程会把"‘桥段名’：这是…／将…／用…反衬…"这类分析句
# 也标成 quote 入库。它们不是小说原文，拿去当"风格范文"会让写手模仿
# 评论腔而不是叙事腔（实测「县城相亲」方向 10 条候选里 4 条是这种）。
_STYLE_ANALYSIS = re.compile(
    r"^[‘'\"「][^’'\"」]{2,40}[’'\"」]\s*(?:[（(][^)）]{0,20}[)）])?\s*[:：]"
    r"|^[‘'\"「][^’'\"」]{2,40}[’'\"」]\s*[（(][^)）]{0,20}[)）]?\s*$"
)
# 评论腔标志词：正文里出现这些多半是拆书解读而非叙事
_STYLE_META = ("这是男主", "这是女主", "确立了", "反衬出", "体现了", "展示了",
               "揭示了", "让读者", "读者会", "爽点", "泪点", "钩子", "本章",
               "该书", "全文", "全书", "逻辑闭环", "原文金句", "题材常用",
               "示例：", "复用句", "摘要缺失", "不可提取")
# 叙事文里不会自称"主角/女主"：出现即说明是拆书稿的第三人称复述，
# 直接拿去当范文会让写手写出"主角手里拿着…"这种非小说腔。
_STYLE_ROLEWORD = re.compile(r"主角|女主|男主|反派|配角")

# 方向归一化：用户自由表述（如"女频修仙"）→ 库内 direction 词 / tag
_DIRECTION_ALIASES = [
    ("修仙", ["修仙", "仙侠", "修真", "飞升", "xiuxian"]),
    ("追妻", ["追妻", "火葬场"]),
    ("规则", ["规则", "怪谈", "守则", "lingyi"]),
    ("怪谈", ["怪谈", "规则", "lingyi"]),
    ("后宫", ["后宫", "买股", "鱼塘", "多男主"]),
    ("古言", ["古言", "宫廷", "权谋", "王爷", "guyan"]),
    ("大女主", ["大女主", "女强", "dazhunu"]),
    ("西幻", ["西幻", "魔法", "精灵", "xihuan"]),
    ("末世", ["末世", "囤货", "moshi"]),
    ("重生", ["重生", "zhongsheng"]),
    ("穿越", ["穿越", "穿书", "chuanyue"]),
    ("悬疑", ["悬疑", "推理", "xuanyi"]),
    ("婆媳", ["婆媳", "婆婆"]),
    ("彩礼", ["彩礼", "退婚"]),
    ("都市", ["都市", "县城", "职场", "xiandai"]),
    ("年代", ["年代", "shidai"]),
    ("武侠", ["武侠", "wuxia"]),
]


def _direction_match(direction: str, unit: dict) -> bool:
    """归一化方向匹配（兼容"女频修仙"等自由表述）。"""
    if not direction:
        return True
    aliases = []
    for core, words in _DIRECTION_ALIASES:
        if core in direction:
            aliases.extend(words)
    if not aliases:
        return _dir_hit(unit.get("direction", ""), direction, unit.get("tags"))
    blob = unit.get("direction", "") + " " + " ".join(unit.get("tags") or [])
    return any(w in blob for w in aliases)


def _style_clean_body(text: str) -> str:
    """去掉"《章节》开头：书名"这类标题行与空行，只留正文。"""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("《") and "开头" in s:
            continue
        out.append(s)
    return "\n".join(out)


def style_samples(cfg: dict, direction: str = "", n: int = 2,
                  max_chars: int = 520) -> str:
    """风格范文：同题材真实原文片段（quote/lead），供写手直接模仿语感。

    与 recall 的"素材清单"不同——这里返回的是完整、自然、无套路的原文，
    让模型照其句长、节奏、用词方式写，而不是靠抽象禁令。
    过滤：记账道具 / 种地套路 / 数量表达密集（≥3 处）的片段；MMR 去重。
    """
    neg_terms = _negative_terms(cfg)
    pool = []
    for u in load_units(cfg):
        if u.get("kind") not in ("quote", "lead"):
            continue
        if not _direction_match(direction, u):
            continue
        body = _style_clean_body(u.get("text", ""))
        if not (40 <= len(body) <= 300):
            continue
        if _is_negative(body, neg_terms):
            continue
        if any(k in body for k in _STYLE_FARM):
            continue
        if len(_STYLE_NUM_EXPR.findall(body)) >= 3:
            continue
        if _STYLE_PLACEHOLDER.search(body):
            continue
        if _STYLE_ANALYSIS.search(body):
            continue
        if sum(1 for k in _STYLE_META if k in body) >= 1:
            continue
        if _STYLE_ROLEWORD.search(body):
            continue
        pool.append({**u, "_body": body})
    if not pool:
        return ""
    qw = {"high": 0, "mid": 1, "low": 2}
    # 排序：质量 → 长度适中优先（40-300 里 60-160 字最像"一段完整正文"，
    # 太长是章节摘录、太短是残句）→ 越短越靠前作末位兜底
    def _rank(u: dict):
        L = len(u.get("_body", ""))
        band = 0 if 60 <= L <= 160 else 1
        return (qw.get(u.get("quality"), 1), band, -L)

    pool.sort(key=_rank)

    sel, sel_bgs, total = [], [], 0
    for u in pool:
        bgs = frozenset(_bigrams(u.get("_body", "")))
        if bgs and any(len(bgs & s) > 0.45 * len(bgs | s) for s in sel_bgs):
            continue
        t = u.get("_body", "")
        if total + len(t) > max_chars and sel:
            continue
        sel.append(u)
        sel_bgs.append(bgs)
        total += len(t)
        if len(sel) >= n or total >= max_chars:
            break
    return "\n\n".join(u.get("_body", "").strip() for u in sel)
