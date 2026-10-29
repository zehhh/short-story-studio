# -*- coding: utf-8 -*-
"""全题材真实模型·完整生产测试（agent 模式全门禁链）。

对指定题材方向跑完整生产线：选题→知识卡→书名方案→导语→大纲→故事圣经→
逐章写作（langqa/主编/读者团 qwen3.8-flash/情绪量化四道门）→整稿润色终审。
产物落正式工作台 workspace/projects/（字数不足 8000 的会被 word gate 淘汰到 rejected/）。

用法：
    python tests/fullprod_batch_test.py --list                 # 看候选题材与 idea
    python tests/fullprod_batch_test.py --only 追妻火葬场,彩礼纠纷
    python tests/fullprod_batch_test.py --all                  # 跑全部候选
    python tests/fullprod_batch_test.py --summary
断点：结果写 workspace_logs/fullprod_results.jsonl，已 ok 的方向自动跳过。
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
IDEAS = ROOT / "workspace_logs" / "fullprod_ideas.json"


def _load_ideas() -> dict:
    if not IDEAS.exists():
        print(f"缺少 idea 文件 {IDEAS}（先从全题材批处理提取 core_trope）")
        raise SystemExit(1)
    return json.loads(IDEAS.read_text(encoding="utf-8"))


def _done() -> set[str]:
    if not RESULTS.exists():
        return set()
    out = set()
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            if r.get("ok"):
                out.add(r["dir"])
        except Exception:
            pass
    return out


def _append(rec: dict) -> None:
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_one(cfg: dict, llm, direction: str, idea: str) -> dict:
    from studio import pipeline
    from studio.store import create_project

    rec: dict = {"dir": direction, "idea": idea[:60], "ok": False, "stages": {}}
    t_all = time.time()
    ws = Path(cfg["studio"]["workspace_abs"])
    project = create_project(ws, idea[:40])
    rec["project"] = project.root.name
    print(f"\n===== [{direction}] {idea[:50]} → {project.root.name} =====", flush=True)

    def timed(stage: str):
        class _T:
            def __enter__(self):
                self.t = time.time()
                return self

            def __exit__(self, *a):
                rec["stages"][stage] = round(time.time() - self.t, 1)
        return _T()

    try:
        concept = pipeline.make_idea_concept(idea, direction)
        project.save_json("concept.json", concept)
        project.set_stage("concept", concept.get("direction", ""))
        with timed("kb"):
            pipeline.stage_kb(cfg, llm, project)
        with timed("pitch"):
            pipeline.stage_pitch(cfg, llm, project)
        with timed("lead"):
            pipeline.stage_lead(cfg, llm, project)
        with timed("outline"):
            pipeline.stage_outline(cfg, llm, project)
        with timed("bible"):
            pipeline.stage_bible(cfg, llm, project)
        with timed("write"):
            pipeline.stage_write(cfg, llm, project)
        with timed("polish"):
            pipeline.stage_polish(cfg, llm, project)
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
        # 违禁道具/格式终检（确定性，零成本）
        if final.exists():
            from studio.langqa import banned_terms
            from studio.langqa import scan as lang_scan

            lq = lang_scan(final.read_text(encoding="utf-8"))
            rec["final_langqa"] = {"pass": lq["pass"], "hits": len(lq["hits"]),
                                   "fmt": len([h for h in lq["hits"] if h["kind"] == "格式"])}
            banned = banned_terms(cfg) + ["静了两秒", "闭不上眼", "别回头", "纸都黄了", "晚半个月"]
            txt = final.read_text(encoding="utf-8")
            rec["banned_hits"] = {b: txt.count(b) for b in banned if txt.count(b)}
        rec["ok"] = bool(final.exists() and rec["final_words"] >= int(cfg["review"].get("word_floor", 8000)))
    except SystemExit as e:
        rec["error"] = f"SystemExit: {str(e)[:200]}"
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    rec["secs"] = round(time.time() - t_all, 1)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="题材，逗号分隔")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    ideas = _load_ideas()

    if args.list:
        for k, v in ideas.items():
            print(f"- {k}: {v[:60]}")
        return 0
    if args.summary:
        if not RESULTS.exists():
            print("暂无结果")
            return 0
        rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
        by_dir = {}
        for r in rows:
            by_dir[r["dir"]] = r
        rows = list(by_dir.values())
        print(f"总计 {len(rows)} | 成稿 {sum(1 for r in rows if r.get('ok'))} | 失败 {sum(1 for r in rows if not r.get('ok'))}")
        for r in rows:
            if r.get("ok"):
                ga = [g["gate"] for g in r.get("gates", [])]
                ra = [g["reader_avg"] for g in r.get("gates", []) if isinstance(g.get("reader_avg"), (int, float))]
                lq = [g.get("langqa") for g in r.get("gates", [])]
                ban = [g.get("ban") for g in r.get("gates", [])]
                print(f"  ✔ {r['dir']:<7} {r.get('final_words')}字 {r.get('chapters')}章 "
                      f"读者均分{sum(ra)/len(ra):.1f} 门禁{set(ga)} "
                      f"langqa {sum(1 for x in lq if x)}/{len(lq)} 禁道具 {sum(1 for x in ban if x == 'fail')}/{len(ban)} "
                      f"终检{r.get('final_langqa',{}).get('pass')} 违禁{r.get('banned_hits') or '无'} "
                      f"| {r.get('title','')[:20]} [{r.get('secs')}s]")
            else:
                print(f"  ✘ {r['dir']} | {r.get('error','')[:90]}")
        return 0

    from studio.config import load_config
    from studio.llm import get_llm

    cfg = load_config()
    llm = get_llm(cfg)
    dirs = list(ideas) if args.all else [d.strip() for d in (args.only or "").split(",") if d.strip()]
    done = set() if args.force else _done()
    todo = [d for d in dirs if d in ideas and d not in done]
    print(f"=== 完整生产测试 | 待跑 {len(todo)} 个题材：{'、'.join(todo)} ===")

    for d in todo:
        rec = run_one(cfg, llm, d, ideas[d])
        _append(rec)
        flag = "✔" if rec["ok"] else "✘"
        print(f"{flag} [{d}] {rec.get('final_words')}字 读者均分"
              f"{(sum(g['reader_avg'] for g in rec.get('gates',[]) if isinstance(g.get('reader_avg'),(int,float)))/max(1,len([g for g in rec.get('gates',[]) if isinstance(g.get('reader_avg'),(int,float))]))):.1f}"
              f" 违禁{rec.get('banned_hits') or '无'} | {rec.get('error','')}", flush=True)
    print("=== 完整生产测试批处理结束 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
