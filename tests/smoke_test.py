# -*- coding: utf-8 -*-
"""冒烟测试：MockLLM 全流程（含审查/读者双门禁循环）+ 知识库降级路径 + 工具函数。

运行：python tests/smoke_test.py
不依赖网络与 API Key，产物写入临时目录。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import pipeline
from studio.config import load_config
from studio.llm import MockLLM
from studio.store import Project
from studio.util import count_words_zh, extract_json


def main() -> int:
    # 工具函数
    assert count_words_zh("你好 世界\nx") == 5
    assert extract_json('废话 {"a": [1,2]} 废话') == {"a": [1, 2]}
    assert extract_json('```json\n{"b": 1,}\n```') == {"b": 1}
    assert extract_json("{'c': '单引号'}") == {"c": "单引号"}
    print("[ok] 工具函数")

    cfg = load_config()
    # 冒烟测试固定走 agent 模式（逐章四道门禁）；config.yaml 可能被用户设为 hybrid，
    # 那会跳过逐章审查，导致下方 reviews/ch01.json 断言不成立。
    cfg.setdefault("pipeline", {})["mode"] = "agent"
    # 测试验证的是「mock 首轮 fail → 次轮 pass」的门禁闭环逻辑，
    # 与生产环境为省 token 调低的轮次无关：这里显式恢复验证所需轮次。
    rcfg = cfg.setdefault("review", {})
    rcfg["max_reader_rounds"] = 2
    rcfg.setdefault("max_emotion_rounds", 2)
    # 临时目录放项目盘（E:），不用系统 C 盘 Temp；测试结束自动清理
    tmp = Path("workspace/.smoke_tmp").resolve()
    import shutil as _sh

    if tmp.exists():
        _sh.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    cfg["studio"]["workspace_abs"] = str(tmp)
    cfg["llm"]["provider"] = "mock"
    llm = MockLLM()

    # 资产完整性
    tropes = pipeline.load_tropes()
    assert len(tropes["directions"]) >= 15, "梗库方向过少"
    assert pipeline.match_direction("婆婆把陪嫁房过户给小叔子") == "婆媳大战"
    readers = json.loads((Path("data") / "readers.json").read_text(encoding="utf-8"))["readers"]
    assert len(readers) >= 4
    print(f"[ok] 资产：{len(tropes['directions'])} 个题材方向，{len(readers)} 个读者画像")

    # knowledge 模块：本地问句生成 + 离线降级构建
    from studio import knowledge

    knowledge.KB_DIR = tmp / "kb"  # 重定向，避免污染真实知识库
    qs = knowledge.gen_queries("彩礼纠纷")
    assert len(qs) >= 3
    knowledge.collect_material = lambda cfg_, d: []  # 模拟断网
    card = knowledge.build_card(cfg, llm, "彩礼纠纷", force=True)
    assert card["fallback"] is True
    dg = knowledge.digest(card, 500)
    assert "要诀" in dg and len(dg) <= 520
    print("[ok] 知识库：问句生成 + 断网降级构建 + digest 压缩")

    # 语言质检门：确定性扫描（零 LLM 成本）
    from studio.langqa import scan as lang_scan

    bad = "他眼中闪过一丝复杂的情绪。只见她站在门口。她很紧张，手心全是汗。"
    r_bad = lang_scan(bad)
    assert not r_bad["pass"] and len(r_bad["hits"]) >= 3, r_bad
    good = "王秀兰把判决书折成四折，塞进围裙口袋。院门口挤了七八个人，陈美凤扒开人群冲进来，一把揪住她的袖子。"
    assert lang_scan(good)["pass"]
    print("[ok] 语言质检门：黑名单/句式词频扫描（坏文本拦截，好文本放行）")

    # 小节切分与节头规范化（回归：失控扩写事故）
    # 事故链：正文里「3 年了…」「4月5日夜里…」这类数字开头的句子被当成小节头 →
    # 整稿误切 400+ 节 → 按节号批量回写 → 成稿从 9000 字膨胀到 9.9 万字。
    assert pipeline._SEC_HEAD.match("01"), "纯数字节头必须识别"
    assert pipeline._SEC_HEAD.match("01 判决书贴上村口"), "带标题节头必须识别"
    assert pipeline._SEC_HEAD.match("## 02 花轿上睁眼，我重生了"), "markdown 节头必须识别"
    for prose in ("3 年了，她一直没回来。", "4月5日夜里十一点，我蹲在化肥垛后头。",
                  "10比2。记分牌上这两个数字还亮着。", "2 emotional amplification.",
                  "00:00至05:00，住院部东侧电梯禁止乘坐。", "5 万块钱，她数了三遍。"):
        assert not pipeline._SEC_HEAD.match(prose), f"正文不应被识别为节头：{prose}"
    # 节数上限守卫：>_MAX_SECTIONS 判定识别失效，返回空（调用方退回整稿模式）
    over = "\n\n".join(f"{i:02d}\n\n正文{i}" for i in range(1, pipeline._MAX_SECTIONS + 5))
    assert pipeline._split_full_sections(over) == [], "超上限应拒绝逐节处理"
    normal = "\n\n".join(f"{i:02d} 标题{i}\n\n正文第{i}节。" for i in range(1, 5))
    _secs = pipeline._split_full_sections(normal)
    assert [s["no"] for s in _secs] == [1, 2, 3, 4], _secs
    # 重复/回退节号 → 顺延为唯一递增（否则按节号回写会污染多节）
    dup = "01 甲\n\nA。\n\n01 乙\n\nB。\n\n02 丙\n\nC。"
    assert [s["no"] for s in pipeline._split_full_sections(dup)] == [1, 2, 3]
    # _final_clean：节头单调唯一 + 保留/去除标题两种平台契约
    raw = "01\n\n甲。\n\n02\n\n乙。\n\n02\n\n丙。\n\n01\n\n丁。"
    clean = pipeline._final_clean(raw)
    heads = [_ln.strip() for _ln in clean.splitlines() if pipeline._SEC_HEAD.match(_ln.strip())]
    assert heads == ["01", "02", "03", "04"], heads
    keep = pipeline._final_clean("01 判决书贴上村口\n\n甲。", keep_titles=True)
    assert keep.splitlines()[0].strip() == "01 判决书贴上村口", keep
    nocheck = pipeline._final_clean("01 判决书贴上村口\n\n甲。")
    assert nocheck.splitlines()[0].strip() == "01", nocheck
    print("[ok] 小节切分与节头规范化：误切守卫 / 节号唯一递增 / 平台标题契约")

    # idea 模式全流程（含双门禁）
    project = Project(tmp / "projects" / "proj1")  # 与真实布局一致：workspace/projects/ 下
    concept = pipeline.make_idea_concept("退婚被索天价彩礼，女主用借条反杀")
    assert concept["direction"] == "彩礼纠纷"
    project.save_json("concept.json", concept)
    pipeline.stage_kb(cfg, llm, project)  # 知识卡已有 → 复用
    pipeline.stage_pitch(cfg, llm, project)
    pipeline.stage_lead(cfg, llm, project)
    pipeline.stage_outline(cfg, llm, project)
    pipeline.stage_bible(cfg, llm, project)
    pipeline.stage_write(cfg, llm, project)
    report = pipeline.stage_polish(cfg, llm, project, skip_final_llm=True)

    chapters = project.read_chapters()
    assert len(chapters) == 6, f"应有6章，实际{len(chapters)}"
    assert (project.root / "final" / "成稿.txt").exists()
    lead_rec = project.load_json("lead.json")
    assert lead_rec and lead_rec["words"] > 30, "导语应生成"
    final_txt = (project.root / "final" / "成稿.txt").read_text(encoding="utf-8")
    # 知乎盐选成稿格式：导语 + 纯数字节头（`_final_clean` 会统一节头为「NN」，
    # 平台 style 明确要求「小节序号用纯阿拉伯数字，不加小节标题」）。
    import re as _re2

    assert "导语：" in final_txt, "成稿应包含导语"
    assert _re2.search(r"(?m)^01$", final_txt), "成稿应包含 01 编号小节"

    # 对话引号契约：全平台统一「」，且 md 与 txt 必须一致。
    # 回归事故：zhihu_format 只作用于成稿.md，投稿用的成稿.txt 保留 “”，
    # 出现"导语用「」、正文用 “”"，实测 146 对未转换。
    from studio.publish import normalize_quotes, zhihu_format

    _q = "他说：“我不填了。”然后走了。\n\n她问：\"真的？\"\n\n“跨\n行对话”"
    _qn = normalize_quotes(_q)
    assert "“" not in _qn and "”" not in _qn and '"' not in _qn, _qn
    assert _qn.count("「") == 3 and _qn.count("」") == 3, _qn
    assert normalize_quotes(_qn) == _qn, "引号规范化必须幂等"
    assert zhihu_format("01 标题\n\n“话”") == "01\n\n「话」", zhihu_format("01 标题\n\n“话”")
    assert "“" not in final_txt and "”" not in final_txt, "成稿.txt 不得残留 “ ”"

    # 双门禁闭环：mock 首轮故意 fail / 压分 → 第二轮通过；四道门禁链按序生效
    rev1 = project.load_json("ch01.json", subdir="reviews")
    assert rev1["review_rounds"] == 2, rev1
    assert rev1["reader_rounds"] == 2 and rev1["reader_avg"] >= 7.5, rev1
    assert rev1["emotion"]["round"] == 2 and rev1["emotion"]["verdict"] == "pass", rev1
    assert rev1["emotion"]["dominant"] == "爽"

    # 最终 gate 必须与各子门结论一致（合并 bug 回归测试）。
    # 旧实现只看 rep_emo+rep_mid，rep_core 的 maxed_langqa/maxed_banprops 会被
    # review 的 pass 覆盖 —— 打满在最终状态里不可见（漏报）。
    # 这里断言不变式而非"mock 一定打满"：mock 素材已清理干净（不再故意含禁用道具），
    # 只要任一子门没通过，最终 gate 就不得是 pass。
    _sub_failed = (
        rev1.get("langqa_pass") is False
        or bool(rev1.get("ban_hits"))
        or (rev1.get("emotion") or {}).get("verdict") in ("fail", "maxed", "degraded")
        or (rev1.get("review_verdict") or {}).get("verdict") in ("fail", "degraded")
    )
    assert not (_sub_failed and rev1.get("gate") == "pass"), f"子门未通过却被合并成 pass：{rev1}"

    # 禁用道具门：用构造文本直接验证（确定性，不依赖 mock 素材是否含脏词）
    from studio.agents import scan_banned as _scan_banned

    _brep = _scan_banned("他把存折塞进棉裤夹层，又摸出那张欠条。", cfg)
    assert not _brep["pass"], _brep
    assert {h["term"] for h in _brep["hits"]} >= {"存折", "欠条"}, _brep
    assert _scan_banned("他把旧棉袄塞进棉裤夹层，又摸出那张婚书。", cfg)["pass"], "禁道具门误报"

    full_rev = report["review"]["full_story"]
    assert full_rev["reader_avg"] >= 7.5 and full_rev["gate"] == "pass", full_rev
    assert report.get("langqa_full") is not None
    print(f"[ok] 四道门禁：langqa✔ 主编第{rev1['review_rounds']}轮 读者{rev1['reader_avg']} 情绪{rev1['emotion']['emotion_score']}；全稿终评 {full_rev['reader_avg']}")

    # chat_json 修复重试：首次输出坏 JSON → 纠错提示重试 → 成功
    from studio.llm import BaseLLM, usage_summary

    class StubLLM(BaseLLM):
        def __init__(self):
            self.calls = 0

        def chat(self, system, user, json_mode=False):
            self.calls += 1
            if self.calls == 1:
                return "好的，这是我的分析：评分大概是8分吧"  # 无法解析
            assert "格式纠错" in user, "重试时应带纠错提示"
            return '{"verdict": "pass", "score": 8}'

    stub = StubLLM()
    assert stub.chat_json("s", "u") == {"verdict": "pass", "score": 8}
    assert stub.calls == 2
    print("[ok] chat_json 修复重试闭环")

    # 全稿组装不重复书名头部 + 成稿.txt 带标题
    full, stats = pipeline.assemble(project)
    assert not full.startswith("《"), "assemble 不应再包含书名头部"
    final_txt = (project.root / "final" / "成稿.txt").read_text(encoding="utf-8")
    assert final_txt.count("退婚之后") == 1, "书名应只出现一次"
    print("[ok] 成稿头部去重 + 投稿 txt 带标题行")

    # 断点续跑幂等
    pipeline.stage_write(cfg, llm, project)
    assert len(project.read_chapters()) == 6
    # polish 幂等：已有 report.json 时跳过
    before = (project.root / "report.json").read_text(encoding="utf-8")
    pipeline.stage_polish(cfg, llm, project)
    assert (project.root / "report.json").read_text(encoding="utf-8") == before
    print("[ok] 断点续跑幂等（含 polish 跳过）")

    # 热点去重
    from studio.hotspots import fetch_mock, dedupe

    items = fetch_mock(10)
    merged = dedupe(items + [items[0]])
    assert len(merged) == len(items)
    print(f"[ok] 热点 mock {len(items)} 条，去重正常")

    # 角色路由：writer/reviewer/panel/planner/light → 独立模型或回落主 llm
    from studio.llm import OpenAICompatLLM, get_llm, resolve_llm

    cfg2 = load_config()
    cfg2["llm"] = {
        "provider": "api", "base_url": "https://api.example.com/v1", "api_key": "sk-t",
        "model": "main-model", "temperature": 0.8, "max_tokens": 100, "timeout": 5, "retries": 1, "qps": 0,
    }
    cfg2["llm_roles"] = {
        "reviewer": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
    }
    from studio.llm import get_llm

    main = get_llm(cfg2)
    rev = resolve_llm(cfg2, main, "reviewer")
    assert main.model == "main-model"
    assert isinstance(rev, OpenAICompatLLM) and rev.model == "qwen3:8b" and rev.api_key == ""
    assert resolve_llm(cfg2, main, "panel") is main  # 未配置 → 沿用主 llm
    assert resolve_llm(cfg2, MockLLM(), "reviewer") is not rev  # mock 主导时忽略路由
    print("[ok] 角色路由：reviewer→本地 qwen3，panel 回落主 llm，mock 全局覆盖")

    # 并发安全回归：LLM 缓存双检 + 限速器持锁 + 流式降级只作用于单次调用。
    # 这三处是章级门禁 ThreadPoolExecutor 并行时的共享状态，历史上全是无锁/共享写：
    #   · get_llm 的 check-then-act → 同一角色造出两个实例（连接池重复、限速器分裂）
    #   · RateLimiter.wait 无锁 → 两线程读同一个 _last，限速形同虚设
    #   · 流式失败写 self.stream=False → 进程级永久关流式（且 pipeline 的
    #     stream_backup 备份到的已是 False，恢复无效）
    import threading as _th
    import time as _time

    from studio.llm import _LLM_CACHE, _LLM_LOCK
    from studio.llm import get_llm as _get_llm

    _LLM_CACHE.clear()
    cfg3 = {**cfg2, "llm_roles": {"panel": {"provider": "api", "base_url": "https://a.b/v1",
                                            "model": "panel-model"}}}
    _built: list = []

    def _grab():
        _built.append(_get_llm(cfg3, "panel"))

    _ts = [_th.Thread(target=_grab) for _ in range(8)]
    for _t in _ts:
        _t.start()
    for _t in _ts:
        _t.join()
    assert len({id(x) for x in _built}) == 1, f"get_llm 并发造出 {len({id(x) for x in _built})} 个实例"
    assert _LLM_CACHE["role:panel"] is _built[0]
    assert _LLM_LOCK is not None
    print("[ok] get_llm 并发双检：8 线程同角色只构造 1 个实例")

    # RateLimiter：8 线程各要一次，总耗时必须 >= 7 个间隔（串行化才算真的限速）
    from studio.util import RateLimiter as _RL

    _lim = _RL(min_interval=0.02)
    _lim._last = 0.0
    _t0 = _time.monotonic()
    _ts = [_th.Thread(target=_lim.wait) for _ in range(8)]
    for _t in _ts:
        _t.start()
    for _t in _ts:
        _t.join()
    _elapsed = _time.monotonic() - _t0
    # 阈值取 6 个间隔而非 7：串行时理论值恰为 7×20ms=140ms，浮点与线程调度抖动
    # 会让它偶尔落到 139.x ms。放宽到 6 个间隔仍有足够区分度——无锁时 8 线程
    # 并发、间隔恒不触发，总耗时约等于 1 个间隔（20ms 量级）。
    assert _elapsed >= 0.02 * 6, f"限速器未串行化：8 次调用仅耗时 {_elapsed:.3f}s"
    print(f"[ok] RateLimiter 持锁：8 线程 20ms 间隔实测 {_elapsed:.2f}s（无锁时应≈0.02s）")

    # 流式降级只影响本次调用：失败的调用结束后 self.stream 必须保持原值
    from studio.llm import LLMError as _LE
    from studio.llm import OpenAICompatLLM as _OCL

    _inst = _OCL({**cfg2["llm"], "retries": 0, "qps": 0})
    _inst.stream = True
    _calls = {"stream": 0}

    def _boom(*a, **k):
        _calls["stream"] += 1
        raise _LE("模拟流式失败")

    _inst._chat_stream = _boom  # type: ignore[method-assign]

    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "降级后的文本"}}], "usage": {}}

    _inst.session.post = lambda *a, **k: _Resp()  # type: ignore[method-assign]
    _out = _inst.chat("sys", "user")
    assert _out == "降级后的文本" and _calls["stream"] == 1
    assert _inst.stream is True, "流式降级污染了共享实例的 stream 开关"
    print("[ok] 流式降级只作用于单次调用，不污染进程级共享实例")

    # local 模块纯函数
    from studio import local as loc

    assert loc.pick_model(["qwen3:4b", "embed-x"], prefer=["4b"]) == "qwen3:4b"
    assert "OLLAMA_CONTEXT_LENGTH" in loc.setup_text()
    assert "llm_roles" in loc.snippet_text("http://x/v1", "big", "small")
    print("[ok] local 模块：模型挑选/落地方案/配置片段")

    # prompt 渲染完整性：自动发现每个模板的占位符并填值渲染。
    # 不再为每个模板硬编码 kw——模板新增占位符时测试自动跟随，避免"测试随生产漂移"
    # （历史缺陷：polish_fix 新增 WORD_STATS/NUMBER_DENSITY 后本测试 KeyError 崩溃）。
    import re as _re

    from studio.config import prompt_path
    from studio.prompts import load_prompt, render_prompt

    _AUTO_PH = {"MARKET_GUIDE", "GENRE_GUIDE"}  # render_prompt 会自动注入，无需传值
    _prompt_dir = Path(prompt_path("mine")).parent
    _checked = 0
    for _p in sorted(_prompt_dir.glob("*.md")):
        _task = _p.stem
        try:
            _sys, _usr = load_prompt(_task)  # 非 <!--TASK:--> 结构的文件会抛异常 → 跳过
        except Exception:  # noqa: BLE001
            continue
        _phs = set(_re.findall(r"\{\{(\w+)\}\}", _sys + _usr)) - _AUTO_PH
        s, u = render_prompt(_task, **{k: "x" for k in _phs})
        assert "<!--TASK:" in s, f"{_task} 渲染后丢失任务标记"
        assert "{{" not in u, f"{_task} 渲染后仍有未替换占位符"
        _checked += 1
    assert _checked >= 20, f"prompt 模板数量异常：{_checked}"
    print(f"[ok] prompt 模板渲染（{_checked} 个模板，占位符自动发现）")

    # 自动发布（mock）+ manual 投稿包
    from studio import publish as pub

    pub_rec = pub.publish(cfg, project, mode="mock")
    assert pub_rec["status"] == "published" and pub_rec["work_id"]
    pkg = pub.build_submission_package(cfg, project)
    assert (pkg / "01_正文.txt").exists() and (pkg / "02_投稿指引.md").exists()
    print(f"[ok] 发布：mock 记录 + manual 投稿包（{pkg.name}/）")

    # 阅读数据回流：mock 抓取 → 时间序列 → 趋势
    from studio import analytics as an

    snap = an.fetch_stats(cfg, project.root.name, source="mock")
    assert snap["metrics"]["reads"] > 0 and snap.get("chapter_retention")
    trend = an.compute_trend(an.load_series(cfg, project.root.name))
    assert trend["snapshots"] >= 1 and trend.get("weakest_chapter")
    print(f"[ok] 数据回流：mock 快照 reads={snap['metrics']['reads']}，最弱章节=第{trend['weakest_chapter']['no']}章")

    # 数据复盘 → 教训库 → 选题注入
    data_improve = pipeline.stage_improve(cfg, llm, project)
    assert len(data_improve["lessons"]) >= 3
    lf = tmp / "feedback_lessons.json"
    assert lf.exists()
    digest = pipeline.lessons_digest(cfg)
    assert "下一部" in digest
    print(f"[ok] 数据复盘：{len(data_improve['lessons'])} 条教训入反馈库，选题注入摘要 {len(digest)} 字")

    # 素材库：自动沉淀 / 去重 / 分类 / 检索 / digest
    from studio import material as mat

    mat.materials_path(cfg)  # 初始化目录
    dep = mat.deposit_from_project(cfg, project)
    assert dep["added"] >= 10, dep
    dep2 = mat.deposit_from_project(cfg, project)
    assert dep2["added"] == 0 and dep2["skipped"] >= dep["added"], "重复沉淀应全部去重"
    st = mat.stats(cfg)
    assert st["total"] >= 10 and "标题" in st["by_type"]
    hits = mat.query(cfg, mtype="标题", limit=5)
    assert hits and all(x["type"] == "标题" for x in hits)
    dg = mat.digest(cfg, n=5, max_chars=300)
    assert "[" in dg
    one_id = hits[0]["id"]
    assert mat.remove_material(cfg, one_id)
    print(f"[ok] 素材库：沉淀 {dep['added']} 条 / 去重 / 分类检索 / digest / 删除")

    # 定时任务：到期判定 / CRUD / 方向提示 / 防并发锁
    import datetime as dt

    from studio import scheduler as sch

    sch.schedules_path = lambda c: tmp / "schedules.json"  # 重定向到临时目录
    j = sch.new_job(cfg, "测试任务", {"type": "daily", "time": "20:00"}, count=2,
                    directions=["追妻火葬场"], words=6000)
    now = dt.datetime(2026, 9, 11, 21, 0)
    assert sch.is_due(j, now) and not sch.is_due({**j, "enabled": False}, now)
    assert not sch.is_due(j, dt.datetime(2026, 9, 11, 10, 0)), "未到 20:00 不应触发"
    assert not sch.is_due({**j, "last_run": "2026-09-11 21:30:00"}, now), "当天已跑不重复"
    j2 = sch.new_job(cfg, "间隔任务", {"type": "interval", "minutes": 180})
    assert sch.is_due(j2, now) and not sch.is_due({**j2, "last_run": "2026-09-11 20:30:00"}, now)
    jobs = sch.load_schedules(cfg)
    jobs += [j, j2]
    sch.save_schedules(cfg, jobs)
    assert len(sch.load_schedules(cfg)) == 2
    cmds = [sch.build_run_cmd(cfg, j, i) for i in range(2)]
    assert all("--pick" in " ".join(c) and "auto" in " ".join(c) for c in cmds)
    assert all("--direction" in " ".join(c) for c in cmds)  # 两篇轮换题材
    print("[ok] 定时任务：到期判定 + CRUD + 命令构造（篇数轮换/强制 auto）")

    # 可视化看板：数据采集 / 静态导出 / HTTP 服务
    from studio import dashboard as dash

    ov = dash.collect_overview(cfg)
    assert ov["kpis"]["projects"] >= 1 and len(ov["directions"]) >= 15
    exp = tmp / "board.html"
    dash.export_dashboard(cfg, str(exp))
    html = exp.read_text(encoding="utf-8")
    assert "__PAYLOAD__" not in html and "短篇小说工作室" in html and '"kpis"' in html
    # 内联 JS 提取后写盘，供 node --check（若本机有 node）做语法把关
    import re as _re
    m = _re.search(r"<script>(.*?)</script>", html, _re.S)
    (tmp / "board.js").write_text(m.group(1), encoding="utf-8")

    srv = dash.make_server(cfg, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview") as r:
        assert json.loads(r.read().decode("utf-8"))["kpis"]["projects"] >= 1
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
        assert "const STATIC = null" in r.read().decode("utf-8")
    srv.shutdown()
    print("[ok] 看板：数据采集 + 静态导出 + HTTP 服务")

    # ---- 低危修复的回归钉（防被后续改动悄悄退回去）----
    _test_agents_all_resolves()
    _test_deterministic_recheck_disabled()
    _test_draft_prune_keeps_latest()

    print("\n全部冒烟测试通过 ✔")
    return 0


def _cleanup():
    import shutil

    d = Path("workspace/.smoke_tmp").resolve()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _test_agents_all_resolves():
    """L4：__all__ 里出现模块中不存在的名字会让 star-import 直接 AttributeError。"""
    import studio.agents as agents

    missing = [n for n in agents.__all__ if not hasattr(agents, n)]
    assert not missing, f"agents.__all__ 有不存在于模块的名字：{missing}"
    ns: dict = {}
    exec("from studio.agents import *", ns)   # 真跑一次 star-import
    print(f"[ok] agents.__all__ 全部可解析（{len(agents.__all__)} 个名字）")


def _test_deterministic_recheck_disabled():
    """L10：max_rounds<=0 曾静默返回 pass=True 却一项检查都没做，必须与「通过」区分。"""
    import studio.agents as agents
    from studio.llm import MockLLM

    cfg = {"review": {}}
    _, rep = agents.deterministic_recheck(cfg, MockLLM(), {}, "正文", max_rounds=0)
    assert rep.get("skipped") is True and rep.get("gate") == "skipped_disabled", rep
    _, rep2 = agents.deterministic_recheck(cfg, MockLLM(), {}, "正文", max_rounds=3)
    assert rep2.get("gate") == "skipped_mock" and "skipped" not in rep2, rep2
    print("[ok] deterministic_recheck：max_rounds=0 → skipped_disabled，与 mock 跳过不混淆")


def _test_draft_prune_keeps_latest():
    """L6：DRAFT_KEEP 曾定义后无人引用、_prune 是空壳，旧版本从不归档。"""
    import shutil
    import tempfile

    import studio.drafts as drafts

    class _P:
        def __init__(self, root):
            self.root = root

    tmp = tempfile.mkdtemp()
    try:
        p = _P(tmp)
        for i in range(1, 6):
            drafts.save_draft(p, 1, f"第{i}版正文内容，足够长一些用于快照。" * 3)
        hist = Path(tmp) / "chapters" / ".drafts" / ".history"
        snaps = sorted(x.name for x in hist.glob("ch01_r*.md"))
        assert len(snaps) == drafts.DRAFT_KEEP, f"快照数 {len(snaps)} != DRAFT_KEEP {drafts.DRAFT_KEEP}"
        assert "第5版" in (hist / snaps[-1]).read_text(encoding="utf-8"), "最新快照不是最后一版"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[ok] 草稿归档：保留最近 {drafts.DRAFT_KEEP} 版，最旧自动淘汰")


if __name__ == "__main__":
    code = main()
    _cleanup()
    sys.exit(code)
