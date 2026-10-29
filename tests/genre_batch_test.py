# -*- coding: utf-8 -*-
"""全题材真实模型批量测试：42 个题材方向逐個跑 提梗→选题→知识卡→书名方案→导语→大纲。

真实 API（config.yaml 当前配置，无 mock）：planner=claude-haiku-5-5，writer=mimo-v2.6-flash，
知识卡 embedding=Qwen3-Embedding-8B（硅基流动）。热点只抓一次，各题材项目复用。

用法：
    python tests/genre_batch_test.py --pilot            # 先跑 1 个题材验证脚本
    python tests/genre_batch_test.py --shard 0/3        # 并行分片（3 个进程）
    python tests/genre_batch_test.py --summary          # 汇总所有分片结果

断点续跑：结果按方向追加到 workspace_logs/genre_test_results.<shard>.jsonl，
已 ok 的方向自动跳过；中断后重跑同分片命令即可续。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "workspace_logs" / "genre_test_results"
TPL_HP: Path | None = None  # 全局热点模板文件路径


def _directions() -> list[dict]:
    d = json.loads((ROOT / "data" / "tropes.json").read_text(encoding="utf-8"))
    return d["directions"]


def _done(shard: str) -> set[str]:
    f = RESULTS / f"{shard}.jsonl"
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            if r.get("ok"):
                out.add(r["dir"])
        except Exception:
            pass
    return out


def _append(shard: str, rec: dict) -> None:
    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / f"{shard}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _ensure_hotspots_template(cfg: dict) -> Path:
    """全局抓一次热点（真实源），供全部题材项目复用。返回 hotspots.json 路径。"""
    from studio import pipeline
    from studio.store import create_project

    ws = Path(cfg["studio"]["workspace_abs"])
    tpl = ws / "_hotspots_tpl"
    tpl.mkdir(parents=True, exist_ok=True)
    project = create_project(tpl, "_tpl")
    hp = project.root / "hotspots.json"
    if not hp.exists():
        pipeline.stage_fetch(cfg, project)
    return hp


def run_one(cfg: dict, llm, d: dict, ws: Path) -> dict:
    from studio import pipeline
    from studio.store import create_project

    name, did = d["name"], d["id"]
    rec: dict = {"dir": name, "id": did, "ok": False, "stages": {}, "t0": time.strftime("%H:%M:%S")}
    t_all = time.time()
    project = create_project(ws, f"{name}_题材测试")
    # 复用全局热点模板
    if TPL_HP and TPL_HP.exists():
        shutil.copy(TPL_HP, project.root / "hotspots.json")

    def timed(stage: str):
        class _T:
            def __enter__(self):
                self.t = time.time()
                return self

            def __exit__(self, *a):
                rec["stages"][stage] = round(time.time() - self.t, 1)
        return _T()

    try:
        with timed("mine"):
            concepts = pipeline.stage_mine(cfg, llm, project, direction_hint=name)
        rec["concepts"] = len(concepts)
        with timed("select"):
            pipeline.stage_select(project, pick="auto", llm=llm, cfg=cfg)
        concept = project.load_json("concept.json") or {}
        rec["picked_dir"] = concept.get("direction", "")
        with timed("kb"):
            pipeline.stage_kb(cfg, llm, project)
        kb_info = project.load_json("kb.json") or {}
        rec["kb_fallback"] = bool(kb_info.get("fallback"))
        rec["kb_sources"] = len(kb_info.get("cases") or [])
        with timed("pitch"):
            pitch = pipeline.stage_pitch(cfg, llm, project)
        rec["title"] = (pitch or {}).get("title", "")
        rec["axes"] = (pitch or {}).get("axes", {})
        with timed("lead"):
            pipeline.stage_lead(cfg, llm, project)
        lead = project.load_json("lead.json") or {}
        rec["lead_words"] = lead.get("words", 0)
        rec["lead_gate"] = lead.get("gate")  # pass / maxed_stage
        with timed("outline"):
            pipeline.stage_outline(cfg, llm, project)
        outline = project.load_json("outline.json") or {}
        chs = outline.get("chapters") or []
        rec["chapters"] = len(chs)
        rec["outline_gate"] = outline.get("gate") or "pass"
        rec["ok"] = True
    except SystemExit as e:
        rec["error"] = f"SystemExit: {str(e)[:200]}"
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    rec["secs"] = round(time.time() - t_all, 1)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", help="k/n 分片（如 0/3）")
    ap.add_argument("--pilot", action="store_true", help="只跑第一个题材")
    ap.add_argument("--only", help="只跑指定题材（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="忽略断点，强制重跑（即使结果里已有 ok 记录）")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()

    if args.summary:
        rows = []
        for f in sorted(RESULTS.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        # 老记录缺 lead_gate 字段 → 从项目目录补读；同题材多条记录取最后一条
        by_dir: dict[str, dict] = {}
        for r in rows:
            by_dir[r["dir"]] = r
        rows = list(by_dir.values())
        for r in rows:
            if r.get("ok") and not r.get("lead_gate"):
                hits = list((ROOT / "workspace" / ".genre_test" / "projects").glob(f"*{r['dir']}_题材测试"))
                for h in hits:
                    lj = h / "lead.json"
                    if lj.exists():
                        r["lead_gate"] = (json.loads(lj.read_text(encoding="utf-8")) or {}).get("gate")
                        break
        ok = [r for r in rows if r.get("ok")]
        bad = [r for r in rows if not r.get("ok")]
        print(f"总 {len(rows)} | 跑通 {len(ok)} | 失败 {len(bad)}")
        for r in bad:
            print("  ✘", r["dir"], "|", r.get("error", "")[:100])
        maxed = [r for r in ok if r.get("lead_gate") == "maxed_stage"]
        for r in maxed:
            print(f"  ⚠ {r['dir']} 导语门打满放行（{r.get('lead_words')}字）")
        for r in sorted(ok, key=lambda x: x.get("secs", 0)):
            print(f"  ✔ {r['dir']:<8} {r.get('secs',0):>6}s 梗卡{r.get('concepts')} "
                  f"导语{r.get('lead_words')}字({r.get('lead_gate')}) "
                  f"{r.get('chapters')}节 {r.get('title','')[:18]} [{r.get('picked_dir')}]")
        return 0

    from studio.config import load_config
    from studio.llm import get_llm

    cfg = load_config()
    ws = ROOT / "workspace" / ".genre_test"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "projects").mkdir(exist_ok=True)
    cfg["studio"]["workspace_abs"] = str(ws)

    dirs = _directions()
    if args.pilot:
        dirs = dirs[:1]
    elif args.only:
        want = {x.strip() for x in args.only.split(",")}
        dirs = [d for d in dirs if d["name"] in want or d["id"] in want]
    elif args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        dirs = [d for i, d in enumerate(dirs) if i % n == k]
    if args.limit:
        dirs = dirs[: args.limit]

    shard = args.shard.replace("/", "_") if args.shard else "pilot"
    done = set() if args.force else _done(shard)
    todo = [d for d in dirs if d["name"] not in done]
    print(f"=== 全题材真实模型测试 | 分片 {shard} | 待跑 {len(todo)} 个方向（已完成 {len(done)} 跳过）===")

    global TPL_HP
    TPL_HP = _ensure_hotspots_template(cfg)
    llm = get_llm(cfg)

    for i, d in enumerate(todo, 1):
        print(f"\n----- [{i}/{len(todo)}] {d['name']} -----", flush=True)
        rec = run_one(cfg, llm, d, ws)
        _append(shard, rec)
        flag = "✔" if rec["ok"] else "✘"
        print(f"  {flag} {d['name']} {rec.get('secs')}s "
              f"梗卡{rec.get('concepts')} 导语{rec.get('lead_words')}字 "
              f"{rec.get('chapters')}节 | {rec.get('error', '')}", flush=True)

    print(f"\n=== 分片 {shard} 完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
