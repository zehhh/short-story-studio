# -*- coding: utf-8 -*-
"""短篇小说工作室 CLI。

用法示例：
  python -m studio run --idea "婆婆把我陪嫁房过户给小叔子，我用公证反击"
  python -m studio run --source weibo,zhihu --pick auto
  python -m studio hot --source mock
  python -m studio continue
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import hotspots, langqa, pipeline
from .config import ROOT, load_config
from .llm import OpenAICompatLLM, get_llm
from .store import create_project, find_project, latest_project
from .util import now_ts


def _setup_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def build_parser() -> argparse.ArgumentParser:
    # 公共参数：放在顶层或子命令后均可。
    # default=SUPPRESS：未显式传参时不写入 namespace，避免子命令的 default
    # 覆盖顶层已解析的值（argparse 经典坑：--config 写在子命令前会被静默丢弃）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="配置文件路径（默认 ./config.yaml）")
    common.add_argument("--provider", choices=["auto", "mock", "api"], default=argparse.SUPPRESS,
                        help="LLM 提供方")
    common.add_argument("--platform", default=argparse.SUPPRESS, help="目标平台 zhihu/fanqie/uc/qimao")
    common.add_argument("--words", type=int, default=argparse.SUPPRESS, help="全稿目标字数")
    p = argparse.ArgumentParser(
        prog="studio",
        description="短篇小说工作室：抓热点 → 提核心梗 → 扩写下沉市场短篇",
        parents=[common],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("hot", parents=[common], help="抓取各大平台热点榜")
    sp.add_argument("--source", help="逗号分隔：weibo,zhihu,baidu,douyin,toutiao,bilibili,tieba,mock,custom")
    sp.add_argument("--top", type=int, help="每个平台抓取条数")

    sp = sub.add_parser("mine", parents=[common], help="抓热点 + 提炼核心梗卡（可停在候选）")
    sp.add_argument("--source", help="逗号分隔的平台来源")
    sp.add_argument("--top", type=int)
    sp.add_argument("--project", help="沿用已有项目（名称关键字）")
    sp.add_argument("--pick", help="auto=自动选最高分 / 数字=指定梗卡 / ask=交互选择（默认 tty 时 ask）")
    sp.add_argument("--n", type=int, help="梗卡候选数量")

    sp = sub.add_parser("run", parents=[common], help="一键全流程：热点/自拟梗 → 完整短篇")
    sp.add_argument("--idea", help="直接给核心梗（跳过热点抓取），如：彩礼陷阱+借条反杀")
    sp.add_argument("--direction", help="指定题材方向（配合 --idea）")
    sp.add_argument("--source", help="逗号分隔的热点来源")
    sp.add_argument("--top", type=int)
    sp.add_argument("--pick", help="auto/数字/ask（默认 tty 时 ask）")
    sp.add_argument("--chapters", type=int, help="强制章节数")
    sp.add_argument("--mock", action="store_true", help="等价 --provider mock（离线试跑）")
    sp.add_argument("--mode", choices=["agent", "hybrid"], default=None,
                    help="正文生成模式：agent=多agent逐章门禁（默认）；hybrid=单writer同上下文续写整稿（不做逐章打回）")
    sp.add_argument("--direct", action="store_true",
                    help="直接写模式：跳过提梗/大纲/圣经，写手从idea从头写到尾，只保留审核+读者团")
    _add_gate_args(sp)

    sp = sub.add_parser("continue", parents=[common], help="续跑最近/匹配的项目至成稿")
    sp.add_argument("--project", help="项目名称关键字（缺省=最新）")
    sp.add_argument("--chapters", type=int)
    sp.add_argument("--mock", action="store_true")
    sp.add_argument("--mode", choices=["agent", "hybrid"], default=None,
                    help="正文生成模式：agent=多agent逐章门禁（默认）；hybrid=单writer同上下文续写整稿")
    _add_gate_args(sp)

    sp = sub.add_parser("tropes", parents=[common], help="查看下沉市场梗库方向")

    sp = sub.add_parser("fetch", parents=[common],
                        help="热门小说抓取→深拆入库（源可在 config novel_fetch.sources 自定义）")
    sp.add_argument("--source", help="源名称（缺省=第一个源）；--list 看全部源")
    sp.add_argument("--top", type=int, default=2, help="抓取本数")
    sp.add_argument("--max-chapters", type=int, help="每本最多章数（缺省用源配置）")
    sp.add_argument("--no-decon", action="store_true", help="只下载不深拆")
    sp.add_argument("--list", action="store_true", help="只列出已配置的源")

    sp = sub.add_parser("kb", parents=[common],
                        help="知识库：collect 知识卡 / list/show 清单 / stats 向量库 / recall 联想 / rebuild 重建 / migrate 素材入库")
    sp.add_argument("action", choices=["list", "show", "collect", "stats", "recall", "rebuild", "migrate",
                                       "dedup", "tag", "lint", "enrich", "batch-decon"])
    sp.add_argument("--apply", action="store_true", help="dedup：应用压实（默认只扫描报告）")
    sp.add_argument("--workers", type=int, default=2, help="batch-decon：并发线程数（默认 2）")
    sp.add_argument("--no-dedup", action="store_true", help="batch-decon：跳过收尾全库去重")
    sp.add_argument("--dedup-wait", type=int, default=150,
                    help="batch-decon：收尾去重前等模型卸载的秒数（默认 150，云 API 可设 0）")
    sp.add_argument("--no-semantic", action="store_true", help="dedup：跳过语义近重复扫描（只做同文/同开头/id修复）")
    sp.add_argument("--threshold", type=float, default=0.93, help="dedup：语义近重复余弦阈值")
    sp.add_argument("--arc", choices=["起", "承", "转", "合"], help="recall 弧位定向")
    sp.add_argument("--q", help="recall 查询词（如：离婚 净身出户 后悔）")
    sp.add_argument("--limit", type=int, default=500, help="migrate 迁移条数 / batch-decon 本次最多拆几篇")
    sp.add_argument("direction", nargs="?", help="题材方向名称（如 彩礼纠纷）")
    sp.add_argument("--all", dest="all_dirs", action="store_true", help="收集全部方向")
    sp.add_argument("--pages", type=int, help="最多抓取网页数")
    sp.add_argument("--refresh", action="store_true", help="强制重新收集")
    sp.add_argument("--per-field", type=int, default=6, help="enrich：每类补充条数（默认 6）")
    sp.add_argument("--rewrite-thin", action="store_true",
                    help="enrich：不追加新条目，只把卡里过于概括的条目就地改写成可写素材")

    sp = sub.add_parser("local", parents=[common], help="本地小模型：探测引擎 / 基准测试 / 落地方案")
    sp.add_argument("--bench", metavar="MODEL", help="对指定模型跑 速度+JSON可靠性 基准")
    sp.add_argument("--base-url", help="引擎地址（默认自动探测 ollama/lmstudio/llama-server）")
    sp.add_argument("--api-key", default="", help="本地引擎一般不需要")
    sp.add_argument("--setup", action="store_true", help="打印按硬件档位的落地方案")
    sp.add_argument("--snippet", action="store_true", help="打印 config.yaml 角色路由片段")
    sp.add_argument("--pure", action="store_true", help="配合 --snippet：生成纯本地模式（默认混合模式）")

    sp = sub.add_parser("material", parents=[common],
                        help="素材库：list/stats/deposit/add/remove/curate/trash/restore/from-hotspots")
    sp.add_argument("action", choices=["list", "stats", "deposit", "add", "remove", "curate", "trash",
                                       "restore", "from-hotspots"])
    sp.add_argument("--batch", type=int, default=15, help="整理 Agent 每批条数")
    sp.add_argument("--project", help="项目关键字（deposit 用，缺省=最新）")
    sp.add_argument("--type", dest="mtype", help="素材类型筛选/指定")
    sp.add_argument("--direction", help="题材筛选/指定")
    sp.add_argument("--directions", help="题材列表，逗号分隔（from-hotspots 用）")
    sp.add_argument("--q", help="关键词搜索")
    sp.add_argument("--n", type=int, default=20, help="列出条数")
    sp.add_argument("--text", help="add 的素材内容")
    sp.add_argument("--id", help="remove 的素材 ID")

    sp = sub.add_parser("memory", parents=[common],
                        help="记忆库：layers 冷热分层 / recall 引力检索自检 / consolidate 衰减整理")
    sp.add_argument("action", choices=["layers", "recall", "consolidate"])
    sp.add_argument("--q", help="recall 查询词")
    sp.add_argument("--direction", help="题材加权")
    sp.add_argument("--dry-run", action="store_true", help="consolidate 只报告不落盘")

    sp = sub.add_parser("schedule", parents=[common], help="定时任务：add/list/remove/toggle/run")
    sp.add_argument("action", choices=["add", "list", "remove", "toggle", "run"])
    sp.add_argument("--name", help="任务名称")
    sp.add_argument("--time", help="每日触发时间 HH:MM")
    sp.add_argument("--interval", type=int, help="间隔触发（分钟）")
    sp.add_argument("--count", type=int, default=1, help="每次撰写篇数")
    sp.add_argument("--directions", help="题材列表，逗号分隔（多篇轮换）")
    sp.add_argument("--sources", help="热点来源，逗号分隔")
    sp.add_argument("--job", help="任务 ID 或名称（remove/toggle/run 用）")

    sp = sub.add_parser("scheduler", parents=[common], help="启动定时调度器（常驻，每 60s 检查）")
    sp.add_argument("--once", action="store_true", help="只执行一轮到期任务即退出（测试）")
    sp.add_argument("--interval", type=int, default=60, help="检查间隔秒数")

    sp = sub.add_parser("deconstruct", parents=[common], help="拆书：txt 拆成结构化知识入库（--dir 批量）")
    sp.add_argument("path", help="txt 文件或目录（目录=批量拆书）")
    sp.add_argument("--title", help="书名（缺省用文件名）")
    sp.add_argument("--max-sections", type=int, default=24, help="最多拆多少段")
    sp.add_argument("--per-call", type=int, default=2, help="每次 LLM 调用拆几段")

    sp = sub.add_parser("rag", parents=[common], help="RAG 知识检索：rebuild 重建 / search 查询")
    sp.add_argument("action", choices=["rebuild", "search"])
    sp.add_argument("--q", help="查询词（search 用）")
    sp.add_argument("--top", type=int, default=8)

    sp = sub.add_parser("produce", parents=[common],
                        help="全流程制作：N 篇写作 → 发布 → 数据回流 → 复盘改进")
    sp.add_argument("--idea", help="核心梗（与热点二选一）")
    sp.add_argument("--source", help="热点来源（热点模式）")
    sp.add_argument("--count", type=int, default=1, help="撰写篇数")
    sp.add_argument("--directions", help="题材列表，逗号分隔（多篇轮换）；A*B=同篇组合题材（如 规则怪谈*女主后宫）")
    sp.add_argument("--publish-mode", choices=["none", "manual", "mock", "custom", "fanqie_draft"],
                    default="manual", help="发布模式（默认 manual 投稿包）")
    sp.add_argument("--data-source", choices=["none", "mock", "custom"],
                    default="mock", help="数据回流来源")
    sp.add_argument("--with-improve", action="store_true", help="复盘改进（数据回流后自动）")
    sp.add_argument("--no-kb", action="store_true", help="关闭题材知识抓取")
    sp.add_argument("--kb-refresh", action="store_true", help="强制重新搜集题材知识卡")
    sp.add_argument("--reject-overflow", action="store_true",
                    help="字数越界时淘汰出工作台（默认保留并标记 length_out_of_range）")
    sp.add_argument("--mode", choices=["agent", "hybrid"], default=None,
                    help="正文生成模式：agent=多agent逐章门禁；hybrid=单writer同上下文续写整稿（默认取 config.pipeline.mode）")
    sp.add_argument("--deposit", action="store_true",
                    help="成稿后自动沉淀素材入库（默认关：机器稿回灌素材库会自回声）")

    sp = sub.add_parser("project", parents=[common], help="项目管理：list/delete")
    sp.add_argument("action", choices=["list", "delete"])
    sp.add_argument("--project", help="项目 ID 或名称关键字（delete 必填）")

    sp = sub.add_parser("publish", parents=[common], help="发布成稿：manual 投稿包 / custom 接口 / mock")


    sp.add_argument("--project", help="项目关键字（缺省=最新）")
    sp.add_argument("--mode", choices=["manual", "custom", "mock", "fanqie_draft"], help="发布模式（fanqie_draft=浏览器自动化推番茄草稿箱）")
    sp.add_argument("--cookie-file", help="cookie 文件路径（custom 模式）")
    sp.add_argument("--probe", action="store_true", help="只探测自定义接口连通性")

    sp = sub.add_parser("analytics", parents=[common], help="阅读数据：fetch 抓取 / import 录入 / show 查看")
    sp.add_argument("action", choices=["fetch", "import", "show"])
    sp.add_argument("--project", help="项目关键字（缺省=全部/最新）")
    sp.add_argument("--source", choices=["mock", "custom"], help="数据源（fetch 用）")
    sp.add_argument("file", nargs="?", help="import 的 JSON/CSV 文件")

    sp = sub.add_parser("improve", parents=[common], help="数据复盘：对比门禁预测与真实数据，沉淀改进教训")
    sp.add_argument("--project", help="项目关键字（缺省=最新）")

    sp = sub.add_parser("dashboard", parents=[common], help="可视化看板：启动本地服务或导出静态 HTML")
    sp.add_argument("--port", type=int, default=8600, help="服务端口（默认 8600，占用自动后移）")
    sp.add_argument("--export", nargs="?", const="dashboard.html", default=None,
                    metavar="PATH", help="不启服务，导出自包含静态 HTML（默认 workspace/dashboard.html）")
    sp.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    sp = sub.add_parser("review", parents=[common],
                        help="证据化评审卡：查看/重算项目的逐章评分卡与证据覆盖率")
    sp.add_argument("--project", help="项目名或关键字（默认最近一个）")
    sp.add_argument("--chapter", type=int, help="只看某一章（默认全部）")
    sp.add_argument("--recompute", action="store_true",
                    help="用已有 reviews/chNN.json 重新计算（不重新调模型）")
    sp.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    sp = sub.add_parser("drafts", parents=[common],
                        help="断点保稿：列出可恢复草稿 / 查看某章草稿 / 丢弃草稿")
    sp.add_argument("--project", help="项目名或关键字（默认最近一个）")
    sp.add_argument("--chapter", type=int, help="只看某一章")
    sp.add_argument("--show", action="store_true", help="打印草稿正文（配合 --chapter）")
    sp.add_argument("--drop", action="store_true", help="丢弃该章草稿（配合 --chapter）")
    sp.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    sp = sub.add_parser("check", parents=[common], help="体检：LLM 连通性 + 热点源可用性")
    sp.add_argument("--source", help="只测这些热点源")

    sp = sub.add_parser("verify", parents=[common],
                        help="项目自检：编译 + 静态可达性 + 提示词契约 + 规则漂移 + 功能矩阵（--full 加冒烟）")
    sp.add_argument("--full", action="store_true", help="全量档：额外跑 tests/smoke_test.py（MockLLM 全链路）")
    sp.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sp.add_argument("--only", help="只跑名字包含该子串的检查（如 --only=契约）")
    return p


def _add_gate_args(sp):
    sp.add_argument("--no-review", action="store_true", help="关闭主编审查+读者评分门禁（单稿直出）")
    sp.add_argument("--no-kb", action="store_true", help="关闭题材知识自动搜集")
    sp.add_argument("--kb-refresh", action="store_true", help="强制重新搜集题材知识卡")
    sp.add_argument("--threshold", type=float, help="读者团均分阈值（默认 7.5）")
    sp.add_argument("--rounds", type=int, help="审查打回重写上限（默认 3）")
    sp.add_argument("--readers", type=int, help="读者团画像数量（默认 3）")
    sp.add_argument("--no-langqa", action="store_true", help="关闭语言质检门（确定性扫描，默认开）")
    sp.add_argument("--no-lang-polish", action="store_true",
                    help="关闭全稿语言打磨（定向改写碎句/欧化/冗余，默认开、花 token）")
    sp.add_argument("--no-emotion", action="store_true", help="关闭情绪量化门（默认开）")
    sp.add_argument("--refresh-polish", action="store_true", help="强制重跑全稿润色体检（默认已有 report.json 则跳过）")
    sp.add_argument("--deposit", action="store_true",
                    help="成稿后自动沉淀素材入库（默认关：机器稿回灌素材库会自回声，越写越像自己）")


def _apply(cfg: dict, args) -> dict:
    # 记录配置文件路径：scheduler 拉起的子进程必须用同一份配置，
    # 否则自定义 --config 下定时任务会用默认 config.yaml（真实 API/真实 workspace）执行
    # （缺陷 P1：调度器配置透传）。所有子命令共用，不只在 schedule add 里设。
    if getattr(args, "config", None):
        cfg["_config_file"] = str(getattr(args, "config"))
    if getattr(args, "provider", None):
        cfg["llm"]["provider"] = args.provider
    if getattr(args, "mock", False):
        cfg["llm"]["provider"] = "mock"
    if getattr(args, "platform", None):
        cfg["studio"]["platform"] = args.platform
    if getattr(args, "words", None):
        cfg["studio"]["target_words"] = args.words
    if getattr(args, "no_review", False):
        cfg["review"]["enabled"] = False
    if getattr(args, "no_kb", False):
        cfg["knowledge"]["auto_collect"] = False
    if getattr(args, "threshold", None):
        cfg["review"]["score_threshold"] = args.threshold
    if getattr(args, "rounds", None):
        cfg["review"]["max_review_rounds"] = args.rounds
    if getattr(args, "readers", None):
        cfg["review"]["reader_panel"] = args.readers
    if getattr(args, "no_langqa", False):
        cfg["review"]["langqa_enabled"] = False
    if getattr(args, "no_lang_polish", False):
        cfg["review"]["lang_polish_enabled"] = False
    if getattr(args, "no_emotion", False):
        cfg["review"]["emotion_enabled"] = False
    if getattr(args, "mode", None):
        cfg.setdefault("pipeline", {})["mode"] = args.mode
    if getattr(args, "deposit", False):
        cfg.setdefault("materials", {})["auto_deposit"] = True
    return cfg


def _sources(args) -> list[str] | None:
    return args.source.split(",") if getattr(args, "source", None) else None


def _pick_mode(args) -> str | None:
    pick = getattr(args, "pick", None)
    if pick is None:
        return "ask" if sys.stdin.isatty() else "auto"
    return pick


def _print_hot(items):
    for it in items[:50]:
        print(f"  {it.rank:>2}. [{it.source}] {it.title}  ({int(it.hot_value)})")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_hot(cfg: dict, args):
    items = hotspots.fetch_all(cfg, sources=_sources(args), top=args.top)
    if not items:
        print("未抓到热点。可尝试 --source mock 离线演示，或配置 dailyhot_base 走聚合代理。")
        return
    _print_hot(items)
    ws = cfg["studio"]["workspace_abs"]
    out = hotspots_save(cfg, items)
    print(f"\n共 {len(items)} 条 → {out}")


def hotspots_save(cfg: dict, items):
    from pathlib import Path

    d = Path(cfg["studio"]["workspace_abs"]) / "hotspots"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"hot_{time.strftime('%Y%m%d_%H%M%S')}.json"
    f.write_text(json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def _project_for_mine(cfg: dict, args):
    if args.project:
        return find_project(cfg["studio"]["workspace_abs"], args.project)
    from pathlib import Path

    return create_project(Path(cfg["studio"]["workspace_abs"]), "hot_" + now_ts())


def cmd_mine(cfg: dict, args):
    llm = get_llm(cfg)
    project = _project_for_mine(cfg, args)
    print(f"[project] {project.root.name}")
    pipeline.stage_fetch(cfg, project, sources=_sources(args), top=args.top,
                         direction_hint=getattr(args, "direction", "") or "")
    pipeline.stage_mine(cfg, llm, project, n=args.n, direction_hint=getattr(args, "direction", "") or "")
    pick = _pick_mode(args)
    if pick != "ask" or sys.stdin.isatty():
        concept = pipeline.stage_select(project, pick=pick if pick != "ask" else "ask", cfg=cfg)
        print(f"\n已选定：{concept.get('direction')} | {concept.get('core_trope')}")
        print(f"继续执行：python -m studio continue --project {project.root.name}")
    else:
        print("\n梗卡候选已生成（未选定）。查看后执行：python -m studio continue --project ...")


def cmd_run(cfg: dict, args):
    llm = get_llm(cfg)
    from pathlib import Path

    if args.idea:
        # 用户亲口写进选题的道具不该被自己的禁用表拦住（实测会连挂两轮大纲）
        langqa.exempt_terms_from_idea(cfg, args.idea)
        concept = pipeline.make_idea_concept(args.idea, args.direction)
        project = create_project(Path(cfg["studio"]["workspace_abs"]), args.idea)
        _persist_banned_exempt(cfg, project)
        project.save_json("concept.json", concept)
        project.set_stage("concept", concept.get("direction", ""))
        print(f"[project] {project.root.name}\n[idea] {concept['direction']} | {concept['core_trope']}")
    else:
        project = create_project(Path(cfg["studio"]["workspace_abs"]), "hot_" + now_ts())
        print(f"[project] {project.root.name}")
        pipeline.stage_fetch(cfg, project, sources=_sources(args), top=args.top)
        pipeline.stage_mine(cfg, llm, project, direction_hint=getattr(args, "direction", "") or "")
        pipeline.stage_select(project, pick=_pick_mode(args), cfg=cfg)

    # 项目写锁：阻止同目录被看板/调度器/另一个 CLI 同时写入（跨进程，见 studio/lock.py）
    from .lock import project_lock

    with project_lock(project.root, "run") as lock:
        pipeline.stage_kb(cfg, llm, project, force=getattr(args, "kb_refresh", False))

        # 直接写模式：跳过提梗/大纲/圣经，写手从头写到尾
        if getattr(args, "direct", False):
            pipeline.stage_write_direct(cfg, llm, project)
            pipeline.stage_polish(cfg, llm, project, force=getattr(args, "refresh_polish", False))
            _summary(project)
            return

        pitch = pipeline.stage_pitch(cfg, llm, project)
        pipeline.stage_lead(cfg, llm, project)
        if args.chapters:
            pitch["chapter_count"] = args.chapters
            project.save_json("pitch.json", pitch)
            print(f"[stage] 指定章节数：{args.chapters}")
        pipeline.stage_outline(cfg, llm, project)
        lock.beat("bible")
        pipeline.stage_bible(cfg, llm, project)
        pipeline.stage_write(cfg, llm, project)
        lock.beat("polish")
        pipeline.stage_polish(cfg, llm, project, force=getattr(args, "refresh_polish", False))
    _summary(project)


def cmd_continue(cfg: dict, args):
    llm = get_llm(cfg)
    ws = cfg["studio"]["workspace_abs"]
    project = (
        find_project(ws, args.project)
        if args.project
        else latest_project(ws)
    )
    if project is None:
        raise SystemExit("没有可续跑的项目，先用 run/mine 创建。")
    from .lock import project_lock

    print(f"[project] {project.root.name}（status: {json.dumps(project.stage_status(), ensure_ascii=False)}）")
    # 直接写项目（project.json.mode=direct / 只有 ch01 且无 outline）：
    # 不能再跑 pitch→lead→outline→bible，否则会把 direct 成稿重排成编号小节
    # （缺陷 P1-13）。直接续润色即可。
    _meta_mode = (project.meta or {}).get("mode", "")
    # 续跑必须延续项目自己的写作模式：cfg 来自 config.yaml，`run --mode hybrid` 建的
    # 项目如果配置里是 agent，continue 就会中途换模式——章节文件命名与门禁语义都不同，
    # 等于把同一篇稿子拆成两套流水线。项目里记过 mode 就以项目为准。
    if _meta_mode and _meta_mode in ("agent", "hybrid", "direct"):
        cfg.setdefault("pipeline", {})
        if (cfg["pipeline"].get("mode") or "agent") != _meta_mode:
            print(f"[mode] 延续项目模式：{_meta_mode}（配置里是 "
                  f"{cfg['pipeline'].get('mode') or 'agent'}）")
        cfg["pipeline"]["mode"] = _meta_mode
    _is_direct = (_meta_mode == "direct"
                  or (not project.has("outline.json")
                      and project.chapter_file(1).exists()
                      and len(list((project.root / "chapters").glob("ch*.md"))) <= 1))
    if _is_direct:
        print("[stage] 直接写项目：跳过提梗/大纲/圣经，直接续跑润色体检")
        with project_lock(project.root, "continue"):
            pipeline.stage_polish(cfg, llm, project, force=getattr(args, "refresh_polish", False))
        _summary(project)
        return
    if project.has("concept.json"):
        c = project.load_json("concept.json")
        print(f"[concept] {c.get('direction')} | {c.get('core_trope')}")
    with project_lock(project.root, "continue") as lock:
        pipeline.stage_kb(cfg, llm, project, force=getattr(args, "kb_refresh", False))
        pitch = pipeline.stage_pitch(cfg, llm, project)
        pipeline.stage_lead(cfg, llm, project)
        if args.chapters:
            pitch["chapter_count"] = args.chapters
            project.save_json("pitch.json", pitch)
        pipeline.stage_outline(cfg, llm, project)
        lock.beat("bible")
        pipeline.stage_bible(cfg, llm, project)
        pipeline.stage_write(cfg, llm, project)
        lock.beat("polish")
        pipeline.stage_polish(cfg, llm, project, force=getattr(args, "refresh_polish", False))
    _summary(project)



def cmd_knowledge_base(cfg: dict, args):
    """知识库统一入口：题材知识卡（collect/list/show）+ 知识系统（stats/recall/rebuild/migrate/dedup）。"""
    from . import kb

    action = getattr(args, "action", "list")
    if action == "batch-decon":
        # 原 `python -m studio.batch_decon` 的口子，收进主 CLI：
        # 这样 --config/--provider 等公共参数按统一规则生效，
        # 用户也不必记第二个入口。
        from . import batch_decon

        res = batch_decon.run(
            cfg,
            limit=int(getattr(args, "limit", 0) or 0),
            workers=int(getattr(args, "workers", 2) or 2),
            dedup=not getattr(args, "no_dedup", False),
            dedup_wait=int(getattr(args, "dedup_wait", 150) or 0),
        )
        print(f"[batch-decon] 汇总：本轮成功 {res['ok']}｜累计完成 {res['done']}/{res['total']}｜"
              f"待重试 {res['failed']}")
        return
    if action == "lint":
        from . import kb_lint
        from .config import data_path

        kdir = data_path("knowledge")
        rows = kb_lint.lint_all(kdir)
        if not rows:
            print(f"[kb-lint] {len(list(kdir.glob('*.json')))} 张知识卡全部通过质量校验")
            return
        total = sum(len(r["issues"]) for r in rows)
        print(f"[kb-lint] {len(rows)} 张卡存在内容问题，共 {total} 处"
              f"（知识卡会原样注入写作提示词，不可用内容=直接污染成稿语感）")
        for r in rows:
            print(f"\n  ■ {r['name']}")
            for it in r["issues"]:
                label = kb_lint.CODE_LABEL.get(it["code"], it["code"])
                idx = "" if it["index"] < 0 else f"[{it['index']}]"
                print(f"    {it['code']} {label} · {it['field']}{idx} · {it['text']}")
        if not getattr(args, "apply", False):
            print("\n（dry-run：确认后加 --apply 执行确定性清理；清理只删不写，"
                  "补内容需人工或 kb collect 重采）")
            return
        n_cards = n_changes = 0
        for p in sorted(kdir.glob("*.json")):
            card = json.loads(p.read_text(encoding="utf-8"))
            new, changes = kb_lint.fix_card(card)
            if not changes:
                continue
            p.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            n_cards += 1
            n_changes += len(changes)
        print(f"\n[kb-lint] 已清理 {n_cards} 张卡 / {n_changes} 处改动")
        left = kb_lint.lint_all(kdir)
        print(f"[kb-lint] 清理后仍有问题 {len(left)} 张卡（需人工重写）")
        return
    if action == "enrich":
        from . import kb_enrich

        dry = not getattr(args, "apply", False)
        dirs = [] if getattr(args, "all_dirs", False) else ([args.direction] if args.direction else [])
        if not dirs and not getattr(args, "all_dirs", False):
            raise SystemExit("用法：kb enrich <方向> / kb enrich --all [--per-field 6] "
                             "[--rewrite-thin] [--apply]")
        if getattr(args, "rewrite_thin", False):
            # 只改写、不追加：把卡里"标题式概括"就地换成可写素材
            st = kb_enrich.rewrite_all(cfg, directions=dirs or None,
                                       backup_dir="out/kb_rewrite_backup", dry_run=dry)
            print(f"[kb-rewrite] {st['cards']} 张卡改写 {st['changed']} 条 | "
                  f"质量门拒绝 {st['rejected']} 条（具体度<{st['min_score']}"
                  f"{'，dry-run' if dry else ''}）")
            for d in st["detail"][:12]:
                print(f"  ~ {d['card']:16s} 改 {d['changed']} 拒 {d['rejected']}")
                for old_t, new_t in d.get("sample") or []:
                    print(f"      - {old_t}")
                    print(f"      + {new_t}")
            print("[kb-rewrite] 建议随后跑 `python -m studio kb lint` 复核整卡")
            return
        per = int(getattr(args, "per_field", 6) or 6)
        st = kb_enrich.enrich_all(cfg, directions=dirs or None, per_field=per,
                                  backup_dir="out/kb_enrich_backup", dry_run=dry)
        if dry:
            print(f"[kb-enrich] dry-run：将扩写 {('全部方向' if not dirs else ','.join(dirs))}，"
                  f"每类补 {per} 条（加 --apply 落盘，落盘前自动备份到 out/kb_enrich_backup/）")
        print(f"[kb-enrich] {st['cards']} 张卡新增 {st['added']} 条 | 质量门拒绝 {st['rejected']} 条")
        for d in st["detail"]:
            if d["added"]:
                print(f"  + {d['card']:16s} {d['added']} 条 {json.dumps(d.get('by') or {}, ensure_ascii=False)}")
                for k, v in (d.get("sample") or {}).items():
                    for s in v:
                        print(f"      [{k}] {s}")
        print("[kb-enrich] 建议随后跑 `python -m studio kb lint` 复核整卡")
        return
    if action == "dedup":
        import time as _t

        from . import kb_dedup

        # 批量深拆在写库，压实会冲突：state 文件 3 分钟内更新过则拒绝 apply
        state_f = kb.kb_dir(cfg) / "decon_state.json"
        if getattr(args, "apply", False) and state_f.exists() and \
                _t.time() - state_f.stat().st_mtime < 180:
            raise SystemExit("[kb-dedup] 批量深拆似在运行（decon_state 3 分钟内有更新），"
                             "请先暂停批量深拆再 apply")
        res = kb_dedup.scan(cfg, semantic=not getattr(args, "no_semantic", False),
                            threshold=float(getattr(args, "threshold", 0.93) or 0.93))
        st = res["stats"]
        print(f"[kb-dedup] 扫描完成：{st['units']} 单元，命中冗余 {st['drop_total']} 条")
        print("  按原因:", json.dumps(st["by_reason"], ensure_ascii=False))
        if st.get("semantic_groups"):
            print(f"  语义近重复组: {st['semantic_groups']}")
        if not getattr(args, "apply", False):
            print("（dry-run，未改动库。确认后加 --apply 执行压实并重建索引）")
            return
        out = kb_dedup.apply(cfg, res)
        print(f"[kb-dedup] 压实完成：保留 {out['kept']} | 剔除 {out['dropped']} | "
              f"补编码 {out['reindexed']} 条")
        return
    if action in ("stats", "recall", "rebuild", "migrate", "tag"):
        if action == "stats":
            st = kb.stats(cfg)
            print(f"知识单元 {st['units']} 条 | 向量已索引 {st['vectors_indexed']} | 共现节点 {st['coco_nodes']}")
            print("按弧位:", json.dumps(st["by_arc"], ensure_ascii=False))
            print("按类型:", json.dumps(st["by_kind"], ensure_ascii=False))
            print("Top 题材:", json.dumps(st["top_directions"], ensure_ascii=False))
            return
        if action == "tag":
            from . import kb as _kb

            st = _kb.tag_all(cfg)
            print(f"单元库 {st['units']} 条：更新 {st['units_updated']}（含题材标签 {st['units_tagged']}）")
            print(f"素材库 {st['materials']} 条：更新 {st['materials_updated']}（含题材标签 {st['materials_tagged']}）")
            if st["units_updated"] or st["materials_updated"]:
                n = _kb.rebuild_cocomatrix(cfg)
                print(f"共现矩阵已重建 {n} 节点（题材共现生效）")
            return
        if action == "recall":
            block, used = kb.recall(cfg, getattr(args, "q", "") or "", arc=getattr(args, "arc", "") or "",
                                    direction=getattr(args, "direction", "") or "", max_chars=900)
            print(block or "（无命中）")
            if used:
                print(f"\n[命中 {len(used)} 条]")
            return
        if action == "rebuild":
            n = kb.rebuild(cfg)
            c = kb.rebuild_cocomatrix(cfg)
            print(f"向量重建 {n} 条 | 共现矩阵 {c}")
            return
        n = kb.ingest_materials(cfg, limit=getattr(args, "limit", 500))
        print(f"存量素材迁移：新增知识单元 {n} 条")
        # 迁移完成必须 return：旧实现缺 return 会继续落进下面的「题材知识卡」分支，
        # 打出「没有 X 的知识卡，先 kb collect」并以退出码 1 收尾（缺陷：迁移成功却报失败）。
        return
    # 题材知识卡（原功能）
    from . import knowledge as kbmod
    from .config import data_path

    if action == "collect":
        from .llm import get_llm, resolve_llm

        direction = getattr(args, "direction", "") or ""
        if getattr(args, "all_dirs", False):
            dirs = [d["name"] for d in json.loads(
                data_path("tropes.json").read_text(encoding="utf-8"))["directions"]]
        elif direction:
            dirs = [direction]
        else:
            raise SystemExit("用法：kb collect <方向> 或 --all")
        llm = get_llm(cfg)
        if getattr(args, "pages", None):
            cfg["knowledge"]["max_pages"] = args.pages
        for i, d in enumerate(dirs, 1):
            print(f"\n== [{i}/{len(dirs)}] 知识卡：{d} ==")
            try:
                card = kbmod.build_card(cfg, resolve_llm(cfg, llm, "light"), d,
                                        force=getattr(args, "refresh", False))
                _cp = kbmod.card_path(d)
                if card.get("unlisted_direction"):
                    print(f"  ⚠ {d}：不在梗库预设方向内，已跳过知识卡（可用 `tropes` 查看预设方向）")
                else:
                    print(f"  ✔ {d}：{card.get('source_count', 0)} 页资料 → "
                          f"{_cp.name if _cp else '（未落盘）'}")
            except Exception as e:  # noqa: BLE001
                print(f"  ✘ {d}：{e}")
        return
    if action == "list":
        kdir = data_path("knowledge")
        for f in sorted(kdir.glob("*.json")):
            try:
                card = json.loads(f.read_text(encoding="utf-8"))
                print(f"  {card.get('direction', f.stem)}（{f.stem}）来源 {card.get('source_count', '?')} 页")
            except json.JSONDecodeError:
                continue
        return
    # show
    direction = getattr(args, "direction", "") or ""
    card = kbmod.load_card(direction)
    if not card:
        raise SystemExit(f"没有 {direction} 的知识卡，先 kb collect {direction}")
    print(json.dumps(card, ensure_ascii=False, indent=1)[:3000])


def cmd_tropes(cfg: dict, args):
    tropes = pipeline.load_tropes()
    print(f"下沉市场梗库：{len(tropes['directions'])} 个方向\n")
    print(f"{'方向':<10}{'热度':<5}{'情绪主轴':<10}核心冲突")
    for d in tropes["directions"]:
        print(f"{d['name']:<10}{d['heat']:<5}{d['emotion']:<10}{d['core_conflict']}")
    print("\n查看完整库：data/tropes.json")


def cmd_local(cfg: dict, args):
    from . import local as loc

    if args.setup:
        print(loc.setup_text())
        return

    engines = loc.detect_engines()
    if engines:
        print("== 探测到本地推理引擎 ==")
        for e in engines:
            print(f"  [{e['label']}] {e['base_url']}  模型: {', '.join(e['models'][:8]) or '（无已加载模型）'}")
    else:
        print("== 未探测到运行中的本地引擎 ==")
        print("  三个引擎任装其一即可（都提供 OpenAI 兼容接口）：")
        print("   - Ollama      https://ollama.com/download      拉模型: ollama pull qwen3:8b")
        print("   - LM Studio   https://lmstudio.ai              GUI 里启动 Local Server")
        print("   - llama.cpp   编译/下载 llama-server 后: llama-server -m 模型.gguf -c 16384")
        print("  ⚠ Ollama 用户务必设置上下文，否则默认 4K 会截断长稿：")
        print("     setx OLLAMA_CONTEXT_LENGTH 16384  （设完重启 Ollama）")
        if not args.snippet:
            print("\n完整落地方案：python -m studio local --setup")
        return

    if args.bench or args.snippet:
        base = args.base_url or (engines[0]["base_url"] if engines else None)
        if not base:
            raise SystemExit("没有可用引擎地址，用 --base-url 指定")
        models = next((e["models"] for e in engines if e["base_url"] == base), [])
        main_m = args.bench or loc.pick_model(models, prefer=["14b", "9b", "8b"])
        small_m = loc.pick_model(models, prefer=["4b", "8b"]) or main_m
        if args.bench:
            print(f"\n== 基准测试：{main_m} @ {base} ==")
            r = loc.bench_model(base, main_m, args.api_key)
            if r.get("speed_ok"):
                print(f"  中文生成：{r['chars']} 字 / {r['seconds']}s ≈ {r['chars_per_sec']} 字/秒")
                print(f"  JSON 指令遵循（审查/评分关键能力）：{r['json_ok']} 成功")
                print(f"  样例：{r['sample']}")
                if r["chars_per_sec"] and r["chars_per_sec"] < 5:
                    print("  ⚠ 速度偏慢：建议更小量化档位（q4_k_m）或换 4B 模型跑审查/评分")
                if r["json_ok"].startswith(("0", "1")):
                    print("  ⚠ JSON 可靠性不足：小模型审查时建议主 llm 也用同引擎较大模型，或开启 json_mode 兼容好的引擎")
            else:
                print(f"  失败：{r.get('error')}")
        if args.snippet:
            print("\n== config.yaml 片段（混合模式：写作云端，审查/评分本地）==")
            cloud_main = "deepseek-chat"  # 云端写手按你的 API 实际模型改名
            print(loc.snippet_text(base, cloud_main, small_m or "qwen3:8b", hybrid=not args.pure))
    else:
        print("\n下一步：")
        print("  python -m studio local --bench <模型名>   # 测速度与 JSON 可靠性")
        print("  python -m studio local --snippet          # 生成 config.yaml 角色路由片段")
        print("  python -m studio local --setup            # 按硬件档位的完整落地方案")


def _pick_project_for_cmd(cfg: dict, args):
    ws = cfg["studio"]["workspace_abs"]
    if getattr(args, "project", None):
        return find_project(ws, args.project)
    prj = latest_project(ws)
    if prj is None:
        raise SystemExit("没有项目。先 run/mine 创建。")
    return prj


def cmd_material(cfg: dict, args):
    from . import material as mat

    if args.action == "stats":
        st = mat.stats(cfg)
        print(f"素材总数：{st['total']}")
        print("按类型：", json.dumps(st["by_type"], ensure_ascii=False))
        print("按题材：", json.dumps(st["by_direction"], ensure_ascii=False))
        return
    if args.action == "deposit":
        project = _pick_project_for_cmd(cfg, args)
        res = mat.deposit_from_project(cfg, project)
        print(f"[material] 新增 {res['added']} 条，跳过重复 {res['skipped']} 条")
        return
    if args.action == "add":
        if not args.text:
            raise SystemExit("用法：material add --text \"素材内容\" [--type 类型] [--direction 题材]")
        item = mat.add_material(cfg, args.mtype or "核心梗", args.text,
                                direction=args.direction or "")
        print(f"[material] 已入库 {item['id']}（标签：{'、'.join(item['tags']) or '无'}）")
        return
    if args.action == "remove":
        if not args.id:
            raise SystemExit("用法：material remove --id Mxxxx")
        print("[material] 已删除" if mat.remove_material(cfg, args.id) else "[material] 未找到该 ID")
        return
    if args.action == "from-hotspots":
        from . import hotspots as hs
        from .llm import resolve_llm

        dirs = [d.strip() for d in args.directions.split(",")] if args.directions else []
        raw = hs.fetch_all(cfg, sources=["novel", "zhihu", "douyin", "toutiao"], top=8,
                           directions=dirs)
        print(f"[material] 抓到 {len(raw)} 条热点，转换中 …")
        conv = mat.convert_hotspots(cfg, resolve_llm(cfg, get_llm(cfg), "light"),
                                    [x.to_dict() for x in raw], direction=dirs[0] if dirs else "")
        for c in conv:
            item = mat.add_material(cfg, "开头钩子", c["text"], direction=c["direction"],
                                    tags=c.get("tags", []), note=c.get("note", ""))
            print(f"  入库 {item['id']} [{item['direction']}] {item['text'][:56]}")
        dropped = len(raw) - len(conv)
        print(f"[material] 转换 {len(conv)} 条入库，{dropped} 条不可用已丢弃（原始事件不入库）")
        return
    if args.action == "curate":
        llm = get_llm(cfg)
        res = mat.curate(cfg, llm, direction=args.direction or "", batch_size=args.batch)
        print(f"[material] 整理完成：评估 {res['checked']} 条，删除低质 {res['removed']} 条，"
              f"high {res.get('high', 0)} / mid {res.get('mid', 0)} / low {res.get('low', 0)}，纠偏 {res.get('corrected', 0)} 处")
        return
    if args.action == "trash":
        tr = mat.list_trash(cfg)
        if not tr:
            print("回收站为空")
            return
        for x in tr:
            print(f"  [{x['id']}] {x['text'][:60]}  理由：{x.get('note','')[:40]}")
        return
    if args.action == "restore":
        if not args.id:
            raise SystemExit("用法：material restore --id Mxxxx")
        print("[material] 已恢复" if mat.restore_material(cfg, args.id) else "[material] 回收站中未找到该 ID")
        return
    # list
    items = mat.query(cfg, mtype=args.mtype or "", direction=args.direction or "",
                      keyword=args.q or "", limit=args.n)
    if not items:
        print("素材库为空。生成时加 --deposit，或 material add 手动添加。")
        return
    for x in items:
        score = f" {x.get('score')}分" if x.get("score") else ""
        tags = f" [{'、'.join(x.get('tags', []))}]" if x.get("tags") else ""
        print(f"  [{x['type']}|{x['direction']}]{score}{tags} {x['text'][:60]}  ({x['id']})")



def cmd_memory(cfg: dict, args):
    """记忆库：冷热分层视图 / 引力检索自检 / 衰减整理（参考 VCPToolBox 记忆思想）。"""
    from . import memory as mem

    if args.action == "layers":
        lays = mem.layers(cfg)
        for k in ("L1", "L2", "L3", "L4"):
            its = lays.get(k, [])
            print(f"== {k}（{len(its)} 条）==")
            for it in its[:10]:
                hot = int(it.get("usage_count") or 0)
                print(f"  [热{hot}|{it.get('quality')}|{it.get('direction')}] {it.get('text', '')[:52]} ({it.get('id')})")
        return
    if args.action == "recall":
        block, used = mem.recall(cfg, args.q or "", direction=args.direction or "",
                                 reinforce=False)
        print(block or "（无命中）")
        print(f"\n[命中 {len(used)} 条素材：{', '.join(used[:8])}]" if used else "")
        return
    rep = mem.consolidate(cfg, dry_run=args.dry_run)
    print(f"衰减 {rep.get('decayed', 0)} 条；建议入回收站 {len(rep.get('suggest_trash', []))} 条"
          + (f"，已移动 {rep.get('moved_to_trash', 0)} 条" if not args.dry_run else "（dry-run 未执行）"))


def cmd_produce(cfg: dict, args):
    """全流程制作流水线：选题 → 写作门禁 → 发布 → 数据回流 → 复盘改进。

    单条命令跑通完整生产线；N 篇自动轮换题材；产出制作报告。
    """
    from . import analytics as an
    from . import publish as pub
    from .llm import get_llm
    from pathlib import Path as _P

    llm = get_llm(cfg)
    make_idea_concept = pipeline.make_idea_concept
    ws = _P(cfg["studio"]["workspace_abs"])
    count = max(1, int(args.count or 1))
    dirs = [d.strip().replace("*", "×") for d in (args.directions or "").split(",") if d.strip()]
    plat = getattr(args, "platform", None) or cfg["studio"].get("platform", "zhihu")
    publish_mode = (args.publish_mode or "none").lower()
    data_source = (args.data_source or "none").lower()
    with_improve = args.with_improve or data_source != "none"

    line = []
    print(f"=== 全流程制作：{count} 篇 | 题材：{'、'.join(dirs) or '跟随热点'} | "
          f"平台：{plat} | 单篇 {getattr(args, 'words', None) or cfg['studio'].get('target_words', '默认')} 字 ===")

    for i in range(count):
        direction = dirs[i % len(dirs)] if dirs else ""
        print(f"\n========== 第 {i + 1}/{count} 篇（题材：{direction or '跟随热点'}） ==========")
        if args.idea and count == 1:
            langqa.exempt_terms_from_idea(cfg, args.idea)
            project = create_project(ws, args.idea)
            concept = make_idea_concept(args.idea, direction or None)
            _persist_banned_exempt(cfg, project)
            project.save_json("concept.json", concept)
            project.set_stage("concept", concept.get("direction", ""))
        else:
            project = create_project(ws, "produce_" + now_ts())
            # 缺省也要抓热点：旧实现不传 --source 就不抓，stage_mine 直接
            # SystemExit('没有热点数据')，与 README「一键全流程可全留空」不符。
            # mock 模式用 mock 源（离线可跑），否则用 config.studio.sources。
            _srcs = args.source or ""
            if not _srcs:
                _srcs = ("mock" if str(cfg["llm"].get("provider")) == "mock"
                         else ",".join(cfg["studio"].get("sources") or []))
            if _srcs:
                pipeline.stage_fetch(cfg, project,
                                     sources=[x.strip() for x in _srcs.split(",") if x.strip()],
                                     top=8, direction_hint=direction)
            pipeline.stage_mine(cfg, llm, project, direction_hint=direction)
            pipeline.stage_select(project, pick="auto", llm=llm, cfg=cfg)
        if not args.no_kb:
            pipeline.stage_kb(cfg, llm, project, force=args.kb_refresh)
        pipeline.stage_pitch(cfg, llm, project)
        pipeline.stage_lead(cfg, llm, project)
        pipeline.stage_outline(cfg, llm, project)
        pipeline.stage_bible(cfg, llm, project)
        pipeline.stage_write(cfg, llm, project)
        pipeline.stage_polish(cfg, llm, project)

        # 字数门终审：最终成稿越界（不足或超标）。
        # 借鉴 openwrite B06「字数门保稿 + 标记回流，而不是整篇淘汰」：
        # 「字数不达标」是可修复的质量信号，「稿子不能用」才该淘汰。旧实现一律
        # rename 到 rejected/，正文和原因都离开工作台，token 白烧且看板看不见。
        # 现在默认**保稿 + 机器可读标记**（report.json.rejected=false +
        # word_gate.length_out_of_range=true + meta.gate_status），只有显式
        # --reject-overflow 才移动目录。看板/复盘都能查到，人再决定去留。
        _rep = project.load_json("report.json") or {}
        wg = _rep.get("word_gate") or {}
        _floor = int(cfg["review"].get("word_floor", 8000))
        _ceil = int(cfg["review"].get("word_ceiling", 12000))
        _fw = int(_rep.get("final_words") or 0)
        _why = ""
        if _fw < _floor:
            _why = f"成稿仅 {_fw} 字（下限 {_floor}）"
        elif _fw > _ceil:
            _why = f"成稿 {_fw} 字（上限 {_ceil}）"
        elif wg.get("gate") == "maxed_words_high":
            _why = f"字数门超标打满（{wg.get('total')} 字，上限 {_ceil}）"
        if _why:
            # 无论保稿还是淘汰，先把可读标记写进 report（复盘/看板的数据源）
            _rep["rejected"] = False
            _rep["reject_reason"] = "CHAPTER_LENGTH_OUT_OF_RANGE"
            _rep["reject_detail"] = _why
            _rep.setdefault("word_gate", {})["length_out_of_range"] = True
            _rep["word_gate"]["floor"] = _floor
            _rep["word_gate"]["ceiling"] = _ceil
            _rep["word_gate"]["words"] = _fw
            project.save_json("report.json", _rep)
            _meta = project.meta
            _meta["gate_status"] = "length_out_of_range"
            _meta["gate_reason"] = _why
            project.save_meta(_meta)
            from . import runlog as _rl

            if getattr(args, "reject_overflow", False):
                rejected = ws / "rejected"
                rejected.mkdir(exist_ok=True)
                dest = rejected / project.root.name
                n = 0
                while dest.exists():
                    n += 1
                    dest = rejected / f"{project.root.name}_{n}"
                _rep["rejected"] = True
                project.save_json("report.json", _rep)
                project.root.rename(dest)
                _rl.event("word_gate", f"{_why}，已淘汰出工作台 → rejected/{dest.name}", level="warn")
                print(f"[gate] ✘ {_why}，已淘汰 → rejected/{dest.name}")
                line.append({"dir": direction or "—", "project": dest.name,
                             "title": "（字数越界已淘汰）", "words": wg.get("total"),
                             "publish": "淘汰", "reads": None, "improve": ""})
                continue
            # 默认保稿：正文留在 chapters/，项目留在工作台，只打标记
            _rl.event("word_gate", f"{_why}，已保稿并标记 length_out_of_range（未淘汰）", level="warn")
            print(f"[gate] ⚠ {_why}，已保稿并标记 length_out_of_range（加 --reject-overflow 才淘汰）")

        # 语言未达投稿线：字数合法但四类语言病超标，同样是可修复信号——
        # 不淘汰（正文还在，跑 `tools/manuscript_audit.py` 能给出逐项返修建议），
        # 但必须让看板/复盘看得见，否则「99617 字事故」那类静默通过会重演。
        if not _why:
            _ml = _rep.get("market_lang") or {}
            if _ml.get("pass") is False:
                _fails = "；".join(_ml.get("fails") or []) or "（未列出明细）"
                _rep["reject_reason"] = "LANG_BELOW_MARKET"
                _rep["reject_detail"] = _fails
                project.save_json("report.json", _rep)
                _meta = project.meta
                _meta["gate_status"] = "lang_below_market"
                _meta["gate_reason"] = _fails
                project.save_meta(_meta)
                _rl.event("market_lang", f"语言未达投稿线：{_fails}", level="warn")
                print(f"[gate] ⚠ 语言未达投稿线：{_fails}")

        # 投稿判定（单一权威源 studio/verdict.py）：pipeline 已把 platform_check /
        # market_lang / review_score 合成 report["submission"]。这里只负责把结论
        # 落到 meta 上给人看——不再自己重算一遍判据（三处口径打架的根源）。
        try:
            from . import verdict as _vd

            _sub = (_rep.get("submission")
                    or _vd.submission(platform_check=_rep.get("platform_check"),
                                      market_lang=_rep.get("market_lang"),
                                      review_score=_rep.get("review_score"),
                                      words=_rep.get("final_words"),
                                      recheck_pass=(_rep.get("lang_recheck") or {}).get("pass"),
                                      title_score=_rep.get("title_score")))
            _meta = project.meta
            _meta["submission_grade"] = _sub.get("grade")
            _meta["submission_ready"] = bool(_sub.get("ready"))
            project.save_meta(_meta)
            print(f"[gate] {_vd.brief(_sub)}")
        except Exception as _se:  # noqa: BLE001
            print(f"[gate] ⚠ 投稿判定跳过：{type(_se).__name__}: {_se}")

        story = {"dir": direction or "—", "project": project.root.name,
                 "title": (project.load_json("pitch.json") or {}).get("title", ""),
                 "words": (project.load_json("report.json") or {}).get("final_words", 0),
                 "publish": "", "reads": None, "improve": ""}
        # 发布
        if publish_mode != "none":
            pub.publish(cfg, project, mode=publish_mode)
            story["publish"] = publish_mode
        # 数据回流
        if data_source != "none":
            try:
                snap = an.fetch_stats(cfg, project.root.name, source=data_source)
                story["reads"] = (snap.get("metrics") or {}).get("reads")
            except Exception as e:  # noqa: BLE001
                story["reads"] = f"失败({e})"
        # 复盘
        if with_improve and data_source != "none":
            try:
                imp = pipeline.stage_improve(cfg, llm, project)
                story["improve"] = f"{len(imp.get('lessons', []))} 条教训"
            except Exception as e:  # noqa: BLE001
                story["improve"] = f"失败({e})"
        line.append(story)

    # 制作报告
    rep = ["=== 全流程制作报告 ==="]
    for x in line:
        rep.append(f"《{x['title']}》 [{x['dir']}] {x['words']}字 | 发布:{x['publish'] or '未'} | "
                   f"阅读:{x['reads'] if x['reads'] is not None else '—'} | {x['improve']}")
    report = "\n".join(rep)
    print("\n" + report)
    (ws / f"production_{time.strftime('%Y%m%d_%H%M%S')}.txt").write_text(report, encoding="utf-8")
    print("\n=== 生产完成 ===")


def cmd_project(cfg: dict, args):
    from . import workbench as wb

    if args.action == "list":
        for x in wb.list_projects_brief(cfg):
            mark = "✓" if x["has_report"] else " "
            print(f"  [{mark}] {x['name']}")
        return
    if not args.project:
        raise SystemExit("用法：project delete --project <关键字>（删除前请确认）")
    proj = find_project(cfg["studio"]["workspace_abs"], args.project)
    sure = input(f"彻底删除 {proj.root.name}？（输入 yes 确认）：").strip().lower()
    if sure != "yes":
        print("已取消")
        return
    res = wb.delete_project(cfg, proj.root.name)
    print("[project] 已删除" if res.get("ok") else f"[project] 失败：{res}")


def cmd_schedule(cfg: dict, args):
    if getattr(args, "config", None):   # 供 scheduler 子进程透传同一份配置
        cfg["_config_file"] = str(args.config)
    from . import scheduler as sch

    if args.action == "add":
        if args.time:
            trigger = {"type": "daily", "time": args.time}
        elif args.interval:
            trigger = {"type": "interval", "minutes": args.interval}
        else:
            raise SystemExit("需要 --time HH:MM 或 --interval 分钟")
        dirs = [d.strip() for d in args.directions.split(",")] if args.directions else []
        srcs = [x.strip() for x in args.sources.split(",")] if args.sources else []
        job = sch.new_job(cfg, args.name or "定时撰写", trigger, count=args.count,
                          directions=dirs, words=getattr(args, "words", None), sources=srcs,
                          provider=getattr(args, "provider", None) or "")
        jobs = sch.load_schedules(cfg)
        jobs.append(job)
        sch.save_schedules(cfg, jobs)
        print(f"[schedule] 已创建 {job['id']}：{job['name']} | {sch.next_run_text(job)} | "
              f"{job['count']} 篇 | 题材：{'、'.join(job['directions']) or '跟随热点'}")
        print("启动调度器：python -m studio scheduler")
        return
    jobs = sch.load_schedules(cfg)
    if args.action == "list":
        if not jobs:
            print("暂无定时任务。创建：python -m studio schedule add --time 20:00 --count 2 --directions 追妻火葬场")
            return
        for j in jobs:
            status = "开" if j.get("enabled") else "关"
            print(f"  [{j['id']}] {j['name']} | {sch.next_run_text(j)} | {j.get('count', 1)}篇 | "
                  f"题材：{'、'.join(j.get('directions', [])) or '跟随热点'} | "
                  f"{j.get('words') or '默认'}字 | 开关:{status} | 上次：{j.get('last_run', '从未')} {j.get('last_result', '')}")
        return
    if not args.job:
        raise SystemExit("需要 --job 任务ID（list 查看）")
    job = next((j for j in jobs if j.get("id") == args.job or j.get("name") == args.job), None)
    if job is None:
        raise SystemExit(f"未找到任务：{args.job}")
    if args.action == "remove":
        sch.save_schedules(cfg, [j for j in jobs if j.get("id") != job["id"]])
        print("[schedule] 已删除")
    elif args.action == "toggle":
        job["enabled"] = not job.get("enabled", True)
        sch.save_schedules(cfg, jobs)
        print(f"[schedule] {job['name']} 已{'开启' if job['enabled'] else '关闭'}")
    elif args.action == "run":
        res = sch.run_job(cfg, job)
        print(f"[schedule] 执行完成：{res.get('status')}")


def cmd_scheduler(cfg: dict, args):
    from . import scheduler as sch

    sch.loop(cfg, interval_sec=args.interval, once=args.once)


def cmd_deconstruct(cfg: dict, args):
    from . import deconstruct as dec
    from .llm import get_llm, resolve_llm

    llm = resolve_llm(cfg, get_llm(cfg), "planner")
    p = Path(args.path)
    if p.is_dir():
        res = dec.deconstruct_dir(cfg, llm, p, max_sections=args.max_sections,
                                  per_call=args.per_call)
        print(f"[decon] 批量完成：{len(res)} 本")
    else:
        dec.deconstruct_file(cfg, llm, p, title=args.title or "",
                             max_sections=args.max_sections, per_call=args.per_call)


def cmd_fetch(cfg: dict, args):
    from . import fetch_novel as fn

    srcs = fn.sources_of(cfg)
    if args.list:
        print("已配置的小说抓取源（config novel_fetch.sources 可自定义增删）：")
        for s in srcs:
            print(f"  - {s.get('name')}（type={s.get('type')}，base={s.get('base')}）")
        return
    from .llm import get_llm, resolve_llm

    llm = resolve_llm(cfg, get_llm(cfg), "planner")
    res = fn.fetch_and_decon(cfg, llm, source_name=args.source or "", top=args.top,
                             max_chapters=args.max_chapters, do_decon=not args.no_decon)
    if not res.get("ok"):
        print(f"[fetch] 未抓到可用书目：{res.get('error', '详见上方日志')}")
        return
    print(f"[fetch] 完成：源「{res['source']}」共 {len(res['books'])} 本")
    for b in res["books"]:
        print(f"  《{b['title']}》{b['chars']} 字 → {b['path']}")


def cmd_rag(cfg: dict, args):
    from . import rag

    if args.action == "rebuild":
        n = rag.build(cfg, force=True)
        print(f"[rag] 索引重建完成：{n} 文档")
        return
    hits = rag.search(cfg, args.q or "", top_k=args.top)
    if not hits:
        print("无结果")
        return
    for h in hits:
        print(f"  [{h['source']}·{h['type']}|{h.get('direction','')}] ({h['score']}) {h['text'][:70]}")


def cmd_publish(cfg: dict, args):
    from . import publish as pub

    if args.probe:
        for k, v in pub.probe(cfg).items():
            print(f"  {k}: {v}")
        return
    cookie = None
    if args.cookie_file:
        cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
        cfg["publish"]["cookie"] = cookie
    project = _pick_project_for_cmd(cfg, args)
    print(f"[project] {project.root.name}")
    rec = pub.publish(cfg, project, platform=getattr(args, "platform", None), mode=args.mode, cookie=cookie)
    print(f"[publish] 状态：{rec['status']}  {rec.get('note', '')}")


def cmd_analytics(cfg: dict, args):
    from . import analytics as an

    if args.action == "fetch":
        if args.project:
            project = find_project(cfg["studio"]["workspace_abs"], args.project)
            snap = an.fetch_stats(cfg, project.root.name, source=args.source)
            print(json.dumps(snap.get("metrics", {}), ensure_ascii=False, indent=1))
        else:
            base = Path(cfg["studio"]["workspace_abs"]) / "projects"
            if not base.exists():
                raise SystemExit("没有项目")
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "publish.json").exists():
                    an.fetch_stats(cfg, d.name, source=args.source)
        return
    if args.action == "import":
        if not args.file or not args.project:
            raise SystemExit("用法：analytics import <文件> --project <关键字>")
        project = find_project(cfg["studio"]["workspace_abs"], args.project)
        an.import_stats(cfg, project.root.name, args.file)
        return
    # show
    if args.project:
        project = find_project(cfg["studio"]["workspace_abs"], args.project)
        snaps = an.load_series(cfg, project.root.name)
        trend = an.compute_trend(snaps)
        print(f"== {project.root.name} ==")
        print(an.summary_text(trend))
        return
    base = Path(cfg["studio"]["workspace_abs"]) / "projects"
    if not base.exists():
        raise SystemExit("没有项目")
    shown = 0
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        snaps = an.load_series(cfg, d.name)
        if not snaps:
            continue
        shown += 1
        print(f"== {d.name} ==")
        print(an.summary_text(an.compute_trend(snaps)))
        print()
    if shown == 0:
        print("（无数据）没有任何项目有阅读数据快照。"
              "可用 analytics fetch --project <关键字> --source mock 先回填演示数据。")


def cmd_improve(cfg: dict, args):
    llm = get_llm(cfg)
    project = _pick_project_for_cmd(cfg, args)
    print(f"[project] {project.root.name}")
    data = pipeline.stage_improve(cfg, llm, project)
    for l in data.get("lessons", []):
        print(f"  [{l.get('dimension')}] {l.get('action')}")
    print(f"  下一个假设：{data.get('next_try', '')}")


def cmd_review(cfg: dict, args):
    """查看/重算证据化评审卡。

    两种模式：
      --recompute  用已落盘的 reviews/chNN.json（主编原始 verdict + 终稿正文）
                   本地重算，零模型调用——改了评分卡权重/阈值后不必重跑整篇；
      默认         直接读 report.json 里流水线写入的 rubric，快速看一眼。
    """
    import json as _json

    from . import review as review_mod

    project = _pick_project_for_cmd(cfg, args)
    print(f"[project] {project.root.name}")

    chs = project.read_chapters()
    # 重算时的目标字数：优先用 reviews 里记录的（流水线写入，最准确），
    # 其次用大纲的 words_target，最后才退化成 0（=未评估，而不是默认达标）。
    outline = project.load_json("outline.json") or {}
    ch_target = {c.get("no"): c.get("words_target") for c in outline.get("chapters", [])}
    if args.chapter:
        chs = [c for c in chs if c["no"] == args.chapter]
        if not chs:
            print(f"（无第 {args.chapter} 章）")
            return

    out = []
    for ch in chs:
        rev = project.load_json(f"ch{ch['no']:02d}.json", subdir="reviews") or {}
        rubric = rev.get("rubric")
        if args.recompute:
            tgt = int(rev.get("target_words") or ch_target.get(ch["no"]) or 0)
            # 重算时用落盘的终稿正文，保证与"最终交付的东西"一致
            rubric = review_mod.build(
                cfg, rev.get("review_verdict") or {}, ch["text"],
                target_words=tgt,
                direction=str((project.load_json("concept.json") or {}).get("direction") or ""),
            )
            rev["rubric"] = rubric
            rev["target_words"] = tgt
            rev.update(review_mod.summary_fields(rubric))
            project.save_json(f"ch{ch['no']:02d}.json", rev, subdir="reviews")
        out.append({"no": ch["no"], "title": rev.get("title") or "", "rubric": rubric})

    if args.json:
        print(_json.dumps(out, ensure_ascii=False, indent=2))
        return

    if not any(o["rubric"] for o in out):
        print("（该项目没有评审卡数据：可能是历史项目，或跑的时候 --no-review 关掉了门禁）")
        print("提示：`studio review --recompute` 只能重算已有主编结论的章节。")
        return

    tot_q = tot_c = 0
    for o in out:
        print(f"\n== 第{o['no']:02d}章 {o['title']}")
        print(review_mod.render_text(o["rubric"]))
        r = o["rubric"] or {}
        if r.get("quality_score") is not None:
            tot_q += r["quality_score"]
            tot_c += 1
    if tot_c:
        print(f"\n[汇总] {tot_c} 章平均质量分 {tot_q / tot_c:.1f}")


def cmd_dashboard(cfg: dict, args):
    import webbrowser

    from . import dashboard as dash

    if args.export:
        path = dash.export_dashboard(cfg, args.export)
        print(f"[dashboard] 静态看板已导出：{path}")
        print("双击用浏览器打开即可（数据已内嵌，离线可看）")
        if not args.no_open:
            webbrowser.open(path.resolve().as_uri())
        return
    dash.serve(cfg, args.port, open_browser=not args.no_open, config_file=getattr(args, "config", None))


def _check_endpoint(tag: str, llm_cfg: dict):
    """探测一个 OpenAI 兼容端点：列模型 + 小生成。"""
    url = llm_cfg.get("base_url", "")
    probe = OpenAICompatLLM({**llm_cfg, "retries": 1, "timeout": 15, "stream": False})
    try:
        r = probe.session.get(f"{url.rstrip('/')}/models", timeout=8)
        models = [m.get("id", "?") for m in (r.json().get("data") or [])][:5]
        print(f"  {tag}: {url} 模型[{len(models)}+] {'，'.join(models[:3])}")
    except Exception as e:  # noqa: BLE001
        print(f"  {tag}: {url} 不可达（{type(e).__name__}）")
        return
    if not llm_cfg.get("api_key"):
        return  # 本地端点不消耗生成
    try:
        t0 = time.time()
        probe2 = OpenAICompatLLM({**llm_cfg, "retries": 1, "timeout": 30, "stream": False, "max_tokens": 16})
        r = probe2.chat("回声机", "只回复两个字：正常")
        print(f"      生成 OK（{time.time()-t0:.1f}s）：{r.strip()[:16]}")
    except Exception as e:  # noqa: BLE001
        print(f"      生成失败：{str(e)[:80]}")


def cmd_verify(cfg: dict, args):
    """项目自检：一条命令跑完所有离线检查（编译/静态/契约/漂移/功能矩阵[/冒烟]）。"""
    from . import verify as verify_mod

    argv = []
    if getattr(args, "full", False):
        argv.append("--full")
    if getattr(args, "json", False):
        argv.append("--json")
    only = getattr(args, "only", None)
    if only:
        argv.append(f"--only={only}")
    code = verify_mod.main(argv)
    if code != 0:
        raise SystemExit(code)


def cmd_drafts(cfg: dict, args):
    """断点保稿：列出/查看/丢弃章节草稿。"""
    from . import drafts as drafts_mod

    ws = cfg["studio"]["workspace_abs"]
    project = find_project(ws, args.project) if args.project else latest_project(ws)
    if project is None:
        raise SystemExit("没有找到项目。")
    if args.drop:
        if not args.chapter:
            raise SystemExit("--drop 需要同时指定 --chapter")
        drafts_mod.drop_draft(project, args.chapter)
        print(f"已丢弃 ch{args.chapter:02d} 草稿")
        return
    if args.show:
        if not args.chapter:
            raise SystemExit("--show 需要同时指定 --chapter")
        text = drafts_mod.restore(project, args.chapter)
        if text is None:
            print(f"ch{args.chapter:02d} 没有草稿")
        else:
            print(text)
        return
    items = drafts_mod.list_recoverable(project)
    if args.chapter:
        items = [d for d in items if d["no"] == args.chapter]
    if getattr(args, "json", False):
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return
    if not items:
        print(f"[drafts] {project.root.name}：没有可恢复草稿（全部已提交或从未中断）")
        return
    print(f"[drafts] {project.root.name}：{len(items)} 条可恢复记录")
    for d in items:
        print("  " + drafts_mod.summary_line(d))
    print("  提示：`drafts --chapter N --show` 查看正文；`--drop` 丢弃；`continue` 会重过门禁。")


def cmd_check(cfg: dict, args):
    print("== LLM 主配置 ==")
    llm_cfg = cfg["llm"]
    print(f"provider={llm_cfg.get('provider')} model={llm_cfg.get('model')} stream={llm_cfg.get('stream', 'auto')}")
    if llm_cfg.get("provider") == "mock":
        print("  mock 模式（离线占位）")
    elif llm_cfg.get("api_key") or llm_cfg.get("provider") in ("local", "api", "openai", "openai_compat"):
        _check_endpoint("主", llm_cfg)
    else:
        print("  未配置 api_key，调用时将降级 MockLLM")
    roles = cfg.get("llm_roles") or {}
    if roles:
        print("== 角色路由 ==")
        for role, rcfg in roles.items():
            merged = {**llm_cfg, **rcfg}
            if rcfg.get("provider") == "local":
                merged["api_key"] = ""
            _check_endpoint(f"{role:<9}", merged)
    print("\n== 资产完整性 ==")
    from .config import ROOT, prompt_path

    for t in ["mine", "pitch", "outline", "bible", "draft", "rewrite", "review", "reader_score", "kb_distill", "polish_fix", "polish_final"]:
        p = prompt_path(t)
        try:
            txt = p.read_text(encoding="utf-8")
            good = p.exists() and "===SYSTEM===" in txt and "<!--TASK:" in txt
        except OSError:
            good = False
        print(f"  {'OK ' if good else '缺失'} prompts/{t}.md")
    for d in ["data/tropes.json", "data/platforms.json", "data/readers.json", "data/lang_rules.json", "data/character_templates.json"]:
        try:
            json.loads((ROOT / d).read_text(encoding="utf-8"))
            print(f"  OK  {d}")
        except Exception as e:  # noqa: BLE001
            print(f"  坏  {d}: {e}")
    print("\n== 热点源 ==")
    from .hotspots import sources as hs

    if cfg["studio"].get("weibo_cookie"):
        hs.WEIBO_COOKIE = cfg["studio"]["weibo_cookie"]
    srcs = args.source.split(",") if args.source else ["weibo", "zhihu", "baidu", "douyin", "toutiao", "bilibili"]
    for s in srcs:
        if s in hotspots.ADAPTERS:
            try:
                items = hotspots.ADAPTERS[s](3)
                ok2 = "OK " if items else "空 "
                sample = items[0].title[:22] if items else ""
                print(f"  {ok2} {s:<10} {sample}")
            except Exception as e:  # noqa: BLE001
                print(f"  失败 {s:<10} {type(e).__name__}: {str(e)[:60]}")
        else:
            print(f"  ??  {s}（未知来源）")


def _summary(project):
    pitch = project.load_json("pitch.json") or {}
    report = project.load_json("report.json") or {}
    review = report.get("review") or {}
    print("\n" + "=" * 56)
    print(f"完成！《{pitch.get('title', '')}》")
    print(f"项目目录：{project.root}")
    print(f"成稿：{project.root / 'final' / '成稿.md'}")
    print(f"字数：{report.get('final_words', '?')} | 平台达标：{report.get('platform_check', {}).get('pass')}")
    print(f"体检评分：{report.get('score', '?')}")
    chapters_rev = review.get("chapters") or {}
    if chapters_rev:
        hybrid = any(isinstance(v, dict) and str(v.get("gate", "")).startswith(("hybrid", "skipped_hybrid"))
                     for v in chapters_rev.values())
        if hybrid:
            print("章节门禁：hybrid 模式（单 writer 同上下文续写，**未做逐章门禁**）")
        else:
            gated = sum(1 for v in chapters_rev.values() if isinstance(v, dict) and v.get("gate") == "pass")
            print(f"章节门禁：{gated}/{len(chapters_rev)} 通过（每章轮次详见 reviews/ 与 report.json）")
    full = review.get("full_story")
    if full:
        print(f"全稿读者团均分：{full.get('reader_avg')}（阈值 {full.get('threshold')}，{full.get('gate')}）")
    usage = report.get("usage") or {}
    if usage:
        est = "（部分为估算）" if any(v.get("est") for v in usage.values()) else ""
        print(f"用量：{sum(v['calls'] for v in usage.values())} 次调用，"
              f"输出 {sum(v['completion_tokens'] for v in usage.values())} tokens {est}")
        for k, v in usage.items():
            print(f"  {k}: {v['calls']} 次 / 输出 {v['completion_tokens']} tokens")


def _persist_banned_exempt(cfg: dict, project) -> None:
    """选题自带的禁用道具放行清单落到 project.meta，供 stage/看板/报告读取。

    为什么单独写一个函数：`cmd_run` 和 `cmd_produce` 两条入口都要这一步，
    之前是两段复制的代码，其中一段漏传了 save_meta 的参数，直接 TypeError
    崩在"刚建好项目"的位置（实测 0.0s 失败，一行正文都没写）。
    """
    hit = list(cfg.get("_banned_exempt") or [])
    if not hit:
        return
    # 坑：Project.meta 是 property，**每次访问都重新读盘**。
    # `project.meta["k"] = v` 改的是一份临时 dict，紧接着 `save_meta(project.meta)`
    # 又读了一份干净的，改动凭空消失（实测放行清单从未落盘，报告里恒为 None）。
    project.update_meta(banned_exempt=hit)


def main(argv=None):
    _setup_stdout()
    args = build_parser().parse_args(argv)
    cfg = _apply(load_config(getattr(args, "config", None)), args)
    from . import runlog

    runlog.start(sys.argv[1:] if argv is None else list(argv),
                 cfg["studio"]["workspace_abs"],
                 enabled=cfg.get("logging", {}).get("enabled", True))
    try:
        {
            "hot": cmd_hot,
            "mine": cmd_mine,
            "run": cmd_run,
            "continue": cmd_continue,
            "tropes": cmd_tropes,
            "fetch": cmd_fetch,
            "kb": cmd_knowledge_base,
            "local": cmd_local,
            "material": cmd_material,
            "memory": cmd_memory,
            "produce": cmd_produce,
            "project": cmd_project,
            "schedule": cmd_schedule,
            "scheduler": cmd_scheduler,
            "deconstruct": cmd_deconstruct,
            "rag": cmd_rag,
            "publish": cmd_publish,
            "analytics": cmd_analytics,
            "improve": cmd_improve,
            "review": cmd_review,
            "dashboard": cmd_dashboard,
            "check": cmd_check,
            "drafts": cmd_drafts,
            "verify": cmd_verify,
        }[args.cmd](cfg, args)
        return 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        from .errors import StudioError

        err = StudioError("INTERRUPTED", "运行被 Ctrl-C 中断")
        print("\n" + str(err))
        runlog.event("interrupt", str(err), level="warn")
        return 130
    except Exception as e:  # noqa: BLE001
        # 统一错误封套：稳定错误码 + request_id + 四元组诊断（studio/errors.py）。
        # 用户贴一行就能定位；runlog 里也能按 request_id 串起整轮。
        from .errors import StudioError, classify

        if isinstance(e, StudioError):
            err = e
        else:
            err = StudioError(classify(e), f"{type(e).__name__}: {e}",
                              where=f"cmd={getattr(args, 'cmd', '?')}",
                              found=str(e)[:300], cause=e)
        print("\n" + str(err))
        try:
            runlog.event("error", str(err).replace("\n", " | "), level="error")
        except Exception:  # noqa: BLE001
            pass
        if getattr(args, "debug", False):
            import traceback

            traceback.print_exc()
        return 1
    finally:
        runlog.end()


if __name__ == "__main__":
    main()
