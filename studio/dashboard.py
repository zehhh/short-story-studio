# -*- coding: utf-8 -*-
"""可视化看板：实时本地服务 / 静态 HTML 导出。

- 纯标准库实现（ThreadingHTTPServer），图表为纯 CSS（conic-gradient 饼图 + 条形图），
  不依赖任何外部 JS 库，离线可用。
- 数据源：workspace/projects/*（project/pitch/concept/outline/reviews/report）、
  workspace/hotspots/、data/knowledge/。
- 用法：
    python -m studio dashboard                 # 起实时看板 http://127.0.0.1:8600
    python -m studio dashboard --port 9000
    python -m studio dashboard --export        # 导出自包含静态 HTML 到 workspace/
    python -m studio dashboard --export report.html
"""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ROOT
from .store import Project
from .util import count_words_zh, now_ts
from . import deconstruct as dec
from . import material as mat_mod
from . import review as review_mod
from . import runlog
from . import rag as rag_mod
from . import workbench as wb

DECON_STATE: dict = {"running": False, "done": 0, "total": 0, "current": "", "error": ""}

# 知识库去重任务状态（后台线程执行，扫描+压实全程数秒到 20+ 分钟）
DEDUP_STATE: dict = {"running": False, "result": None, "error": ""}
# 正在运行的生成任务 id 集合：删除创作记录时禁止误删进行中的任务
GEN_RUNNING: set = set()


def _workspace_inbox(cfg: dict) -> Path:
    d = Path(cfg["studio"]["workspace_abs"]) / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decon_progress(cfg: dict) -> dict:
    """批量深拆进度：读 decon_state.json（checkpoint）+ raw_text 篇数。"""
    import time as _t

    kbdir = Path(cfg["studio"]["workspace_abs"]) / "kb"
    state_f = kbdir / "decon_state.json"
    raw = kbdir / "raw_text"
    try:
        total = len(list(raw.glob("*.txt"))) if raw.exists() else 0
    except Exception:  # noqa: BLE001
        total = 0
    done = failed = 0
    running = False
    if state_f.exists():
        try:
            s = json.loads(state_f.read_text(encoding="utf-8"))
            done, failed = len(s.get("done", {})), len(s.get("failed", {}))
            running = (_t.time() - state_f.stat().st_mtime) < 300
        except Exception:  # noqa: BLE001
            pass
    return {"total": total, "done": done, "failed": failed, "running": running}


_LAYA_STATUS_CACHE: dict = {"at": 0.0, "data": None}


def _laya_status(cfg: dict) -> dict:
    """判别器状态（30s 缓存 + 600ms 探测超时）。

    本机安全软件对回环连接的失败探测要 ~2s（WinError 10061 也要等），
    服务离线时绝不能每个页面请求都同步付这笔钱。
    """
    import time as _time
    import urllib.request as _ur

    if _LAYA_STATUS_CACHE["data"] is not None and _time.time() - _LAYA_STATUS_CACHE["at"] < 30:
        return _LAYA_STATUS_CACHE["data"]
    lc = (cfg.get("laya") or {})
    backend = lc.get("backend", "laya")
    if backend == "self":
        st = {}
        try:
            dec = Path(cfg["studio"]["workspace_abs"]) / "kb" / "dup_decisions.jsonl"
            if dec.exists():
                dup = new = 0
                for l in dec.read_text(encoding="utf-8").splitlines()[-2000:]:
                    try:
                        r = json.loads(l)
                        dup += r.get("verdict") == "dup"
                        new += r.get("verdict") == "new"
                    except json.JSONDecodeError:
                        continue
                st = {"cands": dup + new, "dup": dup, "new": new,
                      "llm_saved": dup + new}
        except Exception:  # noqa: BLE001
            pass
        out = None
        try:
            _ur.urlopen(f"{lc.get('self_url', 'http://127.0.0.1:8152')}/health", timeout=0.6)
            out = {"enabled": True, "ready": True, "backend": "self",
                   "checkpoint": "cn_v4(自训)", "device": "GPU", "stats": st}
        except Exception:  # noqa: BLE001
            out = {"enabled": True, "ready": False, "backend": "self",
                   "checkpoint": "cn_v4(自训)", "device": "GPU", "stats": st}
        _LAYA_STATUS_CACHE.update({"at": _time.time(), "data": out})
        return out
    try:
        from . import laya_judge

        ready = laya_judge.available()
        st = laya_judge.stats()
    except Exception:  # noqa: BLE001
        ready, st = False, {}
    return {"enabled": bool(lc.get("enabled", True)), "ready": ready, "backend": "laya",
            "checkpoint": lc.get("checkpoint", "multilingual"),
            "device": lc.get("device", "cpu"), "stats": st}


def _laya_preload(cfg: dict):
    """看板启动即拉起 Laya 判别子进程（模型在子进程主线程自行加载，秒回不阻塞）。"""
    lc = (cfg.get("laya") or {})
    if not lc.get("enabled", True):
        return
    try:
        from . import laya_judge

        laya_judge.prelaunch()
    except Exception as e:  # noqa: BLE001
        print(f"[dashboard] laya 预热失败（不影响生成，届时回退主模型）：{e}")


def _dedup_worker(cfg: dict, semantic: bool, threshold: float):
    from . import kb, kb_dedup

    try:
        res = kb_dedup.scan(cfg, semantic=semantic, threshold=threshold)
        kb_dedup.scan_keep_old_ids(res)
        out = kb_dedup.apply(cfg, res)
        DEDUP_STATE["result"] = {**res["stats"], **out, "at": now_ts()}
        DEDUP_STATE["error"] = ""
    except Exception as e:  # noqa: BLE001
        DEDUP_STATE["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        DEDUP_STATE["running"] = False


FETCH_STATE: dict = {"running": False, "result": None, "error": ""}


def _novel_fetch_worker(cfg: dict, source: str, top: int):
    """热门小说抓取→深拆入库（后台线程；结果落在 FETCH_STATE）。"""
    try:
        from . import fetch_novel as fn

        llm = resolve_llm_for_dashboard(cfg)
        res = fn.fetch_and_decon(cfg, llm, source_name=source, top=top)
        FETCH_STATE["result"] = {**res, "at": now_ts()}
        FETCH_STATE["error"] = "" if res.get("ok") else (res.get("error") or "未抓到可用书目")
    except Exception as e:  # noqa: BLE001
        FETCH_STATE["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        FETCH_STATE["running"] = False


def resolve_llm_for_dashboard(cfg: dict):
    from .llm import get_llm, resolve_llm

    return resolve_llm(cfg, get_llm(cfg), "planner")

CFG_FILE: str | None = None  # 运行时由 serve() 设置，供控制台写回配置

STAGE_KEYS = ["concept", "pitch", "lead", "outline", "bible", "chapters", "final"]
STAGE_NAMES = ["梗卡", "方案", "大纲", "圣经", "章节", "成稿"]


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------

def _safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _uncovered_criteria(rub: dict | None) -> list[str]:
    """评审卡里没评估到的检查项名称（看板覆盖率列的 tooltip）。

    旧实现读 `rev["uncovered"]`——这个字段从未被任何写入方产生过（评审卡只在
    rubric.criteria 里用 status 表达"未评估"），所以 tooltip 永远是空的：
    用户看到"覆盖 60%"却点不出缺了什么。这里从 criteria 现算，读写两端对齐。
    """
    if not isinstance(rub, dict):
        return []
    return [str(c.get("name") or c.get("id") or "") for c in (rub.get("criteria") or [])
            if c.get("status") != "evaluated" and c.get("name")]


def _project_brief(root: Path) -> dict | None:
    pj = Project(root)
    meta = pj.meta or {}
    pitch = pj.load_json("pitch.json") or {}
    concept = pj.load_json("concept.json") or {}
    report = pj.load_json("report.json") or {}
    status = meta.get("status", {})
    stage_index = sum(1 for k in STAGE_KEYS if k in status)
    reviews = meta.get("reviews", {})
    gates = [v for v in reviews.values() if isinstance(v, dict) and "gate" in v]
    # 只有 pass 才算通过；hybrid/skipped_hybrid 是"跳过逐章门禁"，不能算 pass
    # （旧实现把 hybrid 计入通过率，看板显示"门禁全过"其实一章都没审）。
    gates_pass = sum(1 for v in gates if v.get("gate") == "pass")
    gates_skipped = sum(1 for v in gates if str(v.get("gate", "")).startswith(("hybrid", "skipped")))
    # 降级门（LLM 输出不可解析 → degraded_review / degraded_emotion）既不算 pass
    # 也不算 skipped：它代表"这门压根没审过"。单列出来，避免看板把降级稿
    # 混进"通过率"里显示成 0 通过却看不出是模型故障还是稿子差。
    gates_degraded = sum(1 for v in gates if str(v.get("gate", "")).startswith("degraded"))
    full = (report.get("review") or {}).get("full_story") or {}
    reader_avgs = [v.get("reader_avg") for v in gates if v.get("reader_avg") is not None]
    pub = _safe_json(root / "publish.json") or {}
    reads = finish = None
    adir = root.parent.parent / "analytics" / root.name
    if adir.exists():
        snaps = sorted(adir.glob("*.json"))
        if snaps:
            m = (_safe_json(snaps[-1]) or {}).get("metrics", {})
            reads = m.get("reads")
            finish = m.get("finish_rate")
    return {
        "published": pub.get("status") in ("published", "pending_manual"),
        "publish_mode": pub.get("mode", ""),
        # 平台身份：publish.json 有记录就以它为准，否则回退 pitch.platform。
        # 看板此前完全不显示平台，投知乎和投番茄的稿子在列表里长得一样，
        # 排查"投错平台"时无从下手。
        "platform": pub.get("platform") or ((_safe_json(root / "pitch.json") or {}).get("platform") or ""),
        "reads": reads,
        "finish_rate": finish,
        "name": root.name,
        "created_at": meta.get("created_at", ""),
        "title": pitch.get("title", "（未定名）"),
        "logline": pitch.get("logline", ""),
        "direction": concept.get("direction", ""),
        "platform": pitch.get("platform", ""),
        "stage_index": stage_index,
        "stages": {k: status.get(k, "") for k in STAGE_KEYS},
        "chapters_done": len(list((root / "chapters").glob("ch*.md"))) if (root / "chapters").exists() else 0,
        "final_words": report.get("final_words", 0),
        "platform_pass": (report.get("platform_check") or {}).get("pass"),
        "score": report.get("score"),
        # 成稿体检三元组：以前只有 pipeline 写、没有任何消费方，等于埋进 json 里没人看。
        # 看板直接透出，才能在列表页一眼发现"带禁用道具 / 字数被清理吃掉 / 复查没清干净"的稿子。
        "banned_props": report.get("banned_props_final") or [],
        "langqa_full_pass": (report.get("langqa_full") or {}).get("pass"),
        "langqa_full_hits": (report.get("langqa_full") or {}).get("hits"),
        "recheck_pass": (report.get("deterministic_recheck_final") or {}).get("pass"),
        # 伏笔账本（借鉴 openwrite B08）：bible.foreshadows 以前只进提示词、从不校验，
        # 写完没有任何地方能看出"伏笔收没收"。这里透出超期/无回收痕迹计数，
        # 否则 report.foreshadow_audit 又是一个只写不读的孤立字段。
        "foreshadow_ok": (report.get("foreshadow_audit") or {}).get("ok"),
        "foreshadow_total": (report.get("foreshadow_audit") or {}).get("total"),
        "foreshadow_overdue": len((report.get("foreshadow_audit") or {}).get("overdue") or []),
        "foreshadow_missing": len((report.get("foreshadow_audit") or {}).get("missing_trace") or []),
        # 字数门保稿标记（借鉴 openwrite B06）：越界不再淘汰，改为留在工作台打标，
        # 看板必须能筛出来，否则"保稿"等于"藏起来"。
        "gate_status": meta.get("gate_status", ""),
        "gate_reason": meta.get("gate_reason", ""),
        "length_out_of_range": bool((report.get("word_gate") or {}).get("length_out_of_range")),
        "word_flow": report.get("word_flow") or {},
        # 语言打磨（2026-10-26 用户实测反馈驱动）：改了几节、回滚几节、缺陷分降了多少。
        # 不落这块，用户永远只能靠"自己读一遍"判断语言病治没治——
        # 而 report.langqa_full 只说明"扫出几处"，不说明"改掉几处"。
        "lang_before": (report.get("lang_polish") or {}).get("before"),
        "lang_after": (report.get("lang_polish") or {}).get("after"),
        "lang_rewritten": (report.get("lang_polish") or {}).get("sections_rewritten"),
        "lang_rejected": (report.get("lang_polish") or {}).get("sections_rejected"),
        "lang_deduped": (report.get("lang_polish") or {}).get("deduped_paras"),
        # 投稿线判定（四类语言病的"达标/未达标"结论）。之前只有 lang_polish 的
        # before/after 计数，看板看不出"这篇到底能不能投"——必须透出 pass 与失败项。
        "market_lang_pass": (report.get("market_lang") or {}).get("pass"),
        "market_lang_fails": "；".join((report.get("market_lang") or {}).get("fails") or []),
        # 投稿判定（studio/verdict.py 单一权威源）：把字数、语言、主编分合成
        # 一个 {ready, grade, reasons}。看板此前只能分别看 platform_check 与
        # market_lang，"能不能投"要人自己对——三处口径打架时尤其误导。
        "submission_grade": (report.get("submission") or {}).get("grade"),
        "submission_ready": (report.get("submission") or {}).get("ready"),
        "submission_reasons": "；".join((report.get("submission") or {}).get("reasons") or []),
        # 书名卖点分（studio/title_gate.py）：番茄签约被拒的第一大显性原因是书名
        # 没卖点，不是正文差。看板不显示它，复盘会一直误以为问题都在正文。
        "title": report.get("title") or "",
        "title_score": report.get("title_score"),
        "title_family": report.get("title_family") or "",
        # 复检结论（`tools/manuscript_audit.py --write` 写入）：判据修好后老报告
        # 的 market_lang 会永远停在旧结论上，看板必须能看出"复检已达标"。
        "lang_recheck": report.get("lang_recheck") or {},
        "lang_repair": report.get("lang_repair") or {},
        "lang_repair_final": report.get("lang_repair_final") or {},
        "lang_balance": report.get("lang_balance") or {},
        "banned_exempt": "、".join(report.get("banned_exempt") or []),
        "reader_avg": full.get("reader_avg"),
        "reader_avg_chapters": round(sum(reader_avgs) / len(reader_avgs), 2) if reader_avgs else None,
        "gates_pass": gates_pass,
        "gates_skipped": gates_skipped,
        "gates_degraded": gates_degraded,
        "gates_total": len(gates),
        "review_enabled": bool(gates) or bool(full),
    }


def collect_overview(cfg: dict) -> dict:
    ws = Path(cfg["studio"]["workspace_abs"])
    projects_dir = ws / "projects"
    briefs = []
    if projects_dir.exists():
        for d in sorted(projects_dir.iterdir()):
            if d.is_dir():
                b = _project_brief(d)
                if b:
                    briefs.append(b)

    # KPI
    total_words = sum(b["final_words"] or 0 for b in briefs)
    finished = sum(1 for b in briefs if b["stage_index"] >= 6)
    readers = [b["reader_avg"] for b in briefs if b["reader_avg"] is not None]
    gates_p = sum(b["gates_pass"] for b in briefs)
    gates_t = sum(b["gates_total"] for b in briefs)
    kpis = {
        "projects": len(briefs),
        "finished": finished,
        "total_words": total_words,
        "avg_reader": round(sum(readers) / len(readers), 2) if readers else None,
        "gates_pass_rate": round(gates_p / gates_t * 100, 1) if gates_t else None,
        "kb_cards": len(list((ROOT / "data" / "knowledge").glob("*.json")))
        if (ROOT / "data" / "knowledge").exists()
        else 0,
        "tokens_out": 0,
        "published": sum(1 for b in briefs if b.get("published")),
        "total_reads": sum(b.get("reads") or 0 for b in briefs),
    }

    # 用量聚合
    usage_agg: dict[str, dict] = {}
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            rep = _safe_json(d / "report.json") or {}
            for k, v in (rep.get("usage") or {}).items():
                agg = usage_agg.setdefault(
                    k, {"role": v.get("role", ""), "model": v.get("model", ""), "calls": 0,
                        "prompt_tokens": 0, "completion_tokens": 0}
                )
                agg["calls"] += v.get("calls", 0)
                agg["prompt_tokens"] += v.get("prompt_tokens", 0)
                agg["completion_tokens"] += v.get("completion_tokens", 0)
    kpis["tokens_out"] = sum(v["completion_tokens"] for v in usage_agg.values())

    # 题材方向与知识卡覆盖
    tropes = _safe_json(ROOT / "data" / "tropes.json") or {"directions": []}
    kb_dir = ROOT / "data" / "knowledge"
    cards = {}
    if kb_dir.exists():
        for f in kb_dir.glob("*.json"):
            c = _safe_json(f) or {}
            cards[c.get("id", f.stem)] = {
                "facts": len(c.get("facts", [])),
                "fallback": c.get("fallback", False),
                "collected_at": c.get("collected_at", ""),
            }
    dir_count: dict[str, int] = {}
    for b in briefs:
        if b["direction"]:
            dir_count[b["direction"]] = dir_count.get(b["direction"], 0) + 1
    directions = []
    for d in tropes.get("directions", []):
        card = cards.get(d["id"])
        directions.append(
            {
                "id": d["id"],
                "name": d["name"],
                "heat": d.get("heat", 0),
                "emotion": d.get("emotion", ""),
                "projects": dir_count.get(d["name"], 0),
                "has_card": card is not None,
                "card_facts": (card or {}).get("facts", 0),
            }
        )

    # 热点快照（最近一份）
    hot_items, hot_file, hot_platforms = [], "", {}
    hot_dir = ws / "hotspots"
    if hot_dir.exists():
        files = sorted(hot_dir.glob("hot_*.json"))
        if files:
            hot_file = files[-1].name
            items = _safe_json(files[-1]) or []
            for it in items:
                hot_platforms[it.get("source", "?")] = hot_platforms.get(it.get("source", "?"), 0) + 1
            hot_items = items[:50]

    return {
        "generated_at": now_ts(),
        "kpis": kpis,
        "projects": briefs,
        "directions": directions,
        "usage": usage_agg,
        "hotspots": {"file": hot_file, "count": len(hot_items), "items": hot_items, "platforms": hot_platforms},
    }


def collect_project_detail(cfg: dict, name: str) -> dict | None:
    """按项目名采集详情；name 必须与目录名精确匹配（防路径穿越）。"""
    projects_dir = Path(cfg["studio"]["workspace_abs"]) / "projects"
    root = None
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            if d.is_dir() and d.name == name:
                root = d
                break
    if root is None:
        return None
    pj = Project(root)
    meta = pj.meta or {}
    pitch = pj.load_json("pitch.json") or {}
    outline = pj.load_json("outline.json") or {}
    concept = pj.load_json("concept.json") or {}
    report = pj.load_json("report.json") or {}
    kb = pj.load_json("kb.json") or {}
    ch_titles = {c.get("no"): c.get("title", "") for c in outline.get("chapters", [])}
    chapters = []
    for ch in pj.read_chapters():
        rev = pj.load_json(f"ch{ch['no']:02d}.json", subdir="reviews") or {}
        # 评审卡字段在 reviews/chNN.json 里是**嵌套**的（rev["rubric"]["quality_score"]），
        # 只有 project.json.reviews 的摘要才是扁平字段。旧实现直接读 rev["quality_score"]，
        # 于是看板的质量分/覆盖率/交付三列永远是「—」（实测 48/48 章全空）——
        # "证据化评审"只存在于 CLI，看板看不见，B07 在展示层整条断开。
        # 这里统一走 review.summary_fields()：同一份口径，避免再次手抄字段名。
        rub = rev.get("rubric") if isinstance(rev.get("rubric"), dict) else None
        brief = review_mod.summary_fields(rub) if rub else {}
        chapters.append(
            {
                "no": ch["no"],
                "title": ch_titles.get(ch["no"], ""),
                "words": count_words_zh(ch["text"]),
                "review_rounds": rev.get("review_rounds"),
                "review_score": rev.get("review_score"),
                "quality": brief.get("quality", rev.get("quality_score")),
                "coverage": brief.get("coverage", rev.get("coverage")),
                "delivery": brief.get("delivery", rev.get("delivery")),
                "uncovered": _uncovered_criteria(rub),
                "gate": rev.get("gate"),
                "reader_rounds": rev.get("reader_rounds"),
                "reader_avg": rev.get("reader_avg"),
                "reader_scores": rev.get("reader_scores", {}),
            }
        )
    return {
        "name": root.name,
        "meta": {"created_at": meta.get("created_at", ""), "status": meta.get("status", {})},
        "pitch": pitch,
        "concept": concept,
        "kb": {"direction": kb.get("direction", ""), "fallback": kb.get("fallback"), "digest": (kb.get("digest") or "")[:600]},
        "chapters": chapters,
        "report": report,
    }


# ---------------------------------------------------------------------------
# 静态导出 / HTTP 服务
# ---------------------------------------------------------------------------

def export_dashboard(cfg: dict, out_path: str) -> Path:
    """导出自包含静态 HTML（数据内嵌，离线可看）。"""
    overview = collect_overview(cfg)
    details = {}
    for b in overview["projects"]:
        d = collect_project_detail(cfg, b["name"])
        if d:
            details[b["name"]] = d
    payload = {"overview": overview, "details": details}
    html = _render(payload)
    p = Path(out_path)
    if not p.is_absolute():
        # 相对路径按当前工作目录解析（直觉行为），绝对路径原样
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p


class _Handler(BaseHTTPRequestHandler):
    cfg: dict = {}

    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        try:
            if u.path == "/api/overview":
                body = json.dumps(collect_overview(self.cfg), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/project":
                name = (parse_qs(u.query).get("name") or [""])[0]
                detail = collect_project_detail(self.cfg, name)
                if detail is None:
                    self._send(404, b'{"error":"project not found"}', "application/json; charset=utf-8")
                else:
                    body = json.dumps(detail, ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/generate":
                from . import scheduler as sch

                # 创作记录 = 手动触发的生成任务；定时任务在「定时任务」页管理
                jobs = [dict(j, running=j.get("id") in GEN_RUNNING)
                        for j in sch.load_schedules(self.cfg)
                        if (j.get("trigger") or {}).get("type") == "manual"]
                body = json.dumps({"jobs": jobs}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/runlogs":
                runs = runlog.list_runs(self.cfg)
                self._send(200, json.dumps({"runs": runs}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/runlog":
                name = (parse_qs(u.query).get("name") or [""])[0]
                logs_dir = (Path(self.cfg["studio"]["workspace_abs"]) / "logs").resolve()
                target = (logs_dir / name).resolve()
                if name.endswith(".log") and target.exists() and target.is_relative_to(logs_dir):
                    content = target.read_text(encoding="utf-8", errors="replace")[-30000:]
                    self._send(200, json.dumps({"content": content}, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                else:
                    self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
            elif u.path == "/api/inbox":
                inbox = _workspace_inbox(self.cfg)
                files = [{"name": f.name, "size": f.stat().st_size} for f in sorted(inbox.glob("*.txt"))]
                self._send(200, json.dumps({"files": files, "decon": DECON_STATE},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/rag":
                qs = parse_qs(u.query)
                hits = mat_mod and rag_mod.search(
                    self.cfg, (qs.get("q") or [""])[0], top_k=int((qs.get("top") or ["8"])[0]))
                self._send(200, json.dumps({"items": hits or []}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/schedules":
                from . import scheduler as sch

                body = json.dumps({
                    "jobs": sch.load_schedules(self.cfg),
                    "scheduler_alive": sch.scheduler_alive(self.cfg),
                    "last_heartbeat": (Path(self.cfg["studio"]["workspace_abs"]) / ".scheduler_heartbeat").read_text(encoding="utf-8").strip()
                    if (Path(self.cfg["studio"]["workspace_abs"]) / ".scheduler_heartbeat").exists() else "",
                }, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/tropes":
                tropes = json.loads((ROOT / "data" / "tropes.json").read_text(encoding="utf-8"))
                body = json.dumps({"names": [d["name"] for d in tropes["directions"]]},
                                  ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/config":
                body = json.dumps(wb.load_config_dict(CFG_FILE), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/models":
                body = json.dumps(list_models(self.cfg), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/kb-units":
                from . import kb as kb_mod

                qs = parse_qs(u.query)
                kind = (qs.get("kind") or [""])[0]
                arc = (qs.get("arc") or [""])[0]
                src = (qs.get("src") or [""])[0]
                q = (qs.get("q") or [""])[0].strip()
                offset = int((qs.get("offset") or ["0"])[0] or 0)
                limit = min(200, int((qs.get("limit") or ["80"])[0] or 80))
                units = kb_mod.load_units(self.cfg)
                hits = []
                ql = q.lower()
                for u in reversed(units):   # 新入库在前
                    if kind and u.get("kind") != kind:
                        continue
                    if arc and u.get("arc") != arc:
                        continue
                    if src and not (u.get("source") or "").startswith(src):
                        continue
                    if ql and ql not in (u.get("text", "").lower() + u.get("source", "").lower()):
                        continue
                    hits.append({"id": u.get("id"), "kind": u.get("kind"),
                                 "arc": u.get("arc", ""), "direction": u.get("direction", ""),
                                 "text": u.get("text", ""), "source": u.get("source", "")})
                body = json.dumps({"total": len(hits),
                                   "items": hits[offset:offset + limit]},
                                  ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/decon-list":
                d = Path(self.cfg["studio"]["workspace_abs"]) / "decon"
                docs = []
                for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
                    try:
                        j = json.loads(f.read_text(encoding="utf-8"))
                        docs.append({"file": f.name,
                                     "title": (j.get("title") or f.stem)[:40],
                                     "sections": len(j.get("sections", [])),
                                     "direction": j.get("direction", ""),
                                     "arcs": j.get("arc_map", {})})
                    except Exception:  # noqa: BLE001
                        continue
                total = len(list(d.glob("*.json"))) if d.exists() else 0
                body = json.dumps({"total": total, "docs": docs}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/decon-doc":
                name = (parse_qs(u.query).get("file") or [""])[0]
                safe = Path(name).name   # 防目录穿越
                f = Path(self.cfg["studio"]["workspace_abs"]) / "decon" / safe
                if not f.exists() or f.suffix != ".json":
                    self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                else:
                    body = json.dumps(json.loads(f.read_text(encoding="utf-8")),
                                      ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/agents":
                body = json.dumps(wb.list_agents(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/agent":
                task = (parse_qs(u.query).get("task") or [""])[0]
                data = wb.read_agent(task)
                if data is None:
                    self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                else:
                    self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
            elif u.path == "/api/sources":
                body = json.dumps(wb.sources_state(self.cfg), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/materials":
                qs = parse_qs(u.query)
                view = (qs.get("view") or [""])[0]
                if view == "trash":
                    body = json.dumps({"items": mat_mod.list_trash(self.cfg)},
                                      ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                    return
                if view == "kbstats":
                    from . import kb as kb_mod

                    st = dict(kb_mod.stats(self.cfg))
                    st["decon"] = _decon_progress(self.cfg)
                    st["laya"] = _laya_status(self.cfg)
                    st["dedup"] = {k: DEDUP_STATE[k] for k in ("running", "result", "error")}
                    st["novel_fetch"] = {k: FETCH_STATE[k] for k in ("running", "result", "error")}
                    try:
                        from . import fetch_novel as _fn

                        st["novel_sources"] = [s.get("name") for s in _fn.sources_of(self.cfg)]
                    except Exception:  # noqa: BLE001
                        st["novel_sources"] = []
                    body = json.dumps(st, ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                    return
                if view == "memory":
                    from . import memory as mem_mod

                    lays = mem_mod.layers(self.cfg)
                    body = json.dumps({
                        "layers": {k: [{"id": it.get("id"), "type": it.get("type"),
                                        "direction": it.get("direction"),
                                        "quality": it.get("quality"),
                                        "heat": int(it.get("usage_count") or 0),
                                        "text": (it.get("text") or "")[:70]}
                                       for it in v[:12]] for k, v in lays.items()},
                    }, ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                    return
                data = mat_mod.query(
                    self.cfg,
                    mtype=(qs.get("type") or [""])[0],
                    direction=(qs.get("direction") or [""])[0],
                    keyword=(qs.get("q") or [""])[0],
                    limit=100,
                )
                body = json.dumps({"stats": mat_mod.stats(self.cfg), "items": data},
                                  ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/manuscript":
                qs = parse_qs(u.query)
                name = (qs.get("name") or [""])[0]
                root = _project_root(self.cfg, name)
                if root is None:
                    self._send(404, b'{"error":"project not found"}', "application/json; charset=utf-8")
                    return
                rel = (qs.get("rel") or [""])[0]
                if rel:
                    data = wb.read_manuscript(root, rel)
                    if data is None:
                        self._send(404, b'{"error":"file not found"}', "application/json; charset=utf-8")
                    else:
                        self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                   "application/json; charset=utf-8")
                else:
                    body = json.dumps({"files": wb.list_manuscript(root)}, ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/":
                body = _render(None).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > 128 * 1024 * 1024:
                self._send(413, b'{"error":"payload too large"}', "application/json; charset=utf-8")
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if u.path == "/api/inbox/upload":
                # 看板直接导入 txt（可多选批量）：[{name, data(base64)}] → workspace/inbox/
                import base64 as _b64
                import re as _re

                inbox = _workspace_inbox(self.cfg)
                inbox.mkdir(parents=True, exist_ok=True)
                saved, skipped = [], []
                for f in (payload.get("files") or [])[:50]:
                    name = _re.sub(r'[\\/:*?"<>|\r\n]', "_", str(f.get("name") or "")).strip()
                    if not name.lower().endswith(".txt"):
                        name += ".txt"
                    try:
                        raw = _b64.b64decode(f.get("data") or "")
                    except Exception:  # noqa: BLE001
                        skipped.append(name)
                        continue
                    if not raw:
                        skipped.append(name)
                        continue
                    (inbox / name).write_bytes(raw)
                    saved.append(name)
                self._send(200, json.dumps({"ok": True, "saved": saved, "skipped": skipped},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/config":
                saved = wb.save_config_dict(payload, CFG_FILE)
                self._send(200, json.dumps({"ok": True, "config": saved}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/sources":
                state = wb.save_sources(self.cfg, payload, CFG_FILE)
                self.cfg["studio"]["sources"] = state["enabled"]
                self.cfg["studio"]["dailyhot_base"] = state["dailyhot_base"]
                self._send(200, json.dumps({"ok": True, **state}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path.startswith("/api/agent/"):
                task = u.path.rsplit("/", 1)[-1]
                res = wb.save_agent(task, payload.get("raw", ""), create=bool(payload.get("create")))
                code = 200 if res.get("ok") else 400
                self._send(code, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/project/delete":
                res = wb.delete_project(self.cfg, payload.get("name", ""))
                code = 200 if res.get("ok") else 404
                self._send(code, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/model-test":
                import time as _t

                llm_cfg = {
                    "base_url": payload.get("base_url", ""),
                    "model": payload.get("model", ""),
                    "api_key": payload.get("api_key", ""),
                    "temperature": 0.1, "max_tokens": 512,
                    "timeout": 60, "retries": 1, "stream": False,
                }
                out = {"base_url": llm_cfg["base_url"], "model": llm_cfg["model"]}
                try:
                    from .llm import OpenAICompatLLM as _OCL

                    probe = _OCL(llm_cfg)
                    r = probe.session.get(f"{llm_cfg['base_url'].rstrip('/')}/models", timeout=10)
                    out["models_count"] = len((r.json().get("data")) or [])
                except Exception as e:  # noqa: BLE001
                    out["ok"] = False
                    out["error"] = f"连接失败：{type(e).__name__}: {str(e)[:100]}"
                    self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                try:
                    t0 = _t.time()
                    text = probe.chat("你是一个回声机。", "只回复两个字：正常")
                    out["ok"] = True
                    out["latency"] = round(_t.time() - t0, 1)
                    out["sample"] = text.strip()[:40]
                except Exception as e:  # noqa: BLE001
                    out["ok"] = False
                    out["error"] = f"生成失败：{str(e)[:120]}"
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/materials/delete":
                ok = mat_mod.remove_material(self.cfg, payload.get("id", ""))
                self._send(200, json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/materials/add":
                item = mat_mod.add_material(
                    self.cfg, payload.get("type", "核心梗"), payload.get("text", ""),
                    direction=payload.get("direction", ""), platform=payload.get("platform", ""),
                    note=payload.get("note", ""),
                )
                self._send(200, json.dumps({"ok": True, "item": item}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/generate":
                from . import scheduler as sch

                # sources 兼容字符串/列表两种传法，统一成列表
                _srcs = payload.get("sources") or []
                if isinstance(_srcs, str):
                    _srcs = [s.strip() for s in _srcs.split(",") if s.strip()]
                _dirs = payload.get("directions") or (
                    [payload.get("direction")] if payload.get("direction") else [])
                if isinstance(_dirs, str):
                    _dirs = [s.strip() for s in _dirs.split(",") if s.strip()]
                job = sch.new_job(
                    self.cfg,
                    payload.get("name") or (f"创作_{now_ts()[5:16]}" if not payload.get("idea")
                                            else f"创作_{str(payload.get('idea'))[:18]}"),
                    {"type": "manual"},
                    count=int(payload.get("count", 1)),
                    directions=_dirs,
                    words=payload.get("words"),
                    platform=payload.get("platform") or "",
                    sources=_srcs,
                    provider=payload.get("provider") or "",
                    auto_publish=bool(payload.get("auto_publish")),
                    auto_data=bool(payload.get("auto_data")),
                    auto_improve=bool(payload.get("auto_improve")),
                    publish_mode=payload.get("publish_mode") or "manual",
                )
                if payload.get("idea"):
                    job["idea"] = payload["idea"]
                jobs = sch.load_schedules(self.cfg)
                jobs.append(job)
                sch.save_schedules(self.cfg, jobs)

                import threading as _th

                GEN_RUNNING.add(job["id"])

                def _worker(j=job):
                    try:
                        sch.run_job(self.cfg, j)
                    except Exception as e:  # noqa: BLE001
                        print(f"[generate] 失败：{e}")
                    finally:
                        GEN_RUNNING.discard(j.get("id"))

                _th.Thread(target=_worker, daemon=True).start()
                self._send(200, json.dumps({"ok": True, "job_id": job["id"]},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/deconstruct/run":
                import threading as _th

                from .llm import get_llm

                fname = payload.get("file", "")
                inbox = _workspace_inbox(self.cfg)
                target = inbox / fname
                if not target.exists() or target.suffix != ".txt":
                    self._send(404, b'{"error":"inbox file not found"}', "application/json; charset=utf-8")
                    return
                if DECON_STATE.get("running"):
                    self._send(409, b'{"error":"deconstruct already running"}', "application/json; charset=utf-8")
                    return

                def _worker(f=target, mx=int(payload.get("max_sections") or self.cfg.get("decon", {}).get("max_sections", 24))):
                    DECON_STATE.update({"running": True, "done": 0, "total": 0, "current": f.name, "error": ""})
                    try:
                        llm = resolve_llm_for_dashboard(self.cfg)
                        res = dec.deconstruct_file(self.cfg, llm, f, max_sections=mx)
                        n = int(res.get("section_count") or len(res.get("sections") or []) or 1)
                        DECON_STATE["total"] = n
                        DECON_STATE["done"] = n
                    except Exception as e:  # noqa: BLE001
                        DECON_STATE["error"] = str(e)[:200]
                    finally:
                        DECON_STATE["running"] = False

                _th.Thread(target=_worker, daemon=True).start()
                self._send(200, b'{"ok":true,"started":true}', "application/json; charset=utf-8")
            elif u.path == "/api/materials/curate":
                from .llm import get_llm, resolve_llm

                llm = resolve_llm(self.cfg, get_llm(self.cfg), "light")
                res = mat_mod.curate(
                    self.cfg, llm,
                    direction=payload.get("direction", ""),
                    batch_size=int(payload.get("batch") or self.cfg.get("materials", {}).get("curate_batch", 15)),
                    max_batches=int(payload.get("max_batches") or 6),
                )
                self._send(200, json.dumps({"ok": True, **res}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/materials/restore":
                ok = mat_mod.restore_material(self.cfg, payload.get("id", ""))
                self._send(200, json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/kb/recall":
                from . import kb as kb_mod

                block, used = kb_mod.recall(self.cfg, payload.get("q", ""),
                                            arc=payload.get("arc", ""),
                                            direction=payload.get("direction", ""),
                                            max_chars=900)
                self._send(200, json.dumps({"block": block, "used": used},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/novel-fetch":
                if FETCH_STATE["running"]:
                    self._send(409, json.dumps({"ok": False, "error": "抓取任务进行中"},
                                               ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                FETCH_STATE.update({"running": True, "result": None, "error": ""})
                threading.Thread(target=_novel_fetch_worker, daemon=True, kwargs={
                    "cfg": self.cfg,
                    "source": str(payload.get("source") or ""),
                    "top": int(payload.get("top") or 2),
                }).start()
                self._send(200, json.dumps({"ok": True, "started": True,
                                            "note": "抓取+深拆在后台执行，稍后刷新查看结果"},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/kb/dedup":
                import time as _t

                state_f = Path(self.cfg["studio"]["workspace_abs"]) / "kb" / "decon_state.json"
                if state_f.exists() and _t.time() - state_f.stat().st_mtime < 180:
                    self._send(409, json.dumps({"ok": False,
                                                "error": "批量深拆正在写库，请先暂停批量深拆再执行去重"},
                                               ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                if DEDUP_STATE["running"]:
                    self._send(409, json.dumps({"ok": False, "error": "去重已在进行中"},
                                               ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                DEDUP_STATE.update({"running": True, "result": None, "error": ""})
                threading.Thread(target=_dedup_worker, daemon=True, kwargs={
                    "cfg": self.cfg,
                    "semantic": bool(payload.get("semantic", True)),
                    "threshold": float(payload.get("threshold", 0.93) or 0.93),
                }).start()
                self._send(200, json.dumps({"ok": True, "started": True,
                                            "note": "去重在后台执行，知识结构页可查看进度"},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/memory/consolidate":
                from . import memory as mem_mod

                rep = mem_mod.consolidate(self.cfg, dry_run=bool(payload.get("dry_run")))
                self._send(200, json.dumps({"ok": True, **rep}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/schedules/save":
                from . import scheduler as sch

                job = payload.get("job") or {}
                jobs = sch.load_schedules(self.cfg)
                if job.get("id"):
                    for i, j in enumerate(jobs):
                        if j.get("id") == job["id"]:
                            jobs[i] = {**j, **job}
                            break
                else:
                    nj = sch.new_job(self.cfg, job.get("name", "定时撰写"),
                                     job.get("trigger", {"type": "interval", "minutes": 180}),
                                     count=job.get("count", 1), directions=job.get("directions"),
                                     words=job.get("words"), platform=job.get("platform"),
                                     sources=job.get("sources"), provider=job.get("provider", ""))
                    jobs.append(nj)
                sch.save_schedules(self.cfg, jobs)
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif u.path == "/api/schedules/toggle":
                from . import scheduler as sch

                jobs = sch.load_schedules(self.cfg)
                for j in jobs:
                    if j.get("id") == payload.get("id"):
                        j["enabled"] = not j.get("enabled", True)
                sch.save_schedules(self.cfg, jobs)
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            elif u.path == "/api/schedules/delete":
                from . import scheduler as sch

                if payload.get("id") in GEN_RUNNING:
                    self._send(409, json.dumps({"ok": False,
                                                "error": "该生成任务正在进行中，不能删除"},
                                               ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                sch.save_schedules(self.cfg, [j for j in sch.load_schedules(self.cfg)
                                              if j.get("id") != payload.get("id")])
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            elif u.path == "/api/schedules/run":
                import subprocess

                from . import scheduler as sch

                jobs = sch.load_schedules(self.cfg)
                job = next((j for j in jobs if j.get("id") == payload.get("id")), None)
                if job is None:
                    self._send(404, b'{"error":"job not found"}', "application/json; charset=utf-8")
                    return
                run_cmd = [sys.executable, "-m", "studio", "schedule", "run", "--job", job["id"]]
                if CFG_FILE:   # 看板用了非默认配置时，子进程必须读同一份，否则找不到任务
                    run_cmd += ["--config", CFG_FILE]
                subprocess.Popen(run_cmd,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._send(200, b'{"ok":true,"started":true}', "application/json; charset=utf-8")
            elif u.path == "/api/manuscript":
                name = payload.get("name", "")
                root = _project_root(self.cfg, name)
                if root is None:
                    self._send(404, b'{"error":"project not found"}', "application/json; charset=utf-8")
                    return
                res = wb.save_manuscript(root, payload.get("rel", ""), payload.get("content", ""))
                code = 200 if res.get("ok") else 400
                self._send(code, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001
                pass


    def do_DELETE(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path.startswith("/api/agent/"):
            task = u.path.rsplit("/", 1)[-1]
            res = wb.delete_agent(task)
            code = 200 if res.get("ok") else 400
            self._send(code, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")


def _project_root(cfg: dict, name: str) -> Path | None:
    base = Path(cfg["studio"]["workspace_abs"]) / "projects"
    if not base.exists():
        return None
    for d in base.iterdir():
        if d.is_dir() and d.name == name:
            return d
    return None


def list_models(cfg: dict) -> list[dict]:
    """探测已配置的各端点可用模型（本地 ollama / 云端 /v1/models）。"""
    import requests

    bases = []
    llm = cfg.get("llm", {}) or {}
    bases.append(llm.get("base_url", ""))
    for rcfg in (cfg.get("llm_roles") or {}).values():
        b = (rcfg or {}).get("base_url")
        if b:
            bases.append(b)
    out = []
    for b in dict.fromkeys(x for x in bases if x):
        models = []
        try:
            r = requests.get(f"{b.rstrip('/')}/models", timeout=6,
                             headers={"Authorization": "Bearer " + (llm.get("api_key", "") or "")})
            models = [m.get("id", "?") for m in (r.json().get("data") or [])][:60]
        except Exception as e:  # noqa: BLE001
            models = [f"(不可达: {type(e).__name__})"]
        out.append({"base_url": b, "models": models})
    return out


def make_server(cfg: dict, port: int = 8600) -> ThreadingHTTPServer:
    handler = type("H", (_Handler,), {"cfg": cfg})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(cfg: dict, port: int = 8600, open_browser: bool = True, config_file: str | None = None) -> str:
    global CFG_FILE
    CFG_FILE = config_file
    _laya_preload(cfg)   # 后台预热判别器，不阻塞启动
    srv = None
    for p in range(port, port + 10):
        try:
            srv = make_server(cfg, p)
            port = p
            break
        except OSError:
            print(f"[dashboard] 端口 {p} 被占用，尝试下一个 …")
    if srv is None:
        raise SystemExit(f"端口 {port}-{port+9} 均被占用，用 --port 换一个")
    url = f"http://127.0.0.1:{port}/"
    print(f"[dashboard] 看板运行中：{url}  （Ctrl+C 停止；每 20s 自动刷新可在页面里开关）")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
    finally:
        srv.server_close()
    return url


# ---------------------------------------------------------------------------
# 页面模板（纯 CSS 图表，无外部依赖）
# ---------------------------------------------------------------------------

def _render(payload: dict | None) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else "null"
    return TEMPLATE.replace("__PAYLOAD__", data_json)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>短篇小说工作室 · 看板</title>
<style>
:root{--bg:#f7f7f4;--card:#ffffff;--card2:#f0f0ec;--tx:#141414;--dim:#5c5c5c;
--ink:#111111;--line:#141414;--line2:#c9c9c2}
*{box-sizing:border-box}
::selection{background:#141414;color:#fff}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#bdbdb6;border:2px solid var(--bg);border-radius:0}
::-webkit-scrollbar-track{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.7 "Microsoft YaHei",system-ui,sans-serif}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:20px;flex-wrap:wrap;
padding:11px 26px;background:var(--bg);border-bottom:3px double var(--ink)}
.topbar h1{font-size:17px;margin:0;font-weight:900;letter-spacing:1px}
.topbar h1::before{content:"◆ ";font-size:12px}
.wrap{max-width:1220px;margin:0 auto;padding:22px 26px}
h2{font-size:16px;margin:24px 0 12px;font-weight:900;display:flex;align-items:center;gap:9px}
h2::before{content:"";width:9px;height:15px;border:2px solid var(--ink);background:var(--ink)}
h3{font-size:14px;margin:10px 0 8px}
.dim{color:var(--dim);font-size:12px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.kpi{flex:1;min-width:132px;background:var(--card);border:1.5px solid var(--ink);
border-radius:3px;padding:11px 14px;box-shadow:3px 3px 0 var(--ink);transition:.12s}
.kpi:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--ink)}
.kpi b{font-size:23px;display:block;font-weight:900}
.kpi span{color:var(--dim);font-size:12px}
.card{background:var(--card);border:1.5px solid var(--ink);border-radius:3px;
padding:13px 16px;margin-bottom:13px;box-shadow:3px 3px 0 var(--ink)}
table{width:100%;border-collapse:collapse;background:var(--card)}
th,td{padding:8px 11px;border:1px solid var(--line);text-align:left;font-size:13px}
th{font-weight:900;background:var(--card2);letter-spacing:.5px}
tbody tr:hover td,tr:hover td{background:var(--card2)}
.badge{display:inline-block;padding:0 8px;border:1.5px solid var(--ink);border-radius:2px;
font-size:11.5px;margin-right:3px;background:var(--card)}
.b-good{background:var(--ink);color:#fff}
.b-warn{background:repeating-linear-gradient(45deg,#fff 0 4px,#ddd 4px 6px);color:var(--ink)}
.b-bad{background:#fff;color:var(--ink);border-style:dashed}
.b-dim{color:var(--dim);border-color:var(--dim)}
.b-acc{background:var(--card2);color:var(--ink);font-weight:700}
.stages{display:inline-flex;gap:0}
.stages i{width:20px;height:9px;border:1.5px solid var(--ink);border-right:none;display:inline-block;background:#fff}
.stages i:last-child{border-right:1.5px solid var(--ink)}
.stages i.on{background:var(--ink)}
.bar{background:#fff;border:1.5px solid var(--ink);height:16px;position:relative;min-width:60px}
.bar>div{height:100%;background:repeating-linear-gradient(45deg,var(--ink) 0 2px,transparent 2px 7px)}
.bar span{position:absolute;left:6px;top:-1px;font-size:11px;color:var(--ink);font-weight:700}
.pie{width:140px;height:140px;border-radius:50%;margin:6px auto;border:1.5px solid var(--ink)}
.legend{font-size:12px;color:var(--dim)}
.legend i{display:inline-block;width:10px;height:10px;border:1.5px solid var(--ink);margin-right:5px;vertical-align:-1px}
a{color:var(--tx);text-decoration:underline;text-underline-offset:3px;cursor:pointer}
a:hover{background:var(--ink);color:#fff;text-decoration:none}
.dircols{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:6px 18px;font-size:13px}
.tabs{display:flex;gap:0;flex-wrap:wrap}
.tab{padding:6px 18px;cursor:pointer;color:var(--tx);font-size:13px;font-weight:700;
border:1.5px solid var(--ink);border-left:none;background:var(--card);transition:.12s}
.tab:first-child{border-left:1.5px solid var(--ink)}
.tab:hover{background:var(--card2)}
.tab.on{background:var(--ink);color:#fff}
textarea.ed{width:100%;min-height:260px;background:#fff;color:var(--tx);border:1.5px solid var(--ink);
border-radius:0;padding:10px;font:13px/1.6 Consolas,monospace;outline:none}
input.f,select.f{background:#fff;color:var(--tx);border:1.5px solid var(--ink);
border-radius:0;padding:6px 9px;width:100%;outline:none;font-size:13px}
input.f:focus,select.f:focus,textarea.ed:focus{background:var(--card2)}
.btn{background:var(--ink);color:#fff;border:1.5px solid var(--ink);border-radius:0;
padding:6px 16px;cursor:pointer;font-size:13px;font-weight:700;transition:.12s}
.btn:hover{background:#fff;color:var(--ink)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.gray{background:#fff;color:var(--ink)}
.btn.gray:hover{background:var(--ink);color:#fff}
.btn.danger{background:#fff;color:var(--ink);border-style:dashed}
label.sw{font-size:12px;color:var(--dim);cursor:pointer}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:800px){.grid2{grid-template-columns:1fr}}
.toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}

.toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
label.sw{font-size:12px;color:var(--dim);cursor:pointer}
.dircols{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:6px 18px;font-size:13px}
</style>
</head>
<div class="topbar">
  <h1>短篇小说工作室 <sup style="font-size:11px;color:var(--dim)">v2026-10-07·题材思维链+热门抓取</sup></h1>
  <div class="tabs" id="tabs">
  <a class="tab on" onclick="go('')">总览</a>
  <a class="tab" onclick="go('create')">生成小说</a>
  <a class="tab" onclick="go('docs')">文稿编辑</a>
  <a class="tab" onclick="go('agents')">Agent 管理</a>
  <a class="tab" onclick="go('config')">模型与配置</a>
  <a class="tab" onclick="go('mat')">素材库</a>
  </div>
  <div style="margin-left:auto;display:flex;gap:14px;align-items:center">
    <label class="sw"><input type="checkbox" id="auto" checked> 自动刷新(20s)</label>
    <label class="sw"><input type="checkbox" id="showAll"> 显示全部热点</label>
  </div>
</div>
<div class="wrap">
<div class="dim" id="genAt" style="margin-bottom:6px"></div>
<div id="view"></div>
</div>
<script>
const STATIC = __PAYLOAD__;
const H = s => String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const STAGE = ['梗卡','方案','导语','大纲','圣经','章节','成稿'];
const COLORS = ['#6c8cff','#34d399','#fbbf24','#f87171','#9d6cff','#4dd0e1','#f06292','#aed581','#ffb74d','#9575cd'];
const fmtW = n => n>=10000 ? (n/10000).toFixed(1)+'万' : n;
const badge = (txt, cls) => `<span class="badge ${cls}">${H(txt)}</span>`;
async function postJSON(url, obj){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj)});
  const data = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error || ('HTTP '+r.status));
  return data;
}

function stageBar(i){return `<span class="stages">${STAGE.map((s,k)=>`<i class="${k<i?'on':''}" title="${s}"></i>`).join('')}</span>`;}

function kpiCard(v,label,sub){return `<div class="kpi"><b>${v}</b><span>${label}${sub?` · ${sub}`:''}</span></div>`;}

function pieChart(items){ // items:[{name,value}]
  const total = items.reduce((s,x)=>s+x.value,0);
  if(!total) return '<div class="dim">暂无数据</div>';
  let acc=0, stops=[];
  items.forEach((x,i)=>{const from=acc/total*360; acc+=x.value; const to=acc/total*360;
    stops.push(`${COLORS[i%COLORS.length]} ${from}deg ${to}deg`);});
  const legend = items.map((x,i)=>`<div><i style="display:inline-block;width:10px;height:10px;border-radius:3px;background:${COLORS[i%COLORS.length]};margin-right:6px"></i>${H(x.name)}：${x.value}</div>`).join('');
  return `<div class="pie" style="background:conic-gradient(${stops.join(',')})"></div><div class="legend">${legend}</div>`;
}

function barRows(items, max, fmt){ // items:[{label,value,text}]
  if(!items.length) return '<div class="dim">暂无数据</div>';
  const mx = max || Math.max(...items.map(x=>x.value),1);
  return items.map(x=>`<div style="display:flex;align-items:center;gap:8px;margin:3px 0">
    <div style="width:270px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px" title="${H(x.label)}">${H(x.label)}</div>
    <div class="bar" style="flex:1"><div style="width:${Math.max(2,x.value/mx*100)}%"></div><span>${x.text??H(x.label)}</span></div></div>`).join('');
}

async function getOverview(){
  if (STATIC) return STATIC.overview;
  return await (await fetch('/api/overview')).json();
}
async function getDetail(name){
  if (STATIC) return STATIC.details[name];
  const r = await fetch('/api/project?name='+encodeURIComponent(name));
  if(!r.ok) return null; return await r.json();
}

function renderOverview(o){
  const k=o.kpis;
  let html = `<div class="row">
    ${kpiCard(k.projects,'项目',`${k.finished} 已成稿`)}
    ${kpiCard(fmtW(k.total_words),'成稿字数')}
    ${kpiCard(k.avg_reader??'—','平均读者分','/10')}
    ${kpiCard(k.gates_pass_rate==null?'—':k.gates_pass_rate+'%','门禁通过率')}
    ${kpiCard(k.kb_cards,'知识卡','18 方向')}
    ${kpiCard(k.hotspots?'有':'无','热点快照',o.hotspots.count+' 条')}
    ${kpiCard(k.published,'已发布')}
    ${kpiCard(fmtW(k.total_reads||0),'累计阅读')}
    ${kpiCard(fmtW(k.tokens_out||0),'输出 tokens')}
  </div>`;

  html += `<h2>项目流水线</h2><table><tr><th>项目</th><th>书名</th><th>题材</th><th>进度</th><th>字数</th><th>读者分</th><th>主编分</th><th>门禁</th><th>成稿体检</th><th>平台</th><th>发布</th><th>阅读/完读</th><th>操作</th></tr>`;
  for(const p of o.projects){
    const gates = p.gates_total? `${p.gates_pass}/${p.gates_total}` : '—';
    const gcls = p.gates_total&&p.gates_pass===p.gates_total?'b-good':(p.gates_pass>0?'b-warn':'b-bad');
    // 降级门单列：LLM 解析失败时 gate=degraded_*，既非 pass 也非 skipped。
    // 不显示出来的话，"0/8 门禁"看不出是稿子差还是模型挂了。
    const gdeg = p.gates_degraded? `<span class="b-warn" title="门禁降级：LLM 输出不可解析，这些章实际未审">降${p.gates_degraded}</span>` : '';
    // 成稿体检：禁用道具 / 语言质检 / 确定性复查。三者都是 pipeline 早就在写的
    // report 字段，以前没消费方——看板上看不见，等于白记。
    const bp = p.banned_props||[];
    const chk = [];
    if(bp.length) chk.push(`<span class="b-bad" title="禁用道具：${H(bp.join('、'))}">道具${bp.length}</span>`);
    if(p.langqa_full_pass===false) chk.push(`<span class="b-warn" title="语言质检 ${p.langqa_full_hits??'?'} 处命中">语检${p.langqa_full_hits??''}</span>`);
    if(p.recheck_pass===false) chk.push(`<span class="b-bad">复查✗</span>`);
    // 伏笔账本：埋了没收 / 回收章里找不到痕迹，都在列表页亮出来。
    if(p.foreshadow_ok===false) chk.push(`<span class="b-warn" title="伏笔 ${p.foreshadow_total??'?'} 条，超期 ${p.foreshadow_overdue??0} 条、回收章无痕迹 ${p.foreshadow_missing??0} 条">伏笔${p.foreshadow_overdue?p.foreshadow_overdue:''}${p.foreshadow_missing?('/'+p.foreshadow_missing):''}</span>`);
    // 字数门保稿：项目还在工作台但字数越界，必须显式标出来（以前是直接挪进
    // rejected/，看板上人间蒸发）。
    if(p.length_out_of_range) chk.push(`<span class="b-warn" title="${H(p.gate_reason||'字数越界')}">字数越界</span>`);
    // 语言打磨：before/after 缺陷分直接标出来。用户抱怨的"碎句/语序/冗余/详略"
    // 是读起来才有的病，看板必须给出"改了几节、分降了没有"，否则只能人肉读全文。
    if(p.lang_before!=null && p.lang_after!=null && p.lang_before>0)
      chk.push(`<span class="${p.lang_after<p.lang_before?'b-ok':'b-warn'}" title="语言缺陷分 ${p.lang_before}→${p.lang_after}；改写 ${p.lang_rewritten??0} 节、回滚 ${p.lang_rejected??0} 节、去重 ${p.lang_deduped??0} 段">语修${p.lang_before}→${p.lang_after}</span>`);
    // 投稿线结论：语言打磨"改过"不等于"达标"。没有这一格，看板只能看到改动量，
    // 看不出这篇到底能不能投——四类语言病（语序/详略/冗余/碎句）超线时必须显红。
    if(p.market_lang_pass===true)
      chk.push(`<span class="b-ok" title="四类语言病均在投稿线内">语言达标</span>`);
    else if(p.market_lang_pass===false){
      // 复检优先：判据修好后老报告会永远停在旧结论（e2e7 曾因"台词被句号切断"
      // 误判碎句判成 C 不可投）。有复检结论就按复检显示，并注明原始判定未改。
      const rc = p.lang_recheck||{};
      if(rc.pass===true)
        chk.push(`<span class="b-ok" title="按当前判据复检 ${H(rc.grade||'')}；原始判定保留：${H(p.market_lang_fails||'')}">语言复检达标</span>`);
      else
        chk.push(`<span class="b-bad" title="未达投稿线：${H((rc.fails&&rc.fails.length?rc.fails.join('；'):p.market_lang_fails)||'')}">语言超线</span>`);
    }
    // 投稿判定（studio/verdict.py）：A/B/C 一格显示，鼠标悬停看原因。
    // 它与上面的"语言达标/超线"不是重复：语言只是三条硬线之一，字数越界、
    // 主编分低于 7.0 也会让这篇不能投。
    if(p.submission_grade)
      chk.push(`<span class="${p.submission_ready?'b-ok':'b-bad'}" title="${H(p.submission_reasons||'硬线全过')}">${H(p.submission_grade)}</span>`);
    // 书名卖点分：签约第一道门槛。低分不判死（正文还能救），但必须能被一眼看到。
    if(p.title_score!=null)
      chk.push(`<span class="${p.title_score>=5?'b-ok':'b-warn'}" title="《${H(p.title||'')}》卖点分 ${p.title_score}/10（${H(p.title_family||'-')}）；签约第一道门槛">书名${p.title_score}</span>`);
    const chkTxt = chk.length? chk.join(' ') : '—';
    html += `<tr><td><a onclick="route('${H(p.name)}')">${H(p.name.slice(0,20))}…</a></td>
      <td>${H(p.title)}</td><td>${badge(p.direction||'—','b-dim')}</td>
      <td>${stageBar(p.stage_index)}</td>
      <td>${p.final_words?fmtW(p.final_words):'—'}</td>
      <td>${p.reader_avg??(p.reader_avg_chapters??'—')}</td>
      <td>${p.score??'—'}</td>
      <td>${gates==='—'?gates:badge(gates,gcls)}${gdeg}</td>
      <td>${chkTxt}</td>
      <td>${badge(p.platform_pass==null?'—':(p.platform_pass?'达标':'不足'), p.platform_pass?'b-good':'b-warn')}</td>
      <td>${p.published?badge(p.publish_mode==='manual'?'已打包':'已发布','b-good'):'—'}${p.platform?`<span class="dim" title="目标平台"> ${H(p.platform)}</span>`:''}</td>
      <td>${p.reads!=null?`${fmtW(p.reads)} / ${p.finish_rate!=null?Math.round(p.finish_rate*100)+'%':'—'}`:'—'}</td>
      <td><a style="color:var(--bad)" onclick="projDel('${H(p.name)}')">删除</a></td></tr>`;
  }
  if(!o.projects.length) html += `<tr><td colspan="12" class="dim">还没有项目，先跑一次：python -m studio run --idea "你的核心梗" --mock</td></tr>`;
  html += `</table>`;

  const dirItems = o.directions.filter(d=>d.projects>0).sort((a,b)=>b.projects-a.projects).map(d=>({name:d.name,value:d.projects}));
  const dirHtml = `<div class="card"><b>题材分布</b><div class="grid2"><div>${pieChart(dirItems)}</div>
    <div class="dircols">${o.directions.map(d=>`<div><span class="heat">${'★'.repeat(d.heat)}${'☆'.repeat(5-d.heat)}</span> ${H(d.name)}
      ${d.has_card?badge('知识卡','b-good'):badge('无卡','b-dim')} ${d.projects?`· ${d.projects}篇`:''}</div>`).join('')}</div></div></div>`;

  const hp = o.hotspots.platforms||{};
  const platBars = Object.entries(hp).sort((a,b)=>b[1]-a[1]).map(([s,c])=>({label:s,value:c,text:c+' 条'}));
  const topItems = (document.getElementById('showAll')?.checked ? o.hotspots.items : o.hotspots.items.slice(0,15))
    .map(it=>({label:`[${it.source}] ${it.title}`, value:it.hot_value||0, text:String(Math.round(it.hot_value/10000))+'万'}))
    .sort((a,b)=>b.value-a.value);
  html += `<h2>题材与知识库</h2>${dirHtml}
    <h2>热点榜 <span class="dim">${H(o.hotspots.file||'（无快照，跑一次 hot 命令）')}</span></h2>
    <div class="grid2"><div class="card"><b>平台分布</b>${barRows(platBars)}</div>
    <div class="card"><b>热度 Top</b>${barRows(topItems)}</div></div>`;

  const uk = Object.keys(o.usage);
  html += `<h2>Token 用量</h2><div class="card">${
    uk.length? `<table><tr><th>角色(模型)</th><th>调用</th><th>输入 tokens</th><th>输出 tokens</th></tr>` +
      uk.map(k=>{const v=o.usage[k];return `<tr><td>${H(k)}</td><td>${v.calls}</td><td>${v.prompt_tokens}</td><td>${v.completion_tokens}</td></tr>`;}).join('')+`</table>`
    : '<span class="dim">暂无用量数据（成稿后自动记录）</span>'}</div>`;
  return html;
}

function renderDetail(d){
  if(!d) return '<div class="dim">项目不存在</div>';
  const p = d.pitch, rep = d.report||{}, full=(rep.review||{}).full_story||{};
  let html = `<div><a onclick="route('')">← 返回总览</a></div>
    <h2 style="margin-top:10px">《${H(p.title||'未定名')}》</h2>
    <div class="card">${H(p.logline||'')}
    <div class="dim" style="margin-top:6px">题材：${H(d.concept.direction||'—')} · 平台：${H(p.platform||'—')} ·
    核心梗：${H(d.concept.core_trope||'—')} · 知识卡：${H(d.kb.direction||'—')}${d.kb.fallback?'（降级内化）':''}</div></div>`;

  html += `<div class="grid2">
    <div class="card"><b>章节与门禁</b><table><tr><th>#</th><th>章名</th><th>字数</th><th>主编</th><th>质量分</th><th>覆盖率</th><th>交付</th><th>读者</th><th>门禁</th></tr>`;
  for(const c of d.chapters){
    const g = c.gate==='pass'?badge('pass','b-good'):badge(c.gate||'—','b-warn');
    const rs = Object.entries(c.reader_scores||{}).map(([k,v])=>`${H(k.slice(0,4))} ${v}`).join('，');
    // 质量分/覆盖率/交付三列来自证据化评审卡（studio/review.py）：只列主编自评
    // 分数会让"审了但没证据"的章节看起来和真审过的一样，所以必须分开显示。
    const cov = c.coverage==null?'—':`${Math.round(c.coverage*100)}%`;
    const unc = (c.uncovered||[]).length?` title="${H((c.uncovered||[]).join('、'))}"`:'';
    const dl = c.delivery==='ready'?badge('可交付','b-good')
             : c.delivery==='blocked'?badge('阻断','b-bad')
             : c.delivery==='thin'?badge('待补','b-warn'):(c.delivery||'—');
    html += `<tr><td>${c.no}</td><td>${H(c.title)}</td><td>${c.words}</td>
      <td>${c.review_score??'—'}（${c.review_rounds??'—'}轮）</td>
      <td>${c.quality??'—'}</td><td${unc}>${cov}</td><td>${dl}</td>
      <td><span title="${H(rs)}">${c.reader_avg??'—'}</span></td><td>${g}</td></tr>`;
  }
  html += `</table></div>
    <div class="card"><b>体检与终评</b>
    <p>主编体检分：${rep.score??'—'} · 成稿字数：${rep.final_words??'—'} ·
    平台达标：${(rep.platform_check||{}).pass?'<b style="color:var(--good)">是</b>':'<b style="color:var(--warn)">否</b>'}</p>
    <p>全稿读者终评：${full.reader_avg??'—'}（阈值 ${full.threshold??'—'}，${full.gate||'—'}）</p>
    ${(rep.rubric_rollup&&rep.rubric_rollup.chapters)?`<p>评审卡：${rep.rubric_rollup.reviewed}/${rep.rubric_rollup.chapters} 章已评 ·
      平均质量 ${rep.rubric_rollup.quality_avg??'未评级'} · 平均覆盖 ${Math.round((rep.rubric_rollup.coverage||0)*100)}%
      ${Object.entries(rep.rubric_rollup.delivery_counts||{}).map(([k,v])=>`· ${H(k)} ${v}`).join('')}</p>`:''}
    ${rep.foreshadow_audit?`<p>伏笔账本：共 ${rep.foreshadow_audit.total} 条 / 已收 ${rep.foreshadow_audit.paid} / 待收 ${rep.foreshadow_audit.open}
      ${rep.foreshadow_audit.ok?'<b style="color:var(--good)">· 全部兑现</b>':`<b style="color:var(--warn)">· ${rep.foreshadow_audit.issues.length} 项待处理</b>`}</p>
      ${(rep.foreshadow_audit.issues||[]).length?`<div class="legend">${rep.foreshadow_audit.issues.map(i=>'· '+H(i)).join('<br>')}</div>`:''}`:''}
    <div class="legend">${Object.entries(full.scores||{}).map(([k,v])=>`${H(k)}：${v}`).join('<br>')}</div></div></div>`;

  if(p.titles) html += `<div class="card"><b>候选书名</b><div class="legend">${p.titles.map(t=>'· '+H(t)).join('<br>')}</div></div>`;
  if(Object.keys(rep.usage||{}).length) html += `<div class="card"><b>本项目用量</b><div class="legend">${
    Object.entries(rep.usage).map(([k,v])=>`${H(k)}：${v.calls} 次 / 出 ${v.completion_tokens} tok`).join('<br>')}</div></div>`;
  return html;
}

async function route(hash){
  const name0 = String(hash||'').replace(/^#\/?/,'').replace(/^p\//,'');
  if(!name0){ const o = await getOverview(); document.getElementById('view').innerHTML = renderOverview(o);
    document.getElementById('genAt').textContent = '生成时间：'+o.generated_at; setTab(''); return; }
  const name = decodeURIComponent(name0);
  const d = await getDetail(name); document.getElementById('view').innerHTML = renderDetail(d); setTab('');
}

function setTab(key){
  document.querySelectorAll('#tabs .tab').forEach(a=>{
    a.classList.toggle('on', a.getAttribute('onclick')===`go('${key}')`);
  });
}
window.go = key => { location.hash = key ? '#/'+key : '#/'; route(key); };

/* ---------- 文稿编辑 ---------- */
async function renderDocs(){
  const o = await getOverview();
  if(!o.projects.length){ document.getElementById('view').innerHTML = '<div class="dim">暂无项目</div>'; return; }
  const sel = `<select id="docProj" class="f" style="max-width:420px">` +
    o.projects.map(p=>`<option value="${H(p.name)}">${H(p.name)}（${H(p.title)}）</option>`).join('') + `</select>`;
  document.getElementById('view').innerHTML = `
    <h2>文稿编辑</h2><div class="card">${sel}
    <button class="btn danger" style="margin-left:12px" onclick="deleteProject(document.getElementById('docProj').value)">删除该项目</button>
    <div id="docFiles" style="margin-top:10px"></div></div>
    <div class="card" id="docEdit" style="display:none">
      <div class="dim" id="docRel"></div>
      <textarea class="ed" id="docText" style="min-height:420px"></textarea><br>
      <button class="btn" onclick="saveDoc()">保存（自动备份 .bak）</button>
      <span class="dim" id="docMsg"></span></div>`;
  window.deleteProject = async function(name){
    if(!confirm('确定彻底删除项目？成稿/章节/数据将一并删除，不可恢复。')) return;
    const inp = prompt('输入完整项目名以确认删除：', name);
    if(inp !== name){ alert('名称不匹配，已取消'); return; }
    try{
      await postJSON('/api/project/delete', {name});
      alert('已删除');
      await renderDocs();
    }catch(e){ alert('删除失败：' + e.message); }
  };
  window.deleteProject = window.deleteProject; // expose
  document.getElementById('docProj').onchange = () => loadDocFiles();
  loadDocFiles();
}
async function loadDocFiles(){
  const name = document.getElementById('docProj').value;
  const data = await (await fetch('/api/manuscript?name='+encodeURIComponent(name))).json();
  document.getElementById('docFiles').innerHTML = (data.files||[]).map(f=>
    `<a onclick="openDoc('${H(name)}','${H(f.rel)}')">${H(f.rel)}</a>`).join(' · ') || '<span class="dim">无文稿</span>';
  document.getElementById('docEdit').style.display = 'none';
}
window.loadDocFiles = loadDocFiles;
async function openDoc(name, rel){
  const data = await (await fetch('/api/manuscript?name='+encodeURIComponent(name)+'&rel='+encodeURIComponent(rel))).json();
  document.getElementById('docEdit').style.display = 'block';
  document.getElementById('docRel').textContent = name + ' / ' + rel;
  document.getElementById('docText').value = data.content || '';
  document.getElementById('docText').dataset.rel = rel;
  document.getElementById('docText').dataset.name = name;
}
window.openDoc = openDoc;
async function saveDoc(){
  const t = document.getElementById('docText');
  try{
    await postJSON('/api/manuscript', {name: t.dataset.name, rel: t.dataset.rel, content: t.value});
    document.getElementById('docMsg').textContent = '已保存 ✔（' + new Date().toLocaleTimeString() + '）';
  }catch(e){ document.getElementById('docMsg').textContent = '保存失败：' + e.message; }
}
window.saveDoc = saveDoc;

/* ---------- Agent 管理 ---------- */
const ROLE_CN = {planner:'策划(提梗/方案/大纲)', writer:'写手(扩写/润色)', reviewer:'审查(主编/情绪)',
                 panel:'读者评分团', light:'轻量(知识蒸馏)', custom:'自定义'};
const TOGGLES = [['langqa_enabled','语言质检门','review'],['enabled','主编审查','review'],
                 ['reader_enabled','读者评分团','review'],['emotion_enabled','情绪量化门','review'],
                 ['auto_collect','题材知识库','knowledge']];
const MODE_NOTE = {
  draft: '★ hybrid 正文用（单写手同上下文续写）',
  rewrite: '仅 agent 模式用（逐章打回重写）',
  polish_final: '仅 agent 模式用（终稿润色）',
  review: 'hybrid 下做大纲/圣经评审 + 整稿终审',
  reader_score: 'hybrid 下做整稿读者验收',
  emotion: '仅 agent 模式用（逐章情绪门）',
  outline: '大纲（两种模式都生效）',
  bible: '圣经（两种模式都生效）',
  pitch: '书名（两种模式都生效）',
  lead: '导语（两种模式都生效）',
  mine: '提梗（两种模式都生效）',
  kb_distill: '知识蒸馏（共用）',
};
async function renderAgents(){
  const [agents, cfg] = await Promise.all([fetch('/api/agents').then(r=>r.json()), fetch('/api/config').then(r=>r.json())]);
  const mode = (cfg.pipeline||{}).mode || 'agent';
  let html = `<h2>Agent 管理</h2>
    <div class="card" style="border-width:2px"><b>正文生成模式</b>
      <select id="p_mode" style="margin-left:10px;min-width:340px">
        <option value="hybrid" ${mode==='hybrid'?'selected':''}>hybrid — 单写手同上下文续写（默认）</option>
        <option value="agent" ${mode==='agent'?'selected':''}>agent — 多agent逐章门禁</option>
      </select>
      <span class="dim" id="pmMsg"></span>
      <div class="dim" style="margin-top:6px">hybrid：正文由「执笔人(draft)」一个写手在同一上下文里从头写到尾，不逐章打回，
        写完全稿后由审查/读者团做整稿终审+验收（实测：体检92分/调用-71%/耗时-53%，已设默认）；
        agent：每章独立上下文+主编/读者/情绪门打回。</div></div>`;
  html += `<div class="card"><b>门禁开关</b><div class="row" style="margin-top:6px">`;
  for(const [key, label, sect] of TOGGLES){
    const on = (cfg[sect]||{})[key];
    html += `<label class="sw"><input type="checkbox" data-toggle="${sect}.${key}" ${on?'checked':''}> ${label}</label>`;
  }
  html += `</div><div class="dim">关闭的环节直接跳过；改动即时写入 config.yaml</div></div>`;
  html += `<div class="card"><b>新建提示词</b>
    <div class="row" style="margin-top:6px">
      <input class="f" id="newAgentName" placeholder="任务名（英文/数字/短横线，如 my_writer）" style="flex:1">
      <button class="btn" onclick="newAgent()">＋ 新建</button>
      <span class="dim" id="naMsg"></span></div>
    <div class="dim" style="margin-top:4px">新建后出现在下方表格，可编辑内容并保存（自动备份 .bak）；仅自定义提示词可删除，内置 agent 受流水线依赖保护</div></div>`;
  html += `<div class="card"><b>提示词（点击编辑，保存自动备份 .bak）</b><table><tr><th>Agent</th><th>角色</th><th>模式</th><th>文件</th><th></th></tr>`;
  for(const a of agents){
    const note = MODE_NOTE[a.task] || (a.role==='planner'||a.role==='light'?'共用（两种模式都生效）':'agent 模式用');
    html += `<tr><td>${H(a.name)}</td><td>${H(ROLE_CN[a.role]||a.role)}</td>
      <td class="dim">${H(note)}</td><td class="dim">${H(a.file)}</td>
      <td><a onclick="editAgent('${H(a.task)}')">${a.exists?'编辑':'新建'}</a>
      ${a.custom?` <a style="color:#c33" onclick="delAgent('${H(a.task)}')">删除</a>`:' <span class="dim">内置</span>'}</td></tr>`;
  }
  html += `</table></div><div class="card" id="agEdit" style="display:none">
    <div class="dim" id="agFile"></div>
    <textarea class="ed" id="agText" style="min-height:460px"></textarea><br>
    <button class="btn" onclick="saveAgent()">保存</button>
    <span class="dim" id="agMsg"></span></div>`;
  document.getElementById('view').innerHTML = html;
  document.getElementById('p_mode').onchange = async function(){
    try{
      await postJSON('/api/config', {pipeline:{mode:this.value}});
      document.getElementById('pmMsg').textContent = '已保存，对下次运行生效 ✔';
    }catch(e){ alert('保存失败：'+e.message); this.value = mode; }
  };
  document.querySelectorAll('[data-toggle]').forEach(cb=>{
    cb.onchange = async () => {
      const [sect, key] = cb.dataset.toggle.split('.');
      try{ await postJSON('/api/config', {[sect]:{[key]: cb.checked}}); }
      catch(e){ alert('保存失败：'+e.message); cb.checked = !cb.checked; }
    };
  });
}
window.renderAgents = renderAgents;
async function newAgent(){
  const name = document.getElementById('newAgentName').value.trim();
  if(!/^[\w\-]{1,40}$/.test(name)){
    document.getElementById('naMsg').textContent = '非法任务名（英文/数字/短横线，≤40字符）'; return;
  }
  try{
    await postJSON('/api/agent/'+encodeURIComponent(name),
      {raw:`<!--TASK:${name.toUpperCase()}-->\n===SYSTEM===\n\n===USER===\n`, create:true});
    document.getElementById('naMsg').textContent = '已创建 ✔';
    document.getElementById('newAgentName').value = '';
    await renderAgents();
  }catch(e){ document.getElementById('naMsg').textContent = '失败：' + e.message; }
}
window.newAgent = newAgent;
async function delAgent(task){
  if(!confirm(`删除提示词 ${task}.md？（仅自定义模板；删除前自动备份 .bak）`)) return;
  try{
    const r = await fetch('/api/agent/'+encodeURIComponent(task), {method:'DELETE'});
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error||'删除失败');
    await renderAgents();
  }catch(e){ alert('删除失败：' + e.message); }
}
window.delAgent = delAgent;
async function editAgent(task){
  const r = await fetch('/api/agent?task='+encodeURIComponent(task));
  let raw = '';
  if(r.ok){ raw = (await r.json()).raw; }
  else { raw = `<!--TASK:${task.toUpperCase()}-->\n===SYSTEM===\n\n===USER===\n`; }
  document.getElementById('agEdit').style.display = 'block';
  document.getElementById('agFile').textContent = 'prompts/' + task + '.md';
  document.getElementById('agText').value = raw;
  document.getElementById('agText').dataset.task = task;
  document.getElementById('agEdit').scrollIntoView({behavior:'smooth'});
}
window.editAgent = editAgent;
async function saveAgent(){
  const t = document.getElementById('agText');
  try{
    await postJSON('/api/agent/' + encodeURIComponent(t.dataset.task), {raw: t.value, create: true});
    document.getElementById('agMsg').textContent = '已保存 ✔';
  }catch(e){ document.getElementById('agMsg').textContent = '失败：' + e.message; }
}
window.saveAgent = saveAgent;

/* ---------- 模型与配置 ---------- */
/* ---------- 创作：生成小说 + 拆书 + RAG ---------- */
async function renderCreate(){
  const [inbox, genjobs] = await Promise.all([
    fetch('/api/inbox').then(r=>r.json()).catch(()=>({files:[],decon:{}})),
    fetch('/api/generate').then(r=>r.json()).catch(()=>({jobs:[]}))]);
  const d = inbox.decon||{};
  const deconLine = d.running
    ? badge('拆书中','b-warn') + ` <span class="dim">${H(d.current)}（${d.done}/${d.total||'?'} 段）</span>`
    : (d.error ? badge('拆书出错：'+H(d.error.slice(0,40)),'b-bad') : badge('拆书空闲','b-dim'));
  let html = `<div class="card" style="border-width:2px"><b>⚡ 一键全流程制作</b>
    <div class="dim" style="margin:4px 0">填好下方内容（或全留空用默认）→ 点此一条龙：拆书入库（可选）→ 抓热点热梗 → 转换 → 写作门禁（自动检索知识库）→ 发布 → 数据回收 → 复盘改进</div>
    <div class="row" style="align-items:center">
      <label class="sw"><input type="checkbox" id="oc_decon"> 📚 先拆解 inbox 全部 txt 入库</label>
      <label class="sw"><input type="checkbox" id="oc_runall" checked> 完成后自动刷新总览</label>
    </div>
    <button class="btn" style="width:100%;padding:10px" onclick="oneClickGen()">⚡ 一键全流程制作</button>
    <div class="dim" id="ocMsg" style="margin-top:6px"></div></div>
    <h2>生成小说</h2><div class="card">
    <div class="row" style="align-items:end">
      <div style="flex:2">核心梗<br><input class="f" id="g_idea" placeholder="先勾选右边「用此核心梗」才会生效；不勾=每次从热点随机出新题"></div>
      <div style="width:150px">题材方向（可空）<br><input class="f" id="g_dir" placeholder="追妻火葬场"></div>
      <div style="width:170px"><label class="sw"><input type="checkbox" id="g_useidea"> 用此核心梗（不勾=热点随机）</label></div>
    </div>
    <div class="row" style="margin-top:8px;align-items:end">
      <div style="width:140px">热点来源（热点模式）<br><input class="f" id="g_srcs" placeholder="zhihu,baidu,douyin"></div>
      <div style="width:110px">篇数<br><input class="f" id="g_count" value="1"></div>
      <div style="width:120px">单篇字数<br><input class="f" id="g_words" placeholder="默认"></div>
      <div style="width:120px">平台<br><input class="f" id="g_plat" placeholder="zhihu"></div>
      <div style="width:120px">提供方<br><select id="g_prov" class="f"><option value="">跟随配置</option><option value="mock">mock</option><option value="local">local</option><option value="api">api</option></select></div>
      <div><button class="btn" onclick="startGen()">🚀 开始生成（后台）</button></div>
    </div>
    <div class="row" style="margin-top:6px">
      <label class="sw"><input type="checkbox" id="g_autopub" checked> 自动发布</label>
      <select id="g_pubmode" class="f" style="max-width:220px">
        <option value="manual">投稿包（手动粘贴）</option>
        <option value="fanqie_draft">番茄草稿箱（浏览器自动化）</option>
        <option value="mock">mock（测试）</option>
      </select>
      <label class="sw"><input type="checkbox" id="g_autodata" checked> 自动回收数据</label>
      <label class="sw"><input type="checkbox" id="g_autoimp" checked> 自动复盘改进</label>
    </div>
    <div class="dim" style="margin-top:6px">勾选后走全流程制作（写→发→数据→复盘一条龙）。番茄草稿箱需先：pip install playwright → python -m studio.fanqie_draft login，详见 docs/fanqie_publish.md。</div>
    <div id="genList" style="margin-top:10px">${genJobsHtml(genjobs.jobs||[])}</div></div>`;

  html += `<h2>拆书入库</h2><div class="card">
    <div>${deconLine}</div>
    <div class="dim" style="margin:6px 0">两种方式入库：① 本页直接导入 txt（可多选批量）；
    ② 把 txt 放入 <b>workspace/inbox/</b> 目录后刷新（整本书或单篇皆可，自动分段）。
    拆出的钩子/爽点/技法/金句自动进素材库并进入 RAG 索引。</div>
    <div style="margin:6px 0">
      <input type="file" id="deconFiles" multiple accept=".txt" style="display:none" onchange="deconUpload(this.files)">
      <button class="btn" onclick="document.getElementById('deconFiles').click()">📂 导入 txt（可多选，导入后自动批量拆解）</button>
    </div>
    <div>${(inbox.files||[]).map(f=>`<div style="margin:4px 0">${badge('txt','b-dim')} ${H(f.name)}
      <span class="dim">（${Math.round(f.size/1024)}KB）</span>
      <button class="btn gray" style="padding:2px 10px" onclick="deconOne('${H(f.name)}')">拆解</button></div>`).join('') || '<span class="dim">inbox 为空：导入文件或放入 workspace/inbox/ 后刷新</span>'}</div>
    ${(inbox.files||[]).length?`<button class="btn" onclick="deconBatch()">📚 批量拆书（全部 txt）</button>`:''}
    <div class="dim" id="deconMsg" style="margin-top:6px"></div></div>`;

  html += `<h2>知识检索（RAG）</h2><div class="card">
    <div class="row"><div style="flex:1"><input class="f" id="ragQ" placeholder="如：婆婆 陪嫁房 反转 钱数细节" value=""></div>
    <div><button class="btn gray" onclick="ragSearch()">检索</button></div></div>
    <div id="ragOut" style="margin-top:8px"></div></div>`;
  document.getElementById('view').innerHTML = html;
}
window.renderCreate = renderCreate;
function genJobsHtml(jobs){
  if(!jobs.length) return '<span class="dim">暂无创作记录</span>';
  return jobs.slice(-30).reverse().map(j=>{
    const run = (j.history||[]).slice(-1)[0];
    const stat = j.running ? '进行中'
      : (run ? `${run.results.filter(r=>r.code===0).length}/${run.results.length} 篇成功` : '进行中…');
    const statBadge = j.running ? badge('🟢 '+stat,'b-warn') : badge(stat,'b-acc');
    return `<div class="card" style="margin-bottom:6px"><b>${H(j.name)}</b> ${statBadge}
      <a style="float:right;color:var(--bad)" onclick="genJobDel('${H(j.id)}')">删除</a>
      <div class="dim">题材：${H((j.directions||[]).join('、')||'跟随热点')} ｜ ${H(schNext(j))} ｜ 上次：${H(j.last_run||'')}</div></div>`;
  }).join('');
}
window.genJobsHtml = genJobsHtml;
async function genJobDel(id){
  if(!confirm('删除该条创作记录？只删除任务记录，已生成的项目与成稿不受影响。')) return;
  try{
    const res = await postJSON('/api/schedules/delete', {id});
    if(!res.ok) throw new Error(res.error || '删除失败');
    renderCreate();
  }catch(e){ alert('删除失败：' + e.message); }
}
window.genJobDel = genJobDel;
async function startGen(){
  const useIdea = document.getElementById('g_useidea')?.checked;
  const idea = useIdea ? document.getElementById('g_idea').value.trim() : '';
  if(useIdea && !idea){ alert('已勾选「用此核心梗」但输入为空'); return; }
  const srcs = document.getElementById('g_srcs').value.trim() || (idea ? '' : 'zhihu,baidu,douyin,toutiao');
  if(!idea && !srcs){ alert('请填核心梗或热点来源'); return; }
  try{
    const res = await postJSON('/api/generate', {
      mode: idea ? 'idea' : 'hot', idea,
      direction: document.getElementById('g_dir').value.trim(),
      sources: srcs, count: parseInt(document.getElementById('g_count').value)||1,
      words: parseInt(document.getElementById('g_words').value)||null,
      platform: document.getElementById('g_plat').value.trim(),
      provider: document.getElementById('g_prov').value,
      auto_publish: document.getElementById('g_autopub').checked,
      publish_mode: document.getElementById('g_pubmode')?.value || 'manual',
      auto_data: document.getElementById('g_autodata').checked,
      auto_improve: document.getElementById('g_autoimp').checked});
    alert('已后台启动（任务 '+res.job_id+'），完成后在总览/素材库查看');
    await renderCreate();
  }catch(e){ alert('失败：'+e.message); }
}
window.startGen = startGen;
async function oneClickGen(){
  const doDecon = document.getElementById('oc_decon')?.checked;
  if(!confirm('一条龙：' + (doDecon ? '拆书入库 → ' : '') + '抓热点热梗→转换→写作门禁→发布→数据回收→复盘改进。\n默认 1 篇、mock 数据源（可在表单里改）。开始？')) return;
  const msg = document.getElementById('ocMsg');
  try{
    if(doDecon){
      const inbox = await (await fetch('/api/inbox')).json();
      const files = inbox.files||[];
      if(files.length){
        for(let i=0;i<files.length;i++){
          msg.textContent = `拆书入库 ${i+1}/${files.length}：${files[i].name} …`;
          await postJSON('/api/deconstruct/run', {file: files[i].name});
          while((await (await fetch('/api/inbox')).json()).decon?.running){
            await new Promise(r=>setTimeout(r,1500));
          }
        }
        msg.textContent = `拆书完成（${files.length} 个文件已入库），启动写作…`;
      } else {
        msg.textContent = 'inbox 无 txt，跳过拆书，启动写作…';
      }
    } else {
      msg.textContent = '已启动，后台制作中…完成后在总览/素材库/运行日志查看';
    }
    const idea = document.getElementById('g_useidea')?.checked
      ? (document.getElementById('g_idea')?.value.trim() || '') : '';
    const res = await postJSON('/api/generate', {
      idea: idea, sources: 'zhihu,baidu,douyin,toutiao',
      count: parseInt(document.getElementById('g_count')?.value)||1,
      words: parseInt(document.getElementById('g_words')?.value)||null,
      platform: document.getElementById('g_plat')?.value.trim()||'zhihu',
      provider: document.getElementById('g_prov')?.value||'',
      auto_publish: true,
      publish_mode: document.getElementById('g_pubmode')?.value || 'manual',
      auto_data: true, auto_improve: true});
    msg.textContent = '已启动（任务 ' + res.job_id + '）' + (doDecon ? '｜拆书已完成入库' : '');
  }catch(e){ msg.textContent = '启动失败：' + e.message; }
}
window.oneClickGen = oneClickGen;
async function deconOne(name){
  try{
    await postJSON('/api/deconstruct/run', {file: name});
    pollDecon();
  }catch(e){ alert(e.message); }
}
window.deconOne = deconOne;
async function deconUpload(fileList){
  const files = [...(fileList||[])].filter(f => f.size > 0);
  if(!files.length) return;
  const msg = document.getElementById('deconMsg');
  try{
    msg.textContent = `导入中（0/${files.length}）…`;
    const payload = [];
    for(const f of files){
      const dataUrl = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(String(r.result));
        r.onerror = () => rej(r.error || new Error('读取失败'));
        r.readAsDataURL(f);
      });
      payload.push({name: f.name, data: dataUrl.split(',')[1] || ''});
    }
    const res = await postJSON('/api/inbox/upload', {files: payload});
    msg.textContent = `已导入 ${(res.saved||[]).length} 个文件` +
      ((res.skipped||[]).length ? `，跳过 ${res.skipped.length} 个（空文件）` : '') +
      '，开始批量拆解…';
    await renderCreate();
    await deconBatch(true);
  }catch(e){ msg.textContent = '导入失败：' + e.message; }
}
window.deconUpload = deconUpload;
async function deconBatch(silent){
  if(!silent && !confirm('批量拆解 inbox 全部 txt？拆解会调用模型逐段分析。')) return;
  const inbox = await (await fetch('/api/inbox')).json();
  for(const f of (inbox.files||[])){
    try{ await postJSON('/api/deconstruct/run', {file: f.name});
      while((await (await fetch('/api/inbox')).json()).decon.running){ await new Promise(r=>setTimeout(r,1500)); }
    }catch(e){ break; }
  }
  await renderCreate();
}
window.deconBatch = deconBatch;
function pollDecon(){
  const t = setInterval(async ()=>{
    const inbox = await (await fetch('/api/inbox')).json();
    const d = inbox.decon||{};
    const el = document.getElementById('deconMsg');
    if(el) el.textContent = d.running ? `拆书中：${d.current}（${d.done}/${d.total||'?'} 段）` : (d.error? '出错：'+d.error : '完成 ✔ 已进素材库与 RAG 索引');
    if(!d.running){ clearInterval(t); await renderCreate(); }
  }, 1500);
}
async function ragSearch(){
  const q = document.getElementById('ragQ').value.trim();
  if(!q) return;
  const data = await (await fetch('/api/rag?q='+encodeURIComponent(q)+'&top=8')).json();
  document.getElementById('ragOut').innerHTML = (data.items||[]).map(h=>
    `<div class="card" style="margin-bottom:6px">${badge(h.source,'b-dim')} ${badge(h.type,'b-acc')}
      ${h.direction?badge(h.direction,'b-good'):''} <span class="dim">${h.score}</span>
      <div style="margin-top:4px">${H(h.text)}</div></div>`).join('') || '<span class="dim">无结果</span>';
}
window.ragSearch = ragSearch;

/* ---------- 运行日志 ---------- */
async function renderRunlogs(){
  const data = await (await fetch('/api/runlogs')).json();
  const runs = data.runs||[];
  document.getElementById('view').innerHTML = `
    <h2>运行日志</h2>
    <div class="dim" style="margin-bottom:8px">每次运行自动记录：全模块阶段计时、LLM 调用、错误与全量控制台输出（workspace/logs/）</div>
    <div class="card">${runs.length ? `<table><tr><th>时间</th><th>命令</th><th>耗时</th><th>阶段</th><th>LLM</th><th>错误</th><th></th></tr>` +
      runs.map((r,i)=>`<tr><td>${H(r.at)}</td><td>${H((r.cmd||[]).join(' ')).slice(0,60)}</td>
        <td>${r.duration_s}s</td><td>${(r.stages||[]).length}</td><td>${r.llm_calls} 次 / ${r.tokens_out} tok</td>
        <td>${r.errors?badge(r.errors+' 次','b-bad'):'0'}</td>
        <td><a onclick="viewRunlog('${H(r.log_file)}')">查看</a></td></tr>`).join('') + `</table>`
      : '<span class="dim">暂无运行记录</span>'}</div>
    <div class="card" id="rlView" style="display:none">
      <div class="dim" id="rlFile"></div>
      <pre class="ed" id="rlText" style="min-height:420px;white-space:pre-wrap"></pre></div>`;
}
window.renderRunlogs = renderRunlogs;
async function viewRunlog(file){
  const r = await fetch('/api/runlog?name='+encodeURIComponent(file.split(/[\\\\/]/).pop()));
  if(!r.ok){ alert('日志不存在'); return; }
  const d = await r.json();
  document.getElementById('rlView').style.display = 'block';
  document.getElementById('rlFile').textContent = file;
  document.getElementById('rlText').textContent = d.content;
  document.getElementById('rlView').scrollIntoView({behavior:'smooth'});
}
window.viewRunlog = viewRunlog;

/* ---------- 定时任务 ---------- */
async function renderSched(){
  const [cfg, data] = await Promise.all([
    fetch('/api/config').then(r=>r.json()),
    fetch('/api/schedules').then(r=>r.json())]);
  const dirs = (await fetch('/api/agents').then(r=>r.json()), (cfg.llm, null));
  const tropeDirs = (await fetch('/api/tropes').then(r=>r.json()).catch(()=>({names:[]})));
  const alive = data.scheduler_alive;
  document.getElementById('view').innerHTML = `
    <h2>定时任务</h2>
    <div class="card"><b>调度器状态</b>：
      ${alive ? badge('运行中','b-good') : badge('未运行','b-dim')}
      <span class="dim">（启动：python -m studio scheduler，常驻每 60s 检查）</span>
      ${data.last_heartbeat ? `<span class="dim">心跳：${H(data.last_heartbeat)}</span>` : ''}</div>
    <div id="jobList"></div>
    <div class="card"><b>＋ 新建任务</b>
      <div class="row" style="margin-top:8px">
        <div style="width:200px">名称<br><input class="f" id="j_name" placeholder="如：晚间档追妻文"></div>
        <div style="width:150px">触发方式<br><select id="j_ttype" class="f"><option value="daily">每日定时</option><option value="interval">间隔分钟</option></select></div>
        <div style="width:120px">时间/分钟<br><input class="f" id="j_tval" placeholder="20:00 或 180"></div>
        <div style="width:80px">篇数<br><input class="f" id="j_count" value="1"></div>
        <div style="width:130px">单篇字数<br><input class="f" id="j_words" placeholder="默认"></div>
        <div style="width:130px">平台<br><input class="f" id="j_platform" placeholder="zhihu"></div>
      </div>
      <div class="row" style="margin-top:8px">
        <div style="flex:1">题材列表（逗号分隔，多篇轮换；留空=跟随热点）<br>
          <input class="f" id="j_dirs" placeholder="追妻火葬场,彩礼纠纷,婆媳大战"></div>
        <div style="flex:1">热点来源（留空=用配置默认）<br>
          <input class="f" id="j_srcs" placeholder="zhihu,baidu,douyin,toutiao"></div>
        <div style="width:120px">生成提供方<br><select id="j_prov" class="f"><option value="">跟随配置</option><option value="mock">mock</option><option value="local">local</option><option value="api">api</option></select></div>
      </div><br>
      <button class="btn" onclick="addJob()">创建任务</button> <span class="dim" id="jMsg"></span></div>`;
  document.getElementById('jobList').innerHTML = (data.jobs||[]).map(j=>`
    <div class="card">
      <div><b>${H(j.name)}</b> ${badge(schNext(j), 'b-acc')} ${badge(j.enabled?'启用':'停用', j.enabled?'b-good':'b-dim')}
        <a style="float:right;color:var(--bad)" onclick="jobDel('${H(j.id)}')">删除</a></div>
      <div class="dim" style="margin-top:4px">
        篇数：${j.count} ｜ 字数：${j.words||'默认'} ｜ 平台：${j.platform||'默认'} ｜
        题材：${H((j.directions||[]).join('、')||'跟随热点')} ｜ 来源：${H((j.sources||[]).join(',')||'默认')} ｜
        提供方：${j.provider||'跟随配置'}</div>
      <div class="dim" style="margin-top:3px">上次：${H(j.last_run||'从未')} ${H(j.last_result||'')}</div>
      <div style="margin-top:6px">
        <button class="btn" onclick="jobRun('${H(j.id)}')">立即运行</button>
        <button class="btn gray" onclick="jobToggle('${H(j.id)}')">${j.enabled?'停用':'启用'}</button></div>
    </div>`).join('') || '<div class="dim">暂无任务，用下方表单创建</div>';
}
window.renderSched = renderSched;
function schNext(j){
  const t = j.trigger||{};
  return t.type==='daily' ? `每日 ${t.time}` : `每 ${t.minutes} 分钟`;
}
window.schNext = schNext;
async function addJob(){
  const ttype = document.getElementById('j_ttype').value;
  const tval = document.getElementById('j_tval').value.trim();
  const trigger = ttype==='daily' ? {type:'daily', time: tval||'20:00'} : {type:'interval', minutes: parseInt(tval)||180};
  try{
    await postJSON('/api/schedules/save', {job: {
      name: document.getElementById('j_name').value || '定时撰写',
      trigger, count: parseInt(document.getElementById('j_count').value)||1,
      directions: document.getElementById('j_dirs').value.split(',').map(x=>x.trim()).filter(Boolean),
      words: parseInt(document.getElementById('j_words').value)||null,
      platform: document.getElementById('j_platform').value.trim(),
      sources: document.getElementById('j_srcs').value.split(',').map(x=>x.trim()).filter(Boolean),
      provider: document.getElementById('j_prov').value }});
    document.getElementById('jMsg').textContent = '已创建 ✔';
    await renderSched();
  }catch(e){ document.getElementById('jMsg').textContent = '失败：'+e.message; }
}
window.addJob = addJob;
async function jobRun(id){
  await postJSON('/api/schedules/run', {id});
  document.getElementById('matMsg')?.remove;
  alert('已后台启动，日志见 workspace_logs/');
  await renderSched();
}
window.jobRun = jobRun;
async function jobToggle(id){
  await postJSON('/api/schedules/toggle', {id});
  await renderSched();
}
window.jobToggle = jobToggle;
async function jobDel(id){
  if(!confirm('删除该定时任务？')) return;
  await postJSON('/api/schedules/delete', {id});
  await renderSched();
}
window.jobDel = jobDel;

/* ---------- 素材库 ---------- */
const MAT_TYPES = ['标题','导语','开头钩子','章末钩子','核心梗','反转设计','伏笔设计','人设卡','情绪爆点','修改教训','热点素材','金句','写作技法'];
window.projDel = async function(name){
  if(!confirm(`彻底删除项目「${name}」？成稿/章节/投稿包/阅读数据将一并删除，不可恢复。`)) return;
  try{
    await postJSON('/api/project/delete', {name});
    alert('已删除');
    await route('');
  }catch(e){ alert('删除失败：' + e.message); }
};
const Q_BADGE = {high:['高质量','b-good'], mid:['可用','b-acc'], low:['低质','b-bad']};
let matView = 'main';
async function renderMat(){
  document.getElementById('view').innerHTML = `
    <h2>素材库</h2>
    <div class="card">
      <div class="row" style="align-items:end">
        <div style="width:150px">类型<br><select id="matType" class="f"><option value="">全部类型</option>${MAT_TYPES.map(t=>`<option>${t}</option>`).join('')}</select></div>
        <div style="width:170px">题材<br><input id="matDir" class="f" placeholder="可模糊"></div>
        <div style="flex:1">关键词<br><input id="matQ" class="f" placeholder="搜索"></div>
        <div><button class="btn gray" onclick="loadMat()">筛选</button></div>
        <div><button class="btn" id="matCurBtn" onclick="matCurate()">✨ AI 整理</button></div>
        <div><button class="btn gray" onclick="matToggleTrash()" id="matTrashBtn">回收站</button></div>
        <div><button class="btn gray" onclick="matToggleMemory()" id="matMemBtn">🧠 记忆库</button></div>
        <div><button class="btn gray" onclick="matToggleKb()" id="matKbBtn">📚 知识结构</button></div>
        <div><button class="btn gray" onclick="matAddShow()">＋ 添加</button></div>
      </div>
      <div class="dim" id="matStats" style="margin-top:8px"></div>
    </div>
    <div class="card" id="matAdd" style="display:none">
      <div class="row">
        <select id="matAddType" class="f" style="max-width:130px">${MAT_TYPES.map(t=>`<option>${t}</option>`).join('')}</select>
        <input id="matAddDir" class="f" style="max-width:150px" placeholder="题材">
        <input id="matAddText" class="f" style="flex:1" placeholder="素材内容">
      </div><br><button class="btn" onclick="matAddSave()">入库</button> <span class="dim" id="matAddMsg"></span></div>
    <div class="dim" id="matMsg" style="margin-bottom:8px"></div>
    <div id="matList"></div>`;
  await loadMat();
}
window.renderMat = renderMat;
async function loadMat(){
  const t = document.getElementById('matType')?.value || '';
  const d = document.getElementById('matDir')?.value || '';
  const q = document.getElementById('matQ')?.value || '';
  const tv = matView === 'trash' ? '&view=trash' : '';
  const data = await (await fetch(`/api/materials?type=${encodeURIComponent(t)}&direction=${encodeURIComponent(d)}&q=${encodeURIComponent(q)}${tv}`)).json();
  const st = data.stats||{};
  if(matView !== 'trash'){
    const bq = st.by_quality||{};
    document.getElementById('matStats').textContent =
      `共 ${st.total} 条（已整理 ${st.curated||0}）｜ 高质 ${bq.high||0} · 可用 ${bq.mid||0} · 低质 ${bq.low||0} ｜ ` +
      Object.entries(st.by_type||{}).map(([k,v])=>k+' '+v).join(' · ');
  } else {
    document.getElementById('matStats').textContent = `回收站 ${(data.items||[]).length} 条（可恢复）`;
  }
  document.getElementById('matList').innerHTML = (data.items||[]).map(x=>{
    const q = x.quality || '未整理';
    const qinfo = Q_BADGE[q];
    const qHtml = qinfo ? badge(qinfo[0], qinfo[1]) : badge('未整理','b-dim');
    if(matView === 'trash'){
      return `<div class="card" style="margin-bottom:8px;opacity:.75">
        <div>${badge(x.type,'b-dim')} ${badge(x.direction,'b-dim')} ${badge('低质已删','b-bad')}
          <a style="float:right" onclick="matRestore('${H(x.id)}')">恢复</a></div>
        <div style="margin-top:4px">${H(x.text)}</div>
        <div class="dim">${H(x.note||'')} · ${H(x.created_at||'')}</div></div>`;
    }
    return `<div class="card" style="margin-bottom:8px">
      <div>${badge(x.type,'b-acc')} ${badge(x.direction,'b-good')} ${qHtml} ${(x.tags||[]).map(t=>badge(t,'b-dim')).join(' ')}
        ${x.score?`<span class="dim">${x.score}分</span>`:''}
        <a style="float:right;color:var(--bad)" onclick="matDel('${H(x.id)}')">删除</a></div>
      <div style="margin-top:5px">${H(x.text)}</div>
      ${x.note?`<div class="dim" style="margin-top:3px">整理意见：${H(x.note)}</div>`:''}
      <div class="dim" style="margin-top:3px">${H(x.source_project||'')} · ${H(x.created_at||'')}</div></div>`;
  }).join('') || (matView==='trash' ? '<div class="dim">回收站为空</div>' : '<div class="dim">暂无素材（成稿后自动沉淀，或手动添加）</div>');
}
window.loadMat = loadMat;
async function matCurate(){
  const btn = document.getElementById('matCurBtn');
  const msg = document.getElementById('matMsg');
  btn.disabled = true; btn.textContent = '整理中…（评估+纠偏+删低质）';
  msg.textContent = '';
  try{
    const d = document.getElementById('matDir')?.value || '';
    const res = await postJSON('/api/materials/curate', {direction: d});
    msg.textContent = `整理完成：评估 ${res.checked} 条，删除低质 ${res.removed} 条，` +
      `高质 ${res.high||0} / 可用 ${res.mid||0}，分类纠偏 ${res.corrected||0} 处`;
    await loadMat();
  }catch(e){ msg.textContent = '整理失败：' + e.message; }
  btn.disabled = false; btn.textContent = '✨ AI 整理';
}
window.matCurate = matCurate;
function matToggleTrash(){
  matView = matView === 'trash' ? 'main' : 'trash';
  document.getElementById('matTrashBtn').textContent = matView==='trash' ? '← 返回素材' : '回收站';
  loadMat();
}
window.matToggleTrash = matToggleTrash;
/* 记忆库视图：VCP 式冷热分层（L1 工作记忆/L2 短期/L3 长期/L4 冷归档） */
let memView = false;
function matToggleMemory(){
  memView = !memView;
  document.getElementById('matMemBtn').textContent = memView ? '← 返回素材' : '🧠 记忆库';
  memView ? loadMemory() : loadMat();
}
window.matToggleMemory = matToggleMemory;
const MEM_LAYER_DESC = {
  L1:['工作记忆','高质量且近期被复用，写作时优先注入'],
  L2:['短期记忆','近期沉淀、尚未强化，观察期'],
  L3:['长期记忆','高质量但使用少，按相关性检索'],
  L4:['冷归档','低质或长期零复用，整理时可沉降入回收站']};
async function loadMemory(){
  const data = await (await fetch('/api/materials?view=memory')).json();
  const el = document.getElementById('matList');
  document.getElementById('matStats').textContent = '记忆引力：命中即强化（usage_count++），冷置衰减（heat×0.85/30天）。写作时 BM25 种子 + 标签河道联想自动注入。';
  el.innerHTML = Object.entries(MEM_LAYER_DESC).map(([k,[name,desc]])=>{
    const its = (data.layers||{})[k]||[];
    return `<div class="card"><b>${k} ${name}</b> <span class="dim">${desc} · ${its.length} 条（最多显示12）</span>` +
      (k==='L4'&&its.length?` <a onclick="memConsolidate()">整理沉降</a>`:'') +
      `<div style="margin-top:6px">` + (its.map(x=>
        `<div style="border-top:1px dashed var(--line);padding:4px 0">
          ${badge(x.type||'','b-dim')} ${badge(x.direction||'','b-dim')} ${badge('热'+(x.heat||0),'b-acc')}
          <span style="margin-left:4px">${H(x.text)}</span></div>`).join('') || '<span class="dim">空</span>') +
      `</div></div>`;
  }).join('');
}
window.loadMemory = loadMemory;
/* 知识结构视图：向量库/弧位分布 + 联想测试 */
let kbView = false;
function matToggleKb(){
  kbView = !kbView;
  document.getElementById('matKbBtn').textContent = kbView ? '← 返回素材' : '📚 知识结构';
  kbView ? loadKbView() : loadMat();
}
window.matToggleKb = matToggleKb;
async function loadKbView(){
  const st = await (await fetch('/api/materials?view=kbstats')).json();
  document.getElementById('matStats').textContent =
    `知识单元 ${st.units} 条 | 向量索引 ${st.vectors_indexed} | 共现节点 ${st.coco_nodes} ｜ 弧位 ` +
    Object.entries(st.by_arc||{}).map(([k,v])=>k+' '+v).join(' · ');
  const d = st.decon || {};
  const pct = d.total ? Math.min(100, Math.round(d.done / d.total * 100)) : 0;
  const laya = st.laya || {};
  const ls = laya.stats || {};
  const layaTxt = !laya.enabled ? '未启用'
    : (laya.ready
        ? `就绪（${laya.checkpoint} / ${laya.device}）｜本进程判重：候选 ${ls.cands||0} 个 → 杀重复 ${ls.dup||0} · 放行 ${ls.new||0} · 交主模型 ${ls.unsure||0}（省 ${ls.llm_saved||0} 次大模型调用）`
        : `预热中（${laya.checkpoint} / ${laya.device}，约半分钟）…`);
  const dd = st.dedup || {};
  const ddTxt = dd.running ? '⏳ 后台去重进行中…'
    : (dd.error ? `⚠️ 上次去重失败：${dd.error}`
    : (dd.result ? `上次去重：剔除 ${dd.result.drop_total ?? dd.result.dropped ?? 0} 条（${dd.result.at || ''}）`
    : '尚未运行'));
  const nf = st.novel_fetch || {};
  const nfTxt = nf.running ? '⏳ 抓取+深拆进行中…'
    : (nf.error ? `⚠️ 上次抓取失败：${nf.error}`
    : (nf.result && nf.result.books ? `上次抓取（${nf.result.source}）：《${(nf.result.books||[]).map(b=>b.title).join('》《')}》`
    : '尚未运行'));
  const nfOpts = (st.novel_sources || []).map(s => `<option>${s}</option>`).join('') || '<option>（config 未配置源）</option>';
  document.getElementById('matList').innerHTML = `
    <div class="card"><b>⚙️ 批量深拆（onehu 故事 → 弧位标注知识）</b>
      <div class="dim" style="margin-top:4px">${d.done}/${d.total} 篇 ｜ 失败待补试 ${d.failed} ｜ ${d.running ? '🟢 运行中' : '⚪ 已暂停'}</div>
      <div style="background:var(--card2);border-radius:6px;height:10px;margin-top:6px;overflow:hidden">
        <div style="background:var(--ink);height:100%;width:${pct}%"></div></div>
    </div>
    <div class="card" style="margin-top:10px"><b>🧹 知识库去重</b>
      <div class="dim" style="margin-top:4px">同文/同开头/语义近重复（cos≥0.93）三重清理，入库时自动防重。${ddTxt}</div>
      <div class="row" style="margin-top:6px">
        <button class="btn" onclick="kbDedup(true)">全面去重（含语义扫描，约20分钟）</button>
        <button class="btn" onclick="kbDedup(false)">快速压实（同文/同开头，秒级）</button>
      </div>
      <div class="dim" id="kbDedupOut" style="margin-top:6px"></div>
    </div>
    <div class="card" style="margin-top:10px"><b>📥 热门小说抓取（榜单→下载→自动深拆入库）</b>
      <div class="dim" style="margin-top:4px">源可在 config.yaml 的 novel_fetch.sources 自定义增删。${nfTxt}</div>
      <div class="row" style="margin-top:6px">
        <div style="flex:1"><select id="nfSrc" class="f">${nfOpts}</select></div>
        <div style="width:80px"><select id="nfTop" class="f"><option value="1">1 本</option><option value="2" selected>2 本</option><option value="3">3 本</option></select></div>
        <div><button class="btn" onclick="novelFetch()">抓取并深拆</button></div>
      </div>
      <div class="dim" id="nfOut" style="margin-top:6px"></div>
    </div>
    <div class="card" style="margin-top:10px"><b>⚡ Laya 判别器（选题查重的常驻第一道语义裁判）</b>
      <div class="dim" style="margin-top:4px">状态：${layaTxt} ｜ 用途：选题查重三层裁判的第一层，低置信度才升级主模型仲裁。</div>
    </div>
    <div class="card" style="margin-top:10px"><b>🧩 知识单元（写作时自动检索的形态）</b>
      <div class="dim" style="margin-top:4px">拆解抽取的钩子/技法/金句/弧段+素材，全部已向量化。逐章写作按弧位自动召回，这里可人工检索验证。</div>
      <div class="row" style="margin-top:6px">
        <div style="width:130px"><select id="kuKind" class="f" onchange="kbUnitsReset()"><option value="">全部类型</option><option>hook</option><option>arc_segment</option><option>technique</option><option>quote</option><option>lead</option><option>material</option></select></div>
        <div style="width:90px"><select id="kuArc" class="f" onchange="kbUnitsReset()"><option value="">全部弧位</option><option>起</option><option>承</option><option>转</option><option>合</option></select></div>
        <div style="width:110px"><select id="kuSrc" class="f" onchange="kbUnitsReset()"><option value="">全部来源</option><option value="拆书:">拆书</option><option value="素材:">素材沉淀</option><option value="拆书精选">拆书精选</option><option value="onehu">onehu导入</option></select></div>
        <div style="flex:1"><input class="f" id="kuQ" placeholder="搜文本/来源，回车检索" onkeydown="if(event.key==='Enter')kbUnitsReset()"></div>
        <div><button class="btn" onclick="kbUnitsReset()">检索</button></div>
      </div>
      <div class="dim" id="kuTotal" style="margin-top:6px"></div>
      <div id="kuList" style="margin-top:4px"></div>
      <div id="kuMore" style="margin-top:4px"></div>
    </div>
    <div class="card" style="margin-top:10px"><b>📖 拆解文档（弧位标注的完整拆书结果）</b>
      <div class="dim" style="margin-top:4px">每篇 = 分段目标/钩子/冲突/技法/金句 + 起承转合弧位。拆出的钩子/技法/金句已作为知识单元入库供写作调用。</div>
      <div id="deconList" style="margin-top:6px"><span class="dim">加载中…</span></div>
      <div id="deconDoc" style="margin-top:8px;white-space:pre-wrap"></div>
    </div>
    <div class="card" style="margin-top:10px"><b>🔍 联想检索测试（BM25+向量+共现扩散 三路融合）</b>
    <div class="row" style="margin-top:6px">
      <div style="flex:1"><input class="f" id="kbQ" placeholder="如：婆婆藏了赔偿款怎么办"></div>
      <div style="width:110px"><select id="kbArc" class="f"><option value="">弧位</option><option>起</option><option>承</option><option>转</option><option>合</option></select></div>
      <div><button class="btn" onclick="kbRecallTest()">联想</button></div>
    </div>
    <div class="dim" id="kbOut" style="margin-top:8px;white-space:pre-wrap"></div></div>`;
  // 必须在 innerHTML 注入之后再触发子列表加载（#deconList/#kuList 此时才存在）
  loadDeconList();
  kbUnitsReset();
}
window.loadKbView = loadKbView;
async function novelFetch(){
  const out = document.getElementById('nfOut');
  out.textContent = '启动中…';
  try{
    const res = await postJSON('/api/novel-fetch', {
      source: document.getElementById('nfSrc').value,
      top: parseInt(document.getElementById('nfTop').value || '2', 10)
    });
    if (res.ok){ out.textContent = '✅ ' + (res.note || '已启动') + '（深拆每本约1-3分钟）'; }
    else out.textContent = '❌ ' + (res.error || '启动失败');
  }catch(e){ out.textContent = '失败：' + e.message; }
}
async function kbDedup(semantic){
  const out = document.getElementById('kbDedupOut');
  out.textContent = '启动中…';
  try{
    const res = await postJSON('/api/kb/dedup', { semantic: !!semantic, threshold: 0.93 });
    if (res.ok){ out.textContent = semantic
      ? '✅ 已启动后台去重（语义扫描约20分钟），页面稍后刷新查看结果。'
      : '✅ 快速压实已完成或已启动，刷新页面查看统计。'; loadKbView(); }
    else out.textContent = '❌ ' + (res.error || '启动失败');
  }catch(e){ out.textContent = '失败：' + e.message; }
}
window.kbDedup = kbDedup;
async function loadDeconList(){
  try{
    const res = await (await fetch('/api/decon-list')).json();
    const el = document.getElementById('deconList');
    if(!el) return;   // 视图尚未渲染,防御
    if(!res.docs?.length){ el.innerHTML = '<span class="dim">暂无拆解文档</span>'; return; }
    el.innerHTML = `<div class="dim">共 ${res.total} 篇，最近 ${res.docs.length} 篇：</div>` +
      res.docs.slice(0,12).map(d=>`<div style="margin:3px 0">
        <a onclick="showDeconDoc('${encodeURIComponent(d.file)}')" style="cursor:pointer">${d.sections}段 · ${H(d.title)}</a>
        <span class="dim"> ${H(d.direction||'')}</span></div>`).join('') +
      (res.total>12?`<div class="dim">…其余 ${res.total-12} 篇（每篇拆完即自动入库）</div>`:'');
  }catch(e){ document.getElementById('deconList').innerHTML = '加载失败：'+e.message; }
}
window.loadDeconList = loadDeconList;
let kuOffset = 0;
function kuEsc(t){ return H(t||''); }
async function kbUnitsLoad(reset){
  try{
    const kind = document.getElementById('kuKind').value;
    const arc = document.getElementById('kuArc').value;
    const q = document.getElementById('kuQ').value.trim();
    const src = document.getElementById('kuSrc').value;
    const url = `/api/kb-units?kind=${encodeURIComponent(kind)}&arc=${encodeURIComponent(arc)}&src=${encodeURIComponent(src)}&q=${encodeURIComponent(q)}&offset=${kuOffset}&limit=80`;
    const res = await (await fetch(url)).json();
    document.getElementById('kuTotal').textContent = `命中 ${res.total} 条`;
    const KINDCN = {hook:'钩子', arc_segment:'弧段', technique:'技法', quote:'金句', lead:'导语', material:'素材'};
    const html = res.items.map(u=>`<div style="margin:4px 0;padding:4px 8px;background:var(--card2);border-radius:6px">
      <span class="dim">[${(u.arc||'·')+'·'+(KINDCN[u.kind]||u.kind)}]</span> ${kuEsc(u.text.slice(0,110))}
      <span class="dim" style="float:right">${kuEsc((u.source||'').slice(0,18))}</span></div>`).join('');
    const list = document.getElementById('kuList');
    list.innerHTML = reset ? html : list.innerHTML + html;
    kuOffset += res.items.length;
    document.getElementById('kuMore').innerHTML =
      (kuOffset < res.total) ? `<button class="btn gray" onclick="kbUnitsLoad(false)">加载更多（剩 ${res.total-kuOffset}）</button>` : '';
  }catch(e){ document.getElementById('kuList').textContent = '加载失败：'+e.message; }
}
window.kbUnitsLoad = kbUnitsLoad;
function kbUnitsReset(){ kuOffset = 0; const el = document.getElementById('kuList'); if(!el) return; el.innerHTML=''; kbUnitsLoad(true); }
window.kbUnitsReset = kbUnitsReset;
async function showDeconDoc(fileEnc){
  const el = document.getElementById('deconDoc');
  el.textContent = '加载中…';
  try{
    const d = await (await fetch('/api/decon-doc?file='+fileEnc)).json();
    const arcs = d.arc_map ? ' 弧位分布 '+Object.entries(d.arc_map).map(([k,v])=>k+' '+((v&&v.segs!=null)?v.segs:v)).join(' · ') : '';
    const NL = String.fromCharCode(10);
    let txt = `《${d.title}》 ｜ 方向：${d.direction||'-'} ｜ ${d.sections_total||d.sections?.length||0} 段${arcs}` + NL;
    (d.sections||[]).forEach(s=>{
      txt += NL + `【段落${String(s.no||s.seq||'').padStart(2,'0')}｜${s.arc||'?'}】目标：${s.goal||''}`
           + (s.hook_type ? ` [${s.hook_type}钩]` : '')
           + NL + '  钩子：' + (s.hook||'')
           + (s.emotion ? NL + '  情绪：' + s.emotion : '')
           + (s.escalation ? NL + '  升级：' + s.escalation : '')
           + (s.info_gap ? NL + '  信息差：' + s.info_gap : '')
           + (s.shuang && s.shuang !== '无' ? NL + '  爽点' + (s.shuang_type ? `[${s.shuang_type}]` : '') + '：' + s.shuang : '')
           + (s.nue ? NL + '  虐点：' + s.nue : '')
           + (s.persona ? NL + '  人设：' + s.persona : '')
           + (s.technique ? NL + '  技法：' + s.technique : '')
           + (s.quote ? NL + '  金句：' + s.quote : '');
    });
    el.textContent = txt;
  }catch(e){ el.textContent = '加载失败：'+e.message; }
}
window.showDeconDoc = showDeconDoc;
async function kbRecallTest(){
  const out = document.getElementById('kbOut');
  out.textContent = '检索中…';
  try{
    const res = await postJSON('/api/kb/recall', {
      q: document.getElementById('kbQ').value,
      arc: document.getElementById('kbArc').value });
    out.textContent = (res.block || '（无命中）') + (res.used?.length ? `

[命中 ${res.used.length} 条]` : '');
  }catch(e){ out.textContent = '失败：' + e.message; }
}
window.kbRecallTest = kbRecallTest;
async function memConsolidate(){
  if(!confirm('把 L4 冷记忆中的零复用低质素材沉降入回收站？（可从回收站恢复）')) return;
  const res = await postJSON('/api/memory/consolidate', {});
  alert(`整理完成：热度衰减 ${res.decayed||0} 条，沉降 ${res.moved_to_trash||0} 条`);
  loadMemory();
}
window.memConsolidate = memConsolidate;
async function matRestore(id){
  await postJSON('/api/materials/restore', {id});
  await loadMat();
}
window.matRestore = matRestore;
async function matDel(id){
  if(!confirm('删除该素材？')) return;
  await postJSON('/api/materials/delete', {id});
  await loadMat();
}
window.matDel = matDel;

function matAddShow(){ const el = document.getElementById('matAdd'); el.style.display = el.style.display==='none'?'block':'none'; }
window.matAddShow = matAddShow;
async function matAddSave(){
  try{
    await postJSON('/api/materials/add', {
      type: document.getElementById('matAddType').value,
      direction: document.getElementById('matAddDir').value,
      text: document.getElementById('matAddText').value});
    document.getElementById('matAddMsg').textContent = '已入库 ✔';
    document.getElementById('matAddText').value = '';
    await loadMat();
  }catch(e){ document.getElementById('matAddMsg').textContent = '失败：'+e.message; }
}
window.matAddSave = matAddSave;

const ROLE_LABEL = {planner:'策划（提梗/方案/大纲/圣经）', writer:'写手（扩写/终稿）',
                    reviewer:'审查（主编/情绪/体检）', panel:'读者评分团', light:'轻量（知识蒸馏）'};
async function renderConfig(){
  const [cfg, models, sources] = await Promise.all([
    fetch('/api/config').then(r=>r.json()),
    fetch('/api/models').then(r=>r.json()).catch(()=>[]),
    fetch('/api/sources').then(r=>r.json())]);
  const llm = cfg.llm || {};
  const allModels = [...new Set(models.flatMap(m=>m.models||[]).filter(m=>!m.startsWith('(')))];
  const opt = m => `<option value="${H(m)}">${H(m)}</option>`;
  const dl = `<datalist id="modelList">${allModels.map(opt).join('')}</datalist>`;
  let html = `<h2>模型与配置</h2><div class="dim">保存即写入 config.yaml（自动备份 .bak）；角色留空 = 跟随主模型</div>`;
  html += `<div class="card"><b>主模型（接入 AI API：本地 Ollama 或任意 OpenAI 兼容接口）</b>
    <div class="row" style="margin-top:8px">
      <div style="flex:1;min-width:150px">provider<br><input class="f" id="c_provider" value="${H(llm.provider||'')}"></div>
      <div style="flex:2;min-width:260px">base_url<br><input class="f" id="c_base" value="${H(llm.base_url||'')}"></div>
      <div style="flex:1;min-width:180px">model<br><input class="f" id="c_model" list="modelList" value="${H(llm.model||'')}"></div>
      <div style="flex:1;min-width:170px">api_key<br><input class="f" id="c_key" value="${H(llm.api_key||'')}"></div>
      <div style="width:90px">温度<br><input class="f" id="c_temp" value="${H(llm.temperature??0.85)}"></div>
    </div>${dl}</div>`;
  html += `<div class="card"><b>按 Agent 角色分配模型（留空 = 跟随主模型）</b>`;
  for(const role of ['planner','writer','reviewer','panel','light']){
    const rc = (cfg.llm_roles||{})[role] || {};
    html += `<div class="row" style="margin-top:8px;align-items:end">
      <div style="width:260px"><b>${H(ROLE_LABEL[role])}</b><div class="dim">llm_roles.${role}</div></div>
      <div style="flex:2">base_url<br><input class="f role-base" data-role="${role}" value="${H(rc.base_url||'')}"></div>
      <div style="flex:1">model<br><input class="f role-model" data-role="${role}" list="modelList" value="${H(rc.model||'')}"></div></div>`;
  }
  html += `<br><button class="btn" onclick="saveModels()">保存模型配置</button>
      <button class="btn gray" onclick="testModel()">🔌 连通测试</button>
      <span class="dim" id="mMsg"></span><div class="dim" id="testOut" style="margin-top:6px"></div></div>`;
  const enabled = sources.enabled||[];
  html += `<div class="card"><b>热点抓取来源</b><div class="row" style="margin-top:6px">` +
    sources.available.map(s=>`<label class="sw"><input type="checkbox" class="srcck" value="${s}" ${enabled.includes(s)?'checked':''}> ${s}</label>`).join('') +
    `</div><div class="row" style="margin-top:8px"><div style="flex:1">DailyHotApi 地址（可选代理）<br>
      <input class="f" id="dhBase" value="${H(sources.dailyhot_base||'')}"></div></div>
    <br><button class="btn" onclick="saveSources()">保存来源</button><span class="dim" id="sMsg"></span></div>`;
  html += `<div class="card"><b>高级（直接改 config，JSON 增量合并）</b>
    <textarea class="ed" id="rawCfg" style="min-height:160px">${H(JSON.stringify(cfg, null, 2))}</textarea><br>
    <button class="btn gray" onclick="saveRaw()">合并保存</button><span class="dim" id="rMsg"></span></div>`;
  document.getElementById('view').innerHTML = html;
  document.getElementById('saveModelsBtn')?.setAttribute('data-x','1');
}
window.renderConfig = renderConfig;
async function saveModels(){
  const patch = {llm: {
      provider: document.getElementById('c_provider').value,
      base_url: document.getElementById('c_base').value,
      model: document.getElementById('c_model').value,
      api_key: document.getElementById('c_key').value,
      temperature: parseFloat(document.getElementById('c_temp').value)||0.85},
    llm_roles: {}};
  document.querySelectorAll('.role-model').forEach(inp=>{
    const role = inp.dataset.role;
    const base = document.querySelector(`.role-base[data-role='${role}']`).value;
    if(inp.value || base) patch.llm_roles[role] = {base_url: base, model: inp.value};
  });
  try{ await postJSON('/api/config', patch);
    document.getElementById('mMsg').textContent = '已保存 ✔（新配置对下次运行生效）';
  }catch(e){ document.getElementById('mMsg').textContent = '失败：'+e.message; }
}
window.saveModels = saveModels;
async function testModel(){
  const out = document.getElementById('testOut');
  if(out) out.textContent = '测试中…';
  try{
    const res = await postJSON('/api/model-test', {
      base_url: document.getElementById('c_base')?.value || '',
      model: document.getElementById('c_model')?.value || '',
      api_key: document.getElementById('c_key')?.value || ''});
    const t = document.getElementById('testOut');
    if(res.ok) t.textContent = `✓ 连通正常｜模型列表 ${res.models_count} 个｜生成 ${res.latency}s：「${H(res.sample||'')}」`;
    else t.textContent = '✗ ' + H(res.error||'失败');
  }catch(e){ const t = document.getElementById('testOut'); if(t) t.textContent = '✗ ' + e.message; }
}
window.testModel = testModel;
async function saveSources(){
  const enabled = [...document.querySelectorAll('.srcck:checked')].map(x=>x.value);
  try{
    await postJSON('/api/sources', {studio: {sources: enabled, dailyhot_base: document.getElementById('dhBase').value}});
    document.getElementById('sMsg').textContent = '已保存 ✔';
  }catch(e){ document.getElementById('sMsg').textContent = '失败：'+e.message; }
}
window.saveSources = saveSources;
async function saveRaw(){
  try{
    const patch = JSON.parse(document.getElementById('rawCfg').value);
    await postJSON('/api/config', patch);
    document.getElementById('rMsg').textContent = '已合并保存 ✔';
  }catch(e){ document.getElementById('rMsg').textContent = '失败：'+e.message; }
}
window.saveRaw = saveRaw;
window.route = route;
window.addEventListener('hashchange', ()=>route(location.hash.replace(/^#\/?/,'')));
const _origRoute = route;
let _renderSeq = 0;
let _viewChain = Promise.resolve();
route = function(hash){
  const my = ++_renderSeq;
  const key = String(hash||'');
  _viewChain = _viewChain.then(async () => {
    if(my !== _renderSeq) return;  // 已有更新的渲染请求，丢弃本次（防竞态互相覆盖）
    if(key === 'config'){ setTab('config'); return renderConfig(); }
    if(key === 'agents'){ setTab('agents'); return renderAgents(); }
    if(key === 'docs'){ setTab('docs'); return renderDocs(); }
    if(key === 'mat'){ setTab('mat'); return renderMat(); }
    if(key === 'create'){ setTab('create'); return renderCreate(); }
    if(key === 'sched'){ setTab('sched'); return renderSched(); }
    if(key === 'runlogs'){ setTab('runlogs'); return renderRunlogs(); }
    setTab('');
    return _origRoute(key);
  }).catch(e => { try{ console.warn('render error:', e); }catch(_e){} });
  return _viewChain;
};
window.route = route;
route(location.hash.replace(/^#\/?/,''));
setInterval(()=>{ const h = location.hash.replace(/^#\/?/,'');
  if(!document.getElementById('auto')?.checked || !(h===''||h.startsWith('p/'))) return;
  if(document.hidden) return;                                  // 后台标签不刷
  const ae = document.activeElement;                            // 正在输入/选择时不打断
  if(ae && (ae.tagName==='INPUT'||ae.tagName==='SELECT'||ae.tagName==='TEXTAREA')) return;
  const sy = window.scrollY; route(h).then(()=>window.scrollTo(0, sy)); }, 20000);
</script>
</body></html>
"""
