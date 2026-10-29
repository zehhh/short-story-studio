#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补跑：完整生产测试中失败（字数不达平台下限）的题材，定向修复后重跑润色段。

用法：
    python tests/repolish_retry.py                 # 全部失败题材
    python tests/repolish_retry.py --only 彩礼纠纷  # 指定题材
    python tests/repolish_retry.py --list          # 只看失败清单

每个失败题材做三件事：
  1) 章节残留修复：对每章扫「禁用道具 + langqa 阻断项」，命中则用 rewrite 带清单再修
     （最多 3 轮），修不掉保留现稿并打印留痕（与门禁打满行为一致，不静默）
  2) stage_polish(force=True)：重跑 字数门 → 全稿体检 → 终审修复 → 读者团 → 装配，
     含新增的「装配前字数门复核」兜底（防终审/读者循环把稿子压回字数线下）
  3) 重建结果记录追加进 fullprod_results.jsonl（--summary 按题材取最后一条，自动覆盖旧记录）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "workspace_logs" / "fullprod_results.jsonl"


def _failed() -> list[dict]:
    if not RESULTS.exists():
        return []
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_dir: dict[str, dict] = {}
    for r in rows:
        by_dir[r["dir"]] = r          # 同题材取最后一条
    return [r for r in by_dir.values() if not r.get("ok") and r.get("project")]


def _repair_chapters(cfg, llm, project) -> int:
    """章节残留修复：返回修复掉的章数。"""
    from studio import agents
    from studio.langqa import format_banned, format_issues, scan, scan_banned
    from studio.llm import resolve_llm
    from studio.pipeline import _chapter_ctx

    writer = agents.WriterAgent(resolve_llm(cfg, llm, "writer"))
    state = project.meta.get("chapters_state") or {"summaries": {}, "tails": {}}
    fixed = 0
    n = 1
    while project.chapter_file(n).exists():
        f = project.chapter_file(n)
        txt = f.read_text(encoding="utf-8")
        brep = scan_banned(txt, cfg)
        lq = scan(txt)
        blocking = [h for h in lq["hits"] if h["severity"] in ("high", "mid")]
        if brep["pass"] and not blocking:
            n += 1
            continue
        print(f"    [repair] 小节{n:02d} 残留阻断项 {len(blocking) + len(brep['hits'])} 处，带清单重修 …")
        ctx = _chapter_ctx(cfg, project, n, state)
        ctx["_prev_text"] = txt
        issues = "；".join(filter(None, [format_banned(brep), format_issues(lq)]))
        best_txt, best_cost = txt, len(blocking) + len(brep["hits"])
        for rnd in range(1, 4):
            new_txt = writer.rewrite(ctx, issues + "\n只修以上语言/道具问题，情节骨架与字数不变。", 10 + rnd)
            nb = scan_banned(new_txt, cfg)
            nl = scan(new_txt)
            cost = len([h for h in nl["hits"] if h["severity"] in ("high", "mid")]) + len(nb["hits"])
            if cost < best_cost:
                best_txt, best_cost = new_txt, cost
                issues = "；".join(filter(None, [format_banned(nb), format_issues(nl)]))
                if cost == 0:
                    break
        if best_txt != txt:
            f.write_text(best_txt, encoding="utf-8")
            fixed += 1
            print(f"    [repair] 小节{n:02d} 已修复（残留 {best_cost} 处）")
        else:
            print(f"    [repair] ⚠ 小节{n:02d} 3 轮修不动，保留现稿留痕")
        n += 1
    return fixed


def _rebuild_rec(cfg, llm, project, direction: str, old: dict, t_all: float) -> dict:
    from studio.langqa import banned_terms
    from studio.langqa import scan as lang_scan
    from studio.util import count_words_zh

    rec: dict = {"dir": direction, "project": project.root.name, "ok": False}
    rep = project.load_json("report.json") or {}
    rec["final_words"] = rep.get("final_words", 0)
    rec["langqa_full"] = (rep.get("langqa_full") or {})
    revs = []
    for i in range(1, 30):
        r = project.load_json(f"ch{i:02d}.json", subdir="reviews")
        if not r:
            break
        revs.append(r)
    rec["chapters"] = len(revs)
    rec["gates"] = [
        {
            "no": r.get("no"),
            "gate": r.get("gate"),
            "langqa": r.get("langqa_pass"),
            "ban": "fail" if r.get("ban_hits") else "pass",
            "reader_avg": r.get("reader_avg"),
            "emotion": (r.get("emotion") or {}).get("emotion_score"),
        }
        for r in revs
    ]
    rec["title"] = (project.load_json("pitch.json") or {}).get("title", "")
    final = project.root / "final" / "成稿.md"
    rec["has_final"] = final.exists()
    if final.exists():
        lq = lang_scan(final.read_text(encoding="utf-8"))
        rec["final_langqa"] = {"pass": lq["pass"], "hits": len(lq["hits"]),
                               "fmt": len([h for h in lq["hits"] if h["kind"] == "格式"])}
        banned = banned_terms(cfg) + ["静了两秒", "闭不上眼", "别回头", "纸都黄了", "晚半个月"]
        txt = final.read_text(encoding="utf-8")
        rec["banned_hits"] = {b: txt.count(b) for b in banned if txt.count(b)}
    rec["ok"] = bool(final.exists() and rec["final_words"] >= int(cfg["review"].get("word_floor", 8000)))
    rec["note"] = "repolish 补跑（章节残留修复+润色重跑）"
    rec["secs"] = round(time.time() - t_all, 1)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    failed = _failed()
    if args.list:
        for r in failed:
            print(f"  {r['dir']:<10} {r.get('final_words')}字 项目={r.get('project')}")
        return 0
    if args.only:
        keep = {d.strip() for d in args.only.split(",") if d.strip()}
        failed = [r for r in failed if r["dir"] in keep]
    if not failed:
        print("没有需要补跑的失败题材")
        return 0

    from studio.config import load_config
    from studio.llm import get_llm
    from studio.pipeline import stage_polish
    from studio.store import Project

    cfg = load_config()
    llm = get_llm(cfg)
    workspace = Path(cfg["studio"]["workspace_abs"])
    projects_dir = workspace / "projects"
    for old in failed:
        d = old["dir"]
        root = projects_dir / old["project"]
        if not root.exists():
            root = workspace / old["project"]   # 兼容旧布局（workspace 直下）
        if not root.exists():
            print(f"[skip] {d}：项目目录不在 {root}")
            continue
        print(f"\n===== [补跑] {d} → {old['project']} =====", flush=True)
        project = Project(root)
        t_all = time.time()
        _repair_chapters(cfg, llm, project)
        stage_polish(cfg, llm, project, force=True)
        rec = _rebuild_rec(cfg, llm, project, d, old, t_all)
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{'✔' if rec['ok'] else '✘'} [{d}] {rec.get('final_words')}字 "
              f"违禁{rec.get('banned_hits') or '无'}", flush=True)
    print("=== 补跑结束 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
