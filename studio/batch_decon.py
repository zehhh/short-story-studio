# -*- coding: utf-8 -*-
"""原文批量深拆：逐篇 LLM 拆解叙事弧 → 知识向量库（arc 标注单元）。

断点续跑：workspace/kb/decon_state.json 记录已完成篇目；失败篇记入 failed 可重试。
并发：--workers N（默认 2）。LLM 调用是 IO 密集，线程池即可；checkpoint 只在主线程
写；知识库写入由 kb._IO_LOCK 串行化。Ollama 端若 num_parallel=1 会自动排队，不会出错。
每轮结束后对失败篇自动补试一轮（本地引擎偶发 500/超时多为暂时性）。

两个入口，同一份实现：
  python -m studio.batch_decon [--config X] [--limit N] [--workers N]
  python -m studio kb batch-decon [--limit N] [--workers N]   ← 推荐，带 --config 等公共参数
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATE_FILE = "decon_state.json"
_TLS = threading.local()


def load_state(kbdir: Path) -> dict:
    f = kbdir / STATE_FILE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "failed": {}}


def _progress_path(kbdir: Path) -> Path:
    """进度纯文本落到 workspace/logs/。

    旧实现写死 `kbdir.parent.parent.parent / "workspace"`——kbdir 是
    workspace/kb 时恰好凑对，用户把 knowledge.dir 配到别处就静默写歪。
    改成从 kbdir 里找名为 workspace 的祖先，找不到就不写（进度只是给人看的）。
    """
    for p in (kbdir, *kbdir.parents):
        if p.name == "workspace":
            return p / "logs" / "decon_progress.txt"
    return kbdir / "decon_progress.txt"


def save_state(kbdir: Path, st: dict, total: int = 0):
    (kbdir / STATE_FILE).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    # 纯文本进度(看门狗/人工读,免 JSON 解析):done/总数|failed
    try:
        prog = _progress_path(kbdir)
        prog.parent.mkdir(parents=True, exist_ok=True)
        prog.write_text(f"{len(st.get('done', {}))}/{total}|{len(st.get('failed', {}))}",
                        encoding="utf-8")
    except Exception:
        pass


def _worker_llm(cfg: dict):
    """每线程独立 LLM 实例：requests.Session 非线程安全，绕开 llm 全局缓存。"""
    llm = getattr(_TLS, "llm", None)
    if llm is None:
        from .llm import MockLLM, _build_llm

        llm = _build_llm(dict(cfg["llm"]), f"batch-{threading.get_ident()}")
        if not isinstance(llm, MockLLM):
            llm.role = "planner"
        _TLS.llm = llm
    return llm


def _decon_one(cfg: dict, f: Path):
    """拆一篇：返回 (stem, 结果dict 或 异常)。"""
    from .deconstruct import deconstruct_file

    try:
        # per_call=6：段落合并成更大的单次调用，减少每篇 2-3 次的预填充开销
        # （6 段 × 2400 字 ≈ 9k token 输入，GLM 16k ctx 内安全）
        res = deconstruct_file(cfg, _worker_llm(cfg), f, max_sections=10, per_call=6,
                               deposit_material=False)
        if not res.get("sections"):
            # 全部段落解析失败（典型：LLM 500/限流）→ 记失败可重试
            return f.stem, RuntimeError("0 段（LLM 调用失败或输出无有效段落）")
        return f.stem, res
    except SystemExit as e:
        return f.stem, e
    except Exception as e:  # noqa: BLE001
        return f.stem, e


def _run_pass(cfg: dict, kbdir: Path, st: dict, todo: list, t0: float, label: str,
              total: int = 0):
    workers = int(cfg.get("_workers", 2))
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_decon_one, cfg, f) for f in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            stem, out = fut.result()
            if isinstance(out, Exception):
                st["failed"][stem] = f"{type(out).__name__}: {str(out)[:70]}"
                fail += 1
            else:
                st["done"][stem] = (out.get("title", "") or stem)[:40]
                st["failed"].pop(stem, None)
                ok += 1
            if i % 5 == 0 or i == len(todo):
                save_state(kbdir, st, total)
                rate = (time.time() - t0) / max(1, i)
                remain = (len(todo) - i) * rate / 3600
                print(f"[batch-decon] {label} {i}/{len(todo)}｜成功 {ok}｜失败 {fail}｜"
                      f"{rate:.0f}s/篇（{workers} 线程）｜剩余约 {remain:.1f} 小时", flush=True)
    save_state(kbdir, st, total)
    return ok, fail


def run(cfg: dict, limit: int = 0, workers: int = 2,
        dedup: bool = True, dedup_wait: int = 150) -> dict:
    """批量深拆主流程（CLI 与 `kb batch-decon` 共用的唯一实现）。

    dedup_wait：收尾语义去重前等待本地引擎卸载模型的秒数。默认 150 是为
    Ollama keep_alive 留的余量；通过 `kb batch-decon --dedup-wait 0` 可关掉等待
    （云 API 或 embedding 走远端时没有显存争用，白等 150 秒是纯浪费）。
    """
    from . import kb

    cfg = dict(cfg)
    cfg["_workers"] = max(1, int(workers or 1))
    kbdir = kb.kb_dir(cfg)
    raw = kbdir / "raw_text"
    files = sorted(raw.glob("*.txt"))
    total = len(files)
    st = load_state(kbdir)
    todo = [f for f in files if f.stem not in st["done"]]
    print(f"[batch-decon] 待拆 {len(todo)}/{total} 篇（已完成 {len(st['done'])}，"
          f"历史失败 {len(st['failed'])}）｜{cfg['_workers']} 线程", flush=True)
    if limit:
        todo = todo[:limit]
    ok, _ = _run_pass(cfg, kbdir, st, todo, time.time(), "主拆", total)
    # 末轮补试：本轮失败 + 历史失败篇再给一次机会
    retry = [f for f in files if f.stem in st["failed"]]
    if retry:
        print(f"[batch-decon] 末轮补试 {len(retry)} 篇失败篇…", flush=True)
        ok2, _ = _run_pass(cfg, kbdir, st, retry, time.time(), "补试", total)
        ok += ok2
    print(f"[batch-decon] 本轮完成：成功 {ok}｜仍失败 {len(st['failed'])}｜"
          f"累计已完成 {len(st['done'])} 篇", flush=True)

    summary = {"ok": ok, "total": total, "done": len(st["done"]),
               "failed": len(st["failed"]), "dedup": None}
    if not dedup:
        return summary
    # 收尾自动去重：知识库自维护，无需用户手动触发。
    # 等模型按 keep_alive 卸载后把 embedding 切到 GPU 全层（num_gpu 是层数，99=全部层）；
    # 若显存仍被占用导致 embedding 失败，scan 自动降级为只做同文/同开头压实。
    try:
        from . import kb_dedup

        print("[batch-decon] 收尾：自动全库去重（语义扫描）…", flush=True)
        if dedup_wait > 0:
            time.sleep(dedup_wait)
        kb.EMBED_NUM_GPU = 99
        res = kb_dedup.scan(cfg, semantic=True, threshold=0.93)
        st_ = res["stats"]
        print(f"[batch-decon] 去重扫描：{st_['units']} 单元，命中 {st_['drop_total']} 条冗余 "
              f"{json.dumps(st_['by_reason'], ensure_ascii=False)}", flush=True)
        if st_["drop_total"]:
            out_ = kb_dedup.apply(cfg, res)
            summary["dedup"] = out_
            print(f"[batch-decon] 压实完成：保留 {out_['kept']}｜剔除 {out_['dropped']}｜"
                  f"补编码 {out_['reindexed']}", flush=True)
        else:
            print("[batch-decon] 无冗余，无需压实", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[batch-decon] 收尾去重失败（不影响拆解成果）：{type(e).__name__}: {e}", flush=True)
    return summary


def main():
    argv = sys.argv[1:]
    cfg_path = None
    limit = 0
    workers = 2
    dedup = True
    dedup_wait = 150
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            cfg_path = argv[i + 1]
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
        elif a == "--workers" and i + 1 < len(argv):
            workers = max(1, int(argv[i + 1]))
        elif a == "--no-dedup":
            dedup = False
        elif a == "--dedup-wait" and i + 1 < len(argv):
            dedup_wait = max(0, int(argv[i + 1]))

    from .config import load_config

    cfg = load_config(cfg_path)
    run(cfg, limit=limit, workers=workers, dedup=dedup, dedup_wait=dedup_wait)


if __name__ == "__main__":
    main()
