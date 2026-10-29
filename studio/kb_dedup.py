# -*- coding: utf-8 -*-
"""知识库去重：压实现有重复 + 入库防重。

四类重复（2026-09-24 实测 10417 单元中的存量）：
  A. 跨 kind 完全同文      396 组 / 450 条冗余（指纹含 kind，跨 kind 漏网）
  B. lead 同开头（同文转载） 69 组 /  73 条冗余（标题不同、正文相同）
  C. 同 kind 同文          81 组 /  83 条冗余（指纹前的空白/全半角差异）
  D. id 冲突 2、坏行 1
另加语义近重复扫描（cos ≥ 阈值，抓同故事多站转载的变体标题版本）。

用法（CLI 已接 kb dedup）：
  python -m studio kb dedup            # 只扫描报告（dry-run）
  python -m studio kb dedup --apply    # 应用压实并重建向量/共现索引
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

_OLD_ID = "_old_id"


def _norm(t: str) -> str:
    return re.sub(r"[^一-龥a-z0-9]", "", (t or "").lower())


def _lead_open_key(u: dict) -> str:
    """lead 单元的正文开头指纹（剥掉标题行后取前 60 字）。"""
    body = u.get("text", "")
    parts = body.split("\n", 1)
    return _norm(parts[-1])[:60] if len(parts) > 1 else _norm(body)[:60]


# 保留优先级：弧位标注的深拆单元 > 素材库/技法 > 钩子金句 > lead 存档
_KIND_RANK = {"arc_segment": 0, "technique": 1, "material": 1, "hook": 2,
              "quote": 3, "lead": 8}


def _better(a: dict, b: dict) -> dict:
    """二选一保留：弧位标注优先 → kind 优先级 → 文本更长 → 旧 id 更小。"""
    ka = (bool(a.get("arc")), -_KIND_RANK.get(a.get("kind", ""), 5), len(a.get("text", "")))
    kb_ = (bool(b.get("arc")), -_KIND_RANK.get(b.get("kind", ""), 5), len(b.get("text", "")))
    if ka != kb_:
        return a if ka > kb_ else b
    return a if a.get(_OLD_ID, a.get("id", "")) <= b.get(_OLD_ID, b.get("id", "")) else b


def scan(cfg: dict, semantic: bool = True, threshold: float = 0.93) -> dict:
    """扫描重复。返回 {units, drops, reasons, stats}（不改库）。

    semantic=True 时用向量层做近重复扫描（需 Ollama，全库 CPU 约 20 分钟）。
    """
    import numpy as np

    from . import kb

    units = []
    for u in kb.load_units(cfg):
        if not u.get("id"):
            continue
        u[_OLD_ID] = u["id"]
        units.append(u)
    reasons: dict[str, str] = {}
    kept: dict[str, dict] = {u[_OLD_ID]: u for u in units}

    def drop(u: dict, why: str):
        oid = u[_OLD_ID]
        if oid not in reasons:
            reasons[oid] = why
            kept.pop(oid, None)

    # A/C. 归一化同文（跨 kind 与同 kind 一起抓）
    by_norm: dict[str, dict] = {}
    for u in units:
        key = _norm(u.get("text", ""))
        if len(key) < 12:
            continue
        prev = by_norm.get(key)
        if prev is None:
            by_norm[key] = u
        elif prev[_OLD_ID] in kept and u[_OLD_ID] in kept:
            winner = _better(prev, u)
            loser = u if winner is prev else prev
            by_norm[key] = winner
            drop(loser, "同文")
        # prev 已被其他规则淘汰时，当前 u 顶上
        elif prev[_OLD_ID] not in kept:
            by_norm[key] = u

    # B. lead 同开头（同文转载，标题不同正文同）
    open_seen: dict[str, dict] = {}
    for u in units:
        if u.get("kind") != "lead" or u[_OLD_ID] not in kept:
            continue
        key = _lead_open_key(u)
        if len(key) < 12:
            continue
        prev = open_seen.get(key)
        if prev is not None and prev[_OLD_ID] in kept:
            winner = _better(prev, u)
            drop(u if winner is prev else prev, "lead同开头")
            open_seen[key] = winner
        else:
            open_seen[key] = u

    # D. 语义近重复（余弦 ≥ threshold 的连通分量，组内保优）
    sem_groups = 0
    if semantic:
        texts = [kb.unit_text_for_embed(u) for u in units]
        embs, B = [], 64
        for i in range(0, len(texts), B):
            e = kb._embed_texts(texts[i:i + B])
            if e is None:
                print(f"[kb-dedup] embedding 中断于第 {i} 条，语义扫描跳过（其余规则已生效）")
                break
            embs.extend(e)
            if (i // B) % 20 == 0:
                print(f"[kb-dedup] 语义扫描 {min(i + B, len(texts))}/{len(texts)}…", flush=True)
        if len(embs) == len(units):
            mat = np.array(embs, dtype=np.float32)
            parent = list(range(len(units)))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            # 分块计算自相似(163k² 全矩阵需 100GB,分块峰值仅块大小)
            B = 4096
            N = len(units)
            for bs in range(0, N, B):
                be = min(bs + B, N)
                block = mat[bs:be] @ mat.T                      # (块高, N)
                for bi in range(be - bs):
                    i = bs + bi
                    row = block[bi]
                    row[i] = 0.0                                 # 去自身
                    js = np.argwhere(row >= threshold).flatten()
                    if len(js) > 2000:                            # 超大簇防卡死(全同文轰炸场景)
                        js = js[:2000]
                    for j in js:
                        j = int(j)
                        if j <= i:
                            continue
                        pj, pi = find(j), find(i)
                        if pj != pi:
                            parent[max(pi, pj)] = min(pi, pj)
                del block

            clusters: dict[int, list[int]] = defaultdict(list)
            for i in range(len(units)):
                clusters[find(i)].append(i)
            for members in clusters.values():
                alive = [units[i] for i in members if units[i][_OLD_ID] in kept]
                if len(alive) < 2:
                    continue
                sem_groups += 1
                winner = alive[0]
                for u in alive[1:]:
                    winner = _better(winner, u)
                for u in alive:
                    if u is not winner:
                        drop(u, f"语义近重复(≥{threshold})")

    drops = [u for u in units if u[_OLD_ID] in reasons]
    stats = {
        "units": len(units),
        "drop_total": len(drops),
        "semantic_groups": sem_groups,
        "by_reason": dict(Counter(reasons.values())),
    }
    return {"units": units, "drops": drops, "reasons": reasons, "stats": stats}


def apply(cfg: dict, scan_result: dict) -> dict:
    """按扫描结果压实 units.jsonl + 向量重映射 + 重建共现索引。

    幂等保障：apply 期间批量深拆必须暂停（CLI 侧检查 decon_state 新鲜度）。
    """
    import numpy as np

    from . import kb

    with kb._IO_LOCK:
        units = scan_result["units"]
        reasons = scan_result["reasons"]
        keep = [u for u in units if u[_OLD_ID] not in reasons]
        # 全量顺序重排 id，顺带消除历史 id 冲突
        for i, u in enumerate(keep, 1):
            u["id"] = f"U{i:06d}"
        tmp = kb.units_path(cfg).with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(u, ensure_ascii=False) for u in keep) + "\n", encoding="utf-8")
        os.replace(tmp, kb.units_path(cfg))
        # 向量按旧 id 首见对齐搬移；行数不足的交给 index_pending 增补
        try:
            old_mat = np.load(kb.vectors_path(cfg))
            old_rows = json.loads(kb.offsets_path(cfg).read_text(encoding="utf-8")).get("rows", [])
        except Exception:  # noqa: BLE001
            old_mat, old_rows = None, []
        id2row: dict[str, int] = {}
        for r, uid in enumerate(old_rows):
            id2row.setdefault(uid, r)
        rows, vecs = [], []
        for u in keep:
            r = id2row.get(u[_OLD_ID])
            if r is not None and old_mat is not None and r < old_mat.shape[0]:
                rows.append(u["id"])
                vecs.append(old_mat[r])
        if vecs:
            mat = np.vstack(vecs).astype(np.float32)
            kb._atomic_save_npy(kb.vectors_path(cfg), mat)
            kb.offsets_path(cfg).write_text(
                json.dumps({"rows": rows, "count": int(mat.shape[0]),
                            "lines": len(keep)}), encoding="utf-8")
        else:
            for f in (kb.vectors_path(cfg), kb.offsets_path(cfg)):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
        kb._rebuild_cocomatrix_locked(cfg)
        added = kb.index_pending(cfg)
        kb._FP_SEEN.clear()
    return {"kept": len(keep), "dropped": len(reasons), "reindexed": added}
