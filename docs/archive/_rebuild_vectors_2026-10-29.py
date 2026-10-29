# -*- coding: utf-8 -*-
"""分片重建向量索引：硅基 Qwen3-Embedding-8B，每 10 批落盘一次，可中断续跑。"""
import sys, time, json
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"E:\小说\短篇")
import numpy as np
import studio.kb as kb
import studio.config as cfgmod

cfg = cfgmod.load_config()
VDIM = int(kb._emb_cfg(cfg).get("dim", 4096))
vpath = kb.vectors_path(cfg)
opath = kb.offsets_path(cfg)

# 续跑状态
done_rows = []
if vpath.exists() and opath.exists():
    try:
        off = json.loads(opath.read_text(encoding="utf-8"))
        done_rows = off.get("rows", [])
        print(f"续跑：已有 {len(done_rows)} 条向量", flush=True)
    except Exception:
        done_rows = []
done_set = set(done_rows)

units = kb.load_units(cfg)
pending = [u for u in units if u.get("id") and u["id"] not in done_set]
print(f"总数 {len(units)}，待编码 {len(pending)}，维度 {VDIM}", flush=True)

mat = np.load(vpath) if (vpath.exists() and done_rows) else np.zeros((0, VDIM), dtype=np.float32)
rows = list(done_rows)
BATCH = 32
t0 = time.time()
n_new = 0
for bi in range(0, len(pending), BATCH):
    batch = pending[bi:bi + BATCH]
    texts = [kb.unit_text_for_embed(u) for u in batch]
    for attempt in range(3):
        embs = kb._embed_texts(texts, cfg)
        if embs:
            break
        print(f"  batch@{bi} embed 失败，重试 {attempt+1}/3", flush=True)
        time.sleep(3)
    if not embs:
        print(f"batch@{bi} 三次失败，跳过本段", flush=True)
        continue
    arr = np.array(embs, dtype=np.float32)
    assert arr.shape[1] == VDIM, f"维度不符 {arr.shape} != {VDIM}"
    mat = np.vstack([mat, arr])
    rows.extend(u["id"] for u in batch)
    n_new += len(batch)
    if (bi // BATCH) % 10 == 0:
        kb._atomic_save_npy(vpath, mat)
        opath.write_text(json.dumps({"rows": rows, "count": len(rows),
                                     "lines": len(units)}), encoding="utf-8")
        el = time.time() - t0
        rate = n_new / el if el > 0 else 0
        eta = (len(pending) - n_new) / rate / 60 if rate > 0 else 0
        print(f"[{el/60:.1f}min] 已编码 {n_new}/{len(pending)} 速率 {rate:.1f}/s ETA {eta:.0f}min", flush=True)

kb._atomic_save_npy(vpath, mat)
opath.write_text(json.dumps({"rows": rows, "count": len(rows), "lines": len(units)}),
                 encoding="utf-8")
print(f"DONE 新增 {n_new} 条，总 {len(rows)}，耗时 {(time.time()-t0)/60:.1f}min", flush=True)
