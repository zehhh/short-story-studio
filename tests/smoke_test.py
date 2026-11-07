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
    _test_join_sections_keeps_preamble()
    _test_full_ai_hits_excludes_global_labels()
    _test_title_gate_rewards_real_titles()
    _test_section_cast_and_words()
    _test_dedup_guard_reverts_mass_deletion()
    _test_continuity_pov_and_tail()
    _test_patch_verified_rejects_bad_patch()
    _test_audit_lead_detection()
    _test_score_cap_by_high()
    _test_state_causeless_and_time_hint()
    _test_state_prop_and_exit_ledgers()
    _test_boundary_drops_explained_items()
    _test_grade_issue_evidence()
    _test_style_high_needs_scanner_backing()
    _test_sec_overreach_detector()
    _test_recheck_rejects_rewrite_that_adds_hits()
    _test_strip_body_preamble_removes_duplicate_lead()
    _test_self_ref_consistency()
    _test_death_ledger_resurrection()
    _test_summary_jump_detector()
    _test_body_trope_cluster()
    _test_self_dup_guard()
    _test_plan_replay_nonadjacent()
    _test_rewrite_regress_guard()
    _test_direction_keys_fuzzy()

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


def _test_join_sections_keeps_preamble():
    """逐节回写不得丢序言（书名行 + 导语）。

    历史缺陷：`_split_full_sections` 只从第一个节头开始切，各处逐节处理完直接
    `"\\n\\n".join(s["text"] for s in secs)` 回写，把第一个节头之前的书名行与导语
    整段丢掉——实测 11006 字 → 10502 字，且体检里「A结构/导语」直接判不过。
    第二个坑：定位序言不能用 `full.find(节头)`，逐节写的节头是裸编号 "01"，
    而书名行「字数：10174」里就含 "01"，会拼出半句序言。
    """
    from studio.pipeline import _join_sections, _split_full_sections

    full = (
        "# 封妃当日太后赐我一碗药\n\n"
        "> 平台：知乎盐选 | 字数：10174 | 题材：女主后宫\n\n"
        "**一句话故事**：小官之女封妃当日获太后赐药。\n\n"
        "01\n\n第一节的正文，写得足够长以便被识别成正文。\n\n"
        "02\n\n第二节的正文，同样要有点长度。\n"
    )
    secs = _split_full_sections(full)
    assert len(secs) == 2, f"应切出 2 节，实际 {len(secs)}"
    out = _join_sections(secs, full)
    assert out.startswith("# 封妃当日太后赐我一碗药"), "序言书名行被丢掉了"
    assert "字数：10174" in out, "序言里的元信息行被丢掉了（find 命中了编号）"
    assert "第一节的正文" in out and "第二节的正文" in out, "正文被丢了"
    print("[ok] 逐节回写保住序言（书名行 + 导语），且不被书名行里的裸编号误定位")


def _test_score_cap_by_high():
    """终审分按 high 级硬伤条数封顶（判官分保留相对次序，硬伤多就压天花板）。

    历史缺陷：先用「按清单逐条扣分」折算，v12 三轮 5/6/5 处 high → 40/46/40，
    v13 更抖成 24/32/15/43 —— 清单条数本身是噪声（一处问题常被拆成两三条），
    折算出的分数跟着抖，修复循环既看不出改进，也没法拿来做验收阈值。
    """
    from studio.pipeline import score_cap_by_high

    assert score_cap_by_high(0) is None, "没有硬伤就不许封顶"
    # 3 处起逐级压下：55 / 50 / 45，5 处以上一律不及格
    assert [score_cap_by_high(n) for n in (1, 2, 3, 4, 5)] == [68.0, 62.0, 55.0, 50.0, 45.0]
    assert score_cap_by_high(9) == 45.0, "硬伤再多也不越过 45"
    # 单调不增：硬伤越多，天花板越低
    caps = [score_cap_by_high(n) for n in range(6)]
    vals = [c for c in caps if c is not None]
    assert vals == sorted(vals, reverse=True), f"封顶必须单调：{caps}"
    # 真实配比校准：v13 终审 3 处 high 时，判官 66 分被压到 55（不可发）
    assert min(66.0, score_cap_by_high(3)) == 55.0
    print("[ok] 终审分按硬伤条数封顶（0 不封顶 / 3→55 / 5→45，单调）")


def _test_audit_lead_detection():
    """体检必须认得成稿.md 的真实版式：导语在第 7 行（**导语**：…）。

    历史缺陷：`audit_text` 取固定前 6 行找「导语」，而 成稿.md 是
    标题 / 空行 / 平台行 / 空行 / 一句话故事 / 空行 / 导语 —— 恰好落在窗口外，
    于是每篇 B 级稿都被体检判成「A结构/导语 失败」→ C 不可投，
    与流水线自己的 submission()（B 可投）互相打架。判据应改为「第一个节头之前的全部内容」。
    """
    from studio import audit

    full = "\n".join([
        "# 封妃当日太后赐我一碗避子汤",
        "",
        "> 平台：知乎盐选 | 字数：9144 | 题材：宫廷权谋 | 读者团均分：6.4",
        "",
        "**一句话故事**：小官之女入宫封妃，太后赐下避子汤。",
        "",
        "**导语**：封妃那日，太后亲手端来一碗避子汤。",
        "她笑吟吟地说，前头两位，都没熬过三年。",
        "",
        "01",
        "",
        "第一节正文，写得够长以便被识别成节。",
        "",
        "02",
        "",
        "第二节正文，也要有一定长度。",
    ]) + "\n"
    rep = audit.audit_text(full)
    lead = [c for c in rep["checks"] if c["name"] == "导语"]
    assert lead, "体检里没有「导语」这一项"
    assert lead[0]["ok"], f"导语在第 7 行，体检没认出来：{lead[0]}"
    print("[ok] 体检认得成稿版式：导语在第 7 行也判「有导语」")


def _test_full_ai_hits_excludes_global_labels():
    """整稿 AI 腔判据必须是**逐节扫描的并集**，不能把整稿级判据混进来。

    历史缺陷：`_ai_deflavor` 直接 `_section_ai_hits(full, cfg)` 扫全文，
    langqa 里「节长失衡」这类只有整稿才成立的判据被当成 AI 腔标签写进
    `report["langqa_ai_repair"]["labels"]`，看板上会显示成「AI 腔：节长失衡」。
    """
    from studio.config import load_config
    from studio.pipeline import _full_ai_hits, _split_full_sections

    cfg = load_config(None)
    sec1 = "她推门进来，把伞往墙角一搁。\n\n「你回来了。」我说。\n\n她没应声，径直走到桌边坐下。\n"
    sec2 = "我给她倒了杯水，她没接。\n\n「钱的事，你想怎么办。」她终于开口。\n"
    full = f"# 测试书名\n\n01\n\n{sec1}\n02\n\n{sec2}"
    assert len(_split_full_sections(full)) == 2, "样例应切成 2 节"
    labels = _full_ai_hits(full, cfg)
    assert "节长失衡" not in labels, f"整稿级判据混进了 AI 腔标签：{labels}"
    print("[ok] 整稿 AI 腔判据逐节取并集，不混入整稿级判据")


def _test_title_gate_rewards_real_titles():
    """书名卖点分必须能分清「真实榜单名」和「文艺空名」。

    历史缺陷：`_CONFLICT` 里没有「赐/赏/灌/罚」这类古言动作，`_REVERSAL` 里没有
    「前两个都没活过」这类前例钩，于是实测交付稿《封妃当日太后赐我一碗药，前两个
    妃子都没活过三年》只拿 5 分（刚过门线），改标题的重试循环永远收不到「更好」的信号。
    """
    from studio import title_gate as TG

    good = "封妃当日太后赐我一碗药，前两个妃子都没活过三年"
    assert TG.score(good) >= 8, f"真实古言强书名只拿 {TG.score(good)} 分"
    assert TG.family(good) == "relation", "应判为关系冲突式"
    # 文艺空名必须仍然是 0 分：这条线是「短篇借不到作者名望」的立足点
    for bad in ("萤火之城", "娇养玫瑰", "心肝宠", "够野"):
        assert TG.score(bad) == 0, f"文艺空名 {bad} 拿到了 {TG.score(bad)} 分"
    # 概念式书名不能因为新加的冲突词表被抬高到关系式
    assert TG.family("我在大清当老六") == "concept"
    print("[ok] 书名卖点分：真实强名 8+，文艺空名 0，两套战法不互相污染")


def _test_section_cast_and_words():
    """逐节写的两个契约：人物名册不许丢、每节字数跟计划走。"""
    from studio.pipeline import _cast_block, _plan_cast, _sec_words

    plan = {"characters": [
        {"name": "沈清秋", "role": "主角", "major": True, "pov": True},
        {"name": "赵太后", "role": "对手", "major": True, "pov": False},
        {"name": "柳如烟", "role": "配角", "major": False, "pov": False},
        "李承乾",
    ]}
    cast = _plan_cast(plan)
    assert [c["name"] for c in cast] == ["沈清秋", "赵太后", "柳如烟", "李承乾"], cast
    assert cast[0]["pov"] is True and cast[3]["major"] is True
    block = _cast_block(cast, ["沈清秋", "赵太后"])
    assert "第一人称" in block and "沈清秋" in block
    assert "本节出场：沈清秋、赵太后" in block
    assert _plan_cast({}) == [] and _cast_block([], [])  # 没声明时不许炸

    # 字数：计划给了就照计划；没给或越界就总字数均摊，并夹到区间内
    assert _sec_words({"words": 820}, 10000, 13, 600, 1000) == 820
    assert _sec_words({"words": 9999}, 10000, 13, 600, 1000) == 769
    assert _sec_words({}, 10000, 13, 600, 1000) == 769
    print("[ok] 人物名册与每节目标字数均按计划契约解析，缺声明时安全退化")


def _test_dedup_guard_reverts_mass_deletion():
    """复读型输出不许被「删重复段」删成空壳。

    99617 字事故与 mock 实测（7828 字 → 817 字）是同一种死法：确定性清理步骤
    把正文大面积销毁。守卫口径是「超过 40% 就撤销本步」。
    """
    from studio.language import deterministic_repair

    para = "她把碗往桌上一推，说这三年辛苦你了，房子归你妹妹我们两清。"
    text = "\n".join([para] * 20)
    out, rep = deterministic_repair(text, log=lambda *a, **k: None)
    assert rep.get("deduped_paras") == 0, f"应当撤销删除，实际删了 {rep.get('deduped_paras')} 段"
    assert rep.get("dedup_guard"), "撤销必须留痕"
    assert len(out) >= len(text) * 0.95, f"正文被销毁：{len(text)}→{len(out)}"

    # 正常「贴重一次」必须照删，守卫不能把功能挡死
    ok_text = "\n".join([para, "她没再说话，屋里只剩钟摆声。", "我数了数柜子里的碗，一共七个。",
                         para, "夜里我听见她在院里咳嗽。"])
    out2, rep2 = deterministic_repair(ok_text, log=lambda *a, **k: None)
    assert rep2.get("deduped_paras") == 1, rep2
    assert "dedup_guard" not in rep2
    print("[ok] 删重复段有销毁上限：整篇复读撤销，单处贴重照删")


def _test_continuity_pov_and_tail():
    """连续性两项零成本判据：人称断裂、节尾复述。

    真实终审 3 处 high 硬伤里 2 处是「叙述里突然用第三人称叫主角」、1 处是
    「节尾把本节台词原样再说一遍」。这两类都能确定性判出来，不该留给 LLM 判官
    到终审才发现——补丁轮实测会把硬伤越改越多（3 → 4 → 5 处）。
    """
    from studio import continuity as C

    # 人称：叙述里出现主角名 → 命中；只在别人嘴里 → 不命中
    assert C.pov_breaks("苏清婉握紧银针。", "苏清婉"), "叙述里的主角名必须抓到"
    assert not C.pov_breaks('我握紧银针。赵嬷嬷道：“苏清婉，你可知罪？”', "苏清婉"), \
        "引号里的名字是别人在叫，不是人称跳"
    assert not C.pov_breaks("我握紧银针。", ""), "没给名字时不许炸"

    # 节尾复述：末句与前文逐字重合 ≥12 字 → 命中
    dup = ("我数着步数走进长街，数到第七步停下。风从巷口灌进来，吹得灯影乱晃。"
           "墙根下蹲着个卖炭的老汉，他抬头看我一眼又低下去。我摸到袖里那根银针，针尖凉得刺手。"
           "风从巷口灌进来，吹得灯影乱晃。")
    assert C.tail_repeat(dup), "节尾整句复述前文必须抓到"
    fresh = ("我数着步数走进长街，数到第七步停下。风从巷口灌进来，吹得灯影乱晃。"
             "墙根下蹲着个卖炭的老汉，他抬头看我一眼又低下去。我摸到袖里那根银针，针尖凉得刺手。"
             "我推开门，屋里的灯没点。")
    assert not C.tail_repeat(fresh), "正常收尾不许误报"

    # 线索交接：中文中心语在短语尾部，尾部 ≥3 字命中即算接住；泛词 2 字不算
    assert C.carry_hit("袖中藏着的银针", "我摸到那根银针，指尖一凉。"), "尾部中心语必须能召回"
    assert not C.carry_hit("半张残缺信笺", "我推开门，屋里没人。"), "没接住就是没接住"
    print("[ok] 连续性：人称断裂/节尾复述/线索接住 三项判据零误报")


def _test_grade_issue_evidence():
    """判官的硬伤必须能落回原文，否则不许参与压档。

    实测背景（v15）：判官把「上一节手滑溅出几点 → 本节碗脱手砸碎装疯」判成
    high 级「重演」，而那是升级；按「硬伤条数封顶」把 68 分的稿子压到 43 分，
    四轮修复全在追一个不存在的缺陷。判分可以有主观，但硬伤必须可核验。
    """
    from studio.pipeline import grade_issue_evidence

    text = ("我端起碗，指尖触到滚烫的碗壁，眉头轻蹙装作被烫到的样子，手腕顺势抖动。"
            "汤汁溅出几点落在袖口上。")
    issues = [
        # 只有印象、没有引文 → 降级
        {"severity": "high", "note": "开头重演上节结尾已写的泼汤、手抖、身子歪倒"},
        # 完整引了原句（哪怕结论仍然可疑，但「可核验」这一关它过了）→ 保留
        {"severity": "high", "note": "上节「手腕顺势抖动，汤汁溅出几点」在本节又写了一遍"},
        # 正文里根本没有提到的物件 → 降级
        {"severity": "high", "note": "第二节提到银针击碎瓷壶，前文从未交代"},
        # 非 high 不动
        {"severity": "mid", "note": "口癖偏多"},
    ]
    out, n_down = grade_issue_evidence(issues, text)
    assert n_down == 2, f"应降级 2 条，实际 {n_down}"
    sev = [i["severity"] for i in out]
    assert sev == ["mid", "high", "mid", "mid"], f"级别不对：{sev}"
    assert out[0].get("evidence_checked") is False, "降级必须留痕"
    assert out[1].get("evidence_checked") is not False
    print("[ok] 硬伤可核验：引不出原文的 high 降为 mid（不参与压档）")


def _test_patch_verified_rejects_bad_patch():
    """补丁必须逐条局部体检：改坏的当场丢弃，不许混在好补丁里一起回滚。

    实测背景：终审补丁轮是「6 条意见一次性盲改 → 整稿重判」，改坏的补丁
    混在改好的里面，整稿分不动就整轮回滚，好补丁跟着扔，四轮 62→68→66→68
    原地打转。文献结论一致：没有外部 verifier 的自评自改就是空转。
    """
    from studio import pipeline as P
    from studio.config import load_config

    cfg = load_config(None)
    clean = ("我推开门，屋里没人。桌上放着半碗凉透的粥，粥面结了一层皮。"
             "我把碗端起来闻了闻，是昨天的。灶膛里还有一点火星，我蹲下去吹了两下，"
             "火光慢慢爬上来，照亮墙角那口没盖盖子的水缸。")
    assert P._span_pen(cfg, clean) == 0, "干净文本不许有病分"

    bad_patch = [{"find": "我推开门，屋里没人。",
                  "replace": "我推开门，屋里没人，嘴角勾起一抹冷笑，就在这时空气凝固了。"}]
    kept, n_ok, recs = P._apply_patches_verified(cfg, clean, bad_patch)
    assert kept == clean, "引入 AI 口癖的补丁必须被拒绝"
    assert n_ok == 0 and recs and recs[0].get("reason"), "拒绝必须留痕说明原因"

    good_patch = [{"find": "我把碗端起来闻了闻，是昨天的。",
                   "replace": "我把碗端起来闻了闻，酸味顶鼻子，是昨天的。"}]
    kept2, n_ok2, _ = P._apply_patches_verified(cfg, clean, good_patch)
    assert n_ok2 == 1 and "酸味顶鼻子" in kept2, "中性改写必须能应用"

    assert P._det_hits_count(cfg, clean) == 0
    assert P._det_hits_count(cfg, "就在这时，空气凝固了，嘴角勾起一抹冷笑。") > 0
    print("[ok] 补丁逐条局部体检：坏补丁拒绝留痕 / 好补丁应用 / 确定性缺陷计数")


def _test_state_causeless_and_time_hint():
    """无因之果判定 + 时间锚注入。

    实测背景：v11 终审剩下的 high 硬伤里，「袖中银针弹出击碎瓷壶」是前文从没
    提过银针（无因之果），「小节04又回到天刚亮」是时间线重铺。前者可以确定性判
    （全库 35 篇只命中 3 处，真阳性明确），后者不能——纯词法比较在全库误报 47 次，
    只能做上下文注入，判定交给轻模型（见 studio/state.py 顶部）。
    """
    from studio import state

    prev = "我把门带上，屋里只剩一盏灯。"
    # 前文没提过 + 首次出现 + 当场就用 + 没交代来历 → 报
    hits = state.causeless(prev, "我猛地起身，袖中银针顺势弹出，击碎了那青花瓷壶。")
    assert [h["item"] for h in hits] == ["银针"], f"银针必须报出来，实测 {hits}"

    # 同一句里交代了来历 → 不报
    assert state.causeless(prev, "我取出母亲留下的那枚银针，将它弹出。") == [], "有来历不许报"
    # 前文出现过 → 不报
    assert state.causeless("他把银针收进袖中。", "银针弹出，击中瓷壶。") == []
    # 只是出现在描写里、没被当工具用 → 不报
    assert state.causeless(prev, "月光照在焦土上，像毒药的痕迹。") == []
    # 假设句 → 不报
    assert state.causeless(prev, "若是有人提前备下解药，或者这根本就不是毒。") == []

    hint = state.prev_time_hint("他一直坐到入夜，灯芯爆了个花。")
    assert "入夜" in hint and "次日" in hint, f"时间锚必须带上收尾时点，实测 {hint!r}"
    assert state.prev_time_hint("他推门进来。") == "", "没有时点词时不许编一个出来"
    print("[ok] 无因之果判定（真阳性/有来历/前文已见/仅描写/假设句）+ 时间锚注入")


def _test_state_prop_and_exit_ledgers():
    """已消耗道具 / 已退场人物两类台账。

    实测背景（v15 成稿，6 处 high 硬伤里的 2 处）：
    - 第 08 节「我将蜜丸放入口中…」之后紧接着「从发髻中取出另一粒早已藏好的
      蜜丸吞下」——同一节内把已经吃掉的东西又拿出来；
    - 第 04 节结尾「萧景琰…转身离去」，第 05 节开头「萧景琰的手指离开下颌」——
      人已经退场了又在场。
    这两类写手记不住是因为逐节写时每节都是一次全新对话，必须由代码逐节累加。
    """
    from studio import state

    prior = ("他从袖中取出一个紫檀木盒，掀开盖子，里头躺着一颗金黄的蜜丸。"
             "「吃了它。」他把蜜丸递到我嘴边，我张口吞下那颗蜜丸。"
             "萧景琰松开手，没有回答我的问题，只是深深看了我，转身离去。")
    body = "我取出另一颗蜜丸，放在掌心看了看。萧景琰坐在一旁，端起茶盏。"

    assert [c["obj"] for c in state.consumed_props(prior)] == ["蜜丸"]
    assert [h["obj"] for h in state.prop_reuse(prior, body)] == ["蜜丸"], "已吞下的蜜丸又被取出，必须报"
    exits = state.exit_then_present(prior, body, ["萧景琰", "苏清婉"])
    assert [h["name"] for h in exits] == ["萧景琰"], f"已转身离去的萧景琰又在场，必须报，实测 {exits}"

    # 节内复用：同一节里先吃下、后取出
    intra = "我将蜜丸放入口中，轻轻一咬。药香化开。我又从发髻里取出一粒蜜丸吞下。"
    assert [h["obj"] for h in state.prop_reuse("", intra)] == ["蜜丸"], "节内复用必须报"

    # 反例：泛称当道具 / 取回后重新获得 / 次日重新到场
    assert state.prop_reuse("我把话交代了。", "我取出那封信。") == []
    assert state.prop_reuse("我把银针收回袖中。", "我又取出银针。") == []
    assert state.exit_then_present("他转身离去。", "次日萧景琰又来了。", ["萧景琰"]) == []
    assert state.exit_then_present("他转身离去。", "我盯着门口看了很久。", ["萧景琰"]) == []
    print("[ok] 已消耗道具 / 已退场人物台账（跨节 + 节内 + 三类反例）")


def _test_boundary_drops_explained_items():
    """衔接判据：模型自己写了「有来历」的条目不许再当无因之果报。

    实测背景（v16 真跑）：轻模型返回过
    「假死药引的脉象效果（上节萧景琰已交代解药配方与假死药引，有来历）」
    「乌木令牌（萧景琰当场掷给，有来历）」——判词明说有来历，却被当成
    「凭空出现」打回整节重写，白烧两轮 token，还逼写手删掉正常情节。
    """
    from studio import pipeline

    class _StubLLM:
        def __init__(self, payload):
            self.payload = payload

        def chat(self, system, user, json_mode=False):
            return self.payload

    prev = "他把药方念了一遍。" + "字" * 260
    cur = "我照着方子配药。" + "字" * 260

    explained = ('{"replay": false, "why": "", '
                 '"causeless": [{"item": "乌木令牌", "why": "萧景琰当场掷给，有来历"}, '
                 '{"item": "袖中明黄绢帛", "why": "上节已交代是从密室取的"}]}')
    d = pipeline._sec_boundary(_StubLLM(explained), prev, cur)
    assert d["causeless"] == [], f"判词说有来历的不许报，实测 {d['causeless']}"

    genuine = ('{"replay": false, "why": "", '
               '"causeless": [{"item": "枕下的银簪", "why": "前文从没提过此簪，凭空出现"}]}')
    d = pipeline._sec_boundary(_StubLLM(genuine), prev, cur)
    assert [c["item"] for c in d["causeless"]] == ["枕下的银簪"], f"真无因之果必须留，实测 {d}"
    print("[ok] 衔接判据：判词自述有来历的条目被丢弃，真无因之果保留")


def _test_style_high_needs_scanner_backing():
    """文风/AI 腔类 high 必须由本项目扫描器背书，否则降 mid（不压档）。

    实测背景（v16 真跑）：判官报了一处 high「AI腔与句式黑名单密集」并引了原文，
    `grade_issue_evidence` 核验通过，但同一个 `langqa.scan` 在那份 9546 字成稿上
    命中 0 条（high_mid_blacklist/frag/euro 全 0）。这类「按自己口味说的话」
    会让 3 处 high 封顶到 55 分，压掉一整档。
    """
    from studio import pipeline

    clean = "我端起碗，喝了半口。" * 40
    iss = [
        {"severity": "high", "type": "文风", "note": "AI腔与句式黑名单密集：嘴角勾起一抹"},
        {"severity": "high", "type": "连续性", "note": "两节重演同一场戏"},
        {"severity": "mid", "type": "文风", "note": "口癖多"},
    ]
    out, n = pipeline.demote_unverified_style(iss, clean)
    sev = [i["severity"] for i in out]
    assert n == 1, f"只该降那一条 high 文风，实测降了 {n} 条"
    assert sev == ["mid", "high", "mid"], f"连续性 high 不许动，实测 {sev}"

    # 扫描器真有命中时，判官说的就是清单内的违规 → 保持 high 不降
    dirty = ("他嘴角勾起一抹冷笑，空气凝固了。就在这时，眼底闪过一丝狠厉。"
             "他伸手拿起桌上的茶盏，缓缓开口，声音不大，却带着不容置疑的寒意。") * 12
    out2, n2 = pipeline.demote_unverified_style(iss, dirty)
    assert n2 == 0, f"扫描器命中 ≥1 条时不许降级，实测降了 {n2} 条"
    assert [i["severity"] for i in out2] == ["high", "high", "mid"]
    print("[ok] 文风类 high 需扫描器背书（0 命中降 mid / 有命中保持 high）")


def _test_sec_overreach_detector():
    """反方向越界判定：本节把下一节的戏提前写完 → 报；只是提一句 → 不报。"""
    from studio import pipeline

    class _StubLLM:
        def __init__(self, payload):
            self.payload = payload

        def chat(self, system, user, json_mode=False):
            return self.payload

    cur = "我把药吞了下去。" + "字" * 260
    goal = "太后当场翻脸，当众废后"
    d = pipeline._sec_overreach(_StubLLM('{"overreach": true, "why": "太后已经当众宣了废后旨意"}'),
                                cur, goal, "门外传来脚步")
    assert d["overreach"] is True and d["why"], f"越界必须报，实测 {d}"

    d = pipeline._sec_overreach(_StubLLM('{"overreach": false, "why": ""}'),
                                cur, goal, "门外传来脚步")
    assert d["overreach"] is False, f"未越界不许报，实测 {d}"

    # 没有下一节（goal 为空）或正文太短 → 零成本跳过，不调模型
    class _Boom:
        def chat(self, *a, **k):
            raise AssertionError("不该调用模型")

    assert pipeline._sec_overreach(_Boom(), cur, "", "")["overreach"] is False
    assert pipeline._sec_overreach(_Boom(), "短", goal, "")["overreach"] is False
    print("[ok] 反方向越界判定（越界报 / 未越界不报 / 无下一节零调用）")


def _test_recheck_rejects_rewrite_that_adds_hits():
    """终稿复查的重写只许把硬命中改少，不许改多。

    历史 bug（v17 真跑）：recheck 重写把「不是……而是……」翻译腔黑名单写进正文，
    它修的是上一类问题、写坏的是这一轮没人看的那一类，最后体检判 C 不可投。
    """
    from studio import agents
    from studio.config import load_config

    cfg = load_config(None)
    ctx = {"DIRECTION": "古言"}
    clean = "她把碗搁下，走到窗边，看了一会儿院子里的槐树。风把叶子翻过来，白花花一片。"
    dirty = ("她把碗搁下，走到窗边。这不是害怕，而是一种近乎麻木的平静。"
             "她看了一会儿院子里的槐树。空气仿佛凝固了一般。嘴角勾起一抹冷笑。")
    n_clean = agents._hard_hit_count(cfg, ctx, clean)
    n_dirty = agents._hard_hit_count(cfg, ctx, dirty)
    assert n_clean == 0, f"干净文本不许有硬命中，实测 {n_clean}"
    assert n_dirty > n_clean, f"脏文本必须多算，实测 {n_dirty} vs {n_clean}"
    print(f"[ok] 复查回归守卫的判据（干净 {n_clean} 条 / 脏文本 {n_dirty} 条）")


def _test_strip_body_preamble_removes_duplicate_lead():
    """正文自带的「书名：/导语：」必须砍掉，否则成稿里出现两个书名两段导语。

    v18 实测：抬头《入宫封妃那晚太后赐我一碗药…》，正文序言《入宫封妃那天，
    太后赏了我一碗药…》，读者第一眼就是自相矛盾。
    """
    from studio.pipeline import _strip_body_preamble

    body = ("书名：入宫封妃那天，太后赏了我一碗药\n\n导语：\n我入宫封妃那天，太后赐了一碗汤。\n\n"
            "01\n\n凤冠压在脖颈上。\n\n02\n\n她抬眼看我。\n")
    out = _strip_body_preamble(body)
    assert out.startswith("01"), f"应从第一个节头开始，实测 {out[:30]!r}"
    assert "书名：" not in out[:80] and "导语：" not in out[:200], f"序言没砍干净：{out[:120]!r}"
    assert "凤冠压在脖颈上" in out
    # 没有节头（比如直写模式的散稿）→ 原样返回，不许把正文整段砍掉
    plain = "第一段。\n\n第二段。"
    assert _strip_body_preamble(plain) == plain, "无节头时必须原样返回"
    print("[ok] 成稿抬头去重（砍正文序言 / 无节头原样返回）")


def _test_self_ref_consistency():
    """自称一致性：皇帝一会儿「朕」一会儿「臣妾」要报；单一的「臣妾」「我」不许报。"""
    from studio import state

    names = ["萧承煜", "沈清秋"]
    bad = ("萧承煜放下茶盏，淡淡道：「秋儿胆子大，臣妾劝不住。」\n\n"
           "「朕知道。」萧承煜说。")
    hits = state.self_ref_breaks(bad, names)
    assert len(hits) == 1 and hits[0]["name"] == "萧承煜", f"混用必须报，实测 {hits}"
    assert "朕" in hits[0]["male"] and "臣妾" in hits[0]["female"]

    # 只用一种自称（妃嫔对皇帝称臣妾、对下人称我）→ 不许报
    ok = ("沈清秋低声道：「臣妾遵旨。」\n\n"
          "「我这就去。」沈清秋说。")
    assert state.self_ref_breaks(ok, names) == [], "单一自称不许报"
    # 「臣妾」不能被再算成「臣」（长词优先遮蔽）
    assert state._tokens_in("臣妾遵旨") == ["臣妾"], state._tokens_in("臣妾遵旨")
    # 非名册里的人不参与
    assert state.self_ref_breaks(bad, ["沈清秋"]) == []
    print("[ok] 自称一致性（朕/臣妾混用报 / 单一自称不报 / 长词遮蔽）")


def _test_death_ledger_resurrection():
    """生死台账：宣告死亡之后再活着出场要报；死亡词属于别人（我想让他死）不许报。"""
    from studio import state

    names = ["裴寂", "萧景琰", "太后"]
    s1 = {"no": 1, "text": "裴寂被短刀捅穿肋下，血淌了一地。"}
    s2 = {"no": 2, "text": "「三个月前裴寂死在冷宫，」我抬起手臂。"}
    s3 = {"no": 3, "text": "裴寂扑通跪下，额头重重磕在金砖上。「臣有罪。」"}
    hits = state.death_audit([s1, s2, s3], names)
    assert any(h["name"] == "裴寂" and h["dead_at"] == 2 and h["alive_at"] == 3
               for h in hits), f"死人复活必须报，实测 {hits}"

    # 节级版本只报本节这一处
    now = state.resurrect_in([s1["text"], s2["text"]], s3["text"], names)
    assert [h["name"] for h in now] == ["裴寂"], f"节级必须只报裴寂，实测 {now}"
    # 只宣告、还没复活 → 不报
    assert state.resurrect_in([s1["text"]], s2["text"], names) == [], "刚宣告死亡不许报"

    # 死亡词属于别人：这些都不算宣告
    for sent in ("太后想让我死，却不想背罪名。",
                 "只要我走向萧景琰，便是赴死之路。",
                 "那是柳如烟拼死塞给我的东西。"):
        assert state.death_claims(sent, names) == [], f"不许误判：{sent}"
    # 真宣告的各种写法
    for sent, who in (("太后已死，宫中大乱。", "太后"),
                      ("萧景琰死在御书房。", "萧景琰"),
                      ("裴寂薨了。", "裴寂")):
        got = [c["name"] for c in state.death_claims(sent, names)]
        assert got == [who], f"{sent} 应判 {who} 死亡，实测 {got}"
    # 假设/谎称类有豁免标记
    assert state.death_claims("倘若裴寂死了，这局就散了。", names) == [], "假设句不许判死"
    print("[ok] 生死台账（复活报 / 死亡词属他人不报 / 豁免词生效）")


def _test_summary_jump_detector():
    """用概述跳过关键情节：节首「N个月后」+「一连串变故」→ 报；正常场景 → 不报。"""
    from studio.pipeline import _summary_jump

    bad = ("三个月后，宫宴。\n\n我身着皇后礼服。从贵妃到皇后，中间经历了北境平叛、"
           "太后党羽清洗等一连串血腥变故。\n\n太后坐在对面，脸色灰败。")
    assert _summary_jump(bad), "跳时间+概括必须报"
    bad2 = "这期间发生了太多事。\n\n我坐在殿里。"
    assert _summary_jump(bad2), "「这期间发生了」必须报"
    ok = ("夜深了，穿堂风把烛火吹得忽明忽暗。\n\n门轴轻响，李承乾站在榻前。\n\n"
          "「沈妃好兴致。」他声音低沉。")
    assert _summary_jump(ok) == "", f"正常场景不许报，实测 {_summary_jump(ok)!r}"
    # 只跳时间、不概括（合法的场景切换）→ 不报
    ok2 = "三日后，我在偏殿见了他。\n\n他手里捏着一封信。"
    assert _summary_jump(ok2) == "", "只跳时间不概括不许报"
    print("[ok] 概述跳跃检测（跳时间+概括报 / 单纯场景切换不报）")

def _test_body_trope_cluster():
    """生理反应堆叠：单处合法，200 字内挤三个以上才报（判官嘴里的「测量式细节密集」）。"""
    from studio import langqa

    # 单个身体反应是合法中文，不许报
    one = "我额角沁出冷汗，把碗放回案上。"
    r = langqa.scan(one)
    assert r["stats"]["body_clusters"] == 0, "单个身体反应不许判堆叠"

    # 三个以上挤在一段里 → 报
    bad = ("额角沁出冷汗，寒意顺着骨头缝往里钻，指节泛白，浑身抖如筛糠。"
           "她抬眼看他，一句话都说不出来。")
    r2 = langqa.scan(bad)
    assert r2["stats"]["body_clusters"] >= 1, f"堆叠必须报，实测 {r2['stats']}"
    assert any("生理反应堆叠" in h["label"] for h in r2["hits"]), "必须产出对应 hit"
    hit = [h for h in r2["hits"] if "生理反应堆叠" in h["label"]][0]
    assert hit["severity"] == "mid", "堆叠是文风密度问题，不许判 high（评分封顶只数硬伤）"

    # 换成看得见的行为结果 → 不报
    ok = "我把碗放回案上，放得太轻，它自己滑了一下。她捏着帕子的手停在半空。"
    assert langqa.scan(ok)["stats"]["body_clusters"] == 0, "行为结果不许判堆叠"
    print("[ok] 生理反应堆叠（单个不报 / 密集才报 / 严重度 mid）")

    # 密度判据：阈值来自 onehu 人类语料（中位 0.21/千字、p90 0.73），取 1.2 作红线。
    # 构造 1000 字里 2 处（2.0/千字）→ 必须报；1000 字里 1 处（1.0/千字）→ 不报。
    _filler = "她把帕子叠好，放回袖中，又看了一眼门外。" * 40
    dense = "额角沁出冷汗。" + _filler + "寒意顺着骨头缝往里钻。"
    r3 = langqa.scan(dense)
    assert r3["stats"]["body_rate"] >= 1.2, f"密度必须算出来，实测 {r3['stats']['body_rate']}"
    assert any("密度超标" in h["label"] for h in r3["hits"]), "密度超标必须报"
    thin = "额角沁出冷汗。" + _filler * 2
    assert langqa.scan(thin)["stats"]["body_rate"] < 1.2, "单处不许判密度超标"
    print("[ok] 生理反应密度（人类基准 0.21/千字 · 红线 1.2）")

def _test_rewrite_regress_guard():
    """整稿改写的语言回归守卫：改长改短不误判，语言变差必须拦。"""
    from studio.pipeline import _rewrite_regress

    NL = chr(10)

    clean = ("01" + NL + NL + "她端着药进来，手一抖，药洒了。我没说话。" * 8 + NL
             + NL + "02" + NL + NL + "他把碗放在桌上，没抬头。" * 8)
    ok, why = _rewrite_regress({}, clean, clean, "自检")
    assert ok, f"同稿应放行，实际 {why}"
    bad = clean + "就在这时，空气凝固了，嘴角勾起一抹冷笑，眼底闪过一丝阴狠。" * 8
    ok, why = _rewrite_regress({}, clean, bad, "自检")
    assert not ok, f"语言变坏稿应被拒，实际 ok={ok}"
    ok, why = _rewrite_regress({}, clean, clean[: len(clean) // 2], "自检")
    assert not ok and "坍缩" in why, f"坍缩应被拒，实际 {why}"
    grow = ("01" + NL + NL + "她端着药进来，手一抖，药洒了。我没说话。" * 16 + NL
            + NL + "02" + NL + NL + "他把碗放在桌上，没抬头。" * 16)
    ok, why = _rewrite_regress({}, clean, grow, "自检", min_keep=1.0, pen_slack=1.6, hit_slack=2.0)
    assert ok, f"等密度扩写应放行，实际 {why}"
    print("[ok] 整稿改写语言守卫：同稿放行 / 语言变坏拦 / 坍缩拦 / 等密度扩写放行")


def _test_direction_keys_fuzzy():
    """题材名不是梗库原名时也必须解析出 id，且读者画像要覆盖全部梗库方向。"""
    import json
    import pathlib

    from studio.agents import _direction_keys

    keys = _direction_keys("婆媳矛盾")
    assert "poxi" in keys, f"「婆媳矛盾」应解析到 poxi，实际 {sorted(keys)}"
    keys2 = _direction_keys("宫廷权谋")
    assert "gongting" in keys2, f"「宫廷权谋」应解析到 gongting，实际 {sorted(keys2)}"
    assert _direction_keys("完全编造的题材九十九") == {"完全编造的题材九十九"}

    root = pathlib.Path(__file__).resolve().parents[1]
    readers = json.loads((root / "data" / "readers.json").read_text(encoding="utf-8"))["readers"]
    tropes = json.loads((root / "data" / "tropes.json").read_text(encoding="utf-8"))
    dirs = tropes.get("directions") or []
    cov = set()
    for r in readers:
        cov |= set(str(x) for x in (r.get("affinity") or []))
    missing = [str(d.get("id")) for d in dirs if d.get("id") and str(d.get("id")) not in cov]
    assert not missing, f"这些梗库方向没有任何读者画像覆盖（会退化成默认取样）：{missing}"
    print("[ok] 题材键模糊解析（婆媳矛盾→poxi）+ 读者画像 42/42 全覆盖")



def _test_self_dup_guard():
    """节内自我重复判据：同一句贴两遍要报，正常不会误报。"""
    from studio.pipeline import _self_dup

    bad = "我依言跪在软垫上，凤冠霞帔压得脖颈生疼。我抬头看了一眼。" * 2
    assert _self_dup(bad), "同一段贴两遍必须报"
    ok = ("太后放下碗，淡淡道：「哀家累了。」殿里没人应声。"
          "我垂着眼睛数砖缝，数到第七道，听见外面有人拖步过来。")
    assert not _self_dup(ok), f"正常段落不许误报：{_self_dup(ok)!r}"
    assert not _self_dup("短"), "过短文本直接放过"
    print("[ok] 节内自我重复：复制残句报 / 正常段落不误报")


def _test_plan_replay_nonadjacent():
    """分节计划：隔着几节的同一场戏也要拦住（v27 的 04↔06 就是这么漏的）。"""
    from studio.pipeline import plan_replay_risk

    plan = {"sections": [
        {"no": 1, "goal": "太后赐药试探我", "scene": "慈宁宫·太后"},
        {"no": 2, "goal": "我当众推拒不饮", "scene": "大殿·百官"},
        {"no": 3, "goal": "太后又赐药试探我", "scene": "慈宁宫·太后"},
    ]}
    hits = plan_replay_risk(plan)
    assert hits and hits[0]["at"] == [1, 3], f"隔着三节的同场戏必须报：{hits}"
    clean = {"sections": [
        {"no": 1, "goal": "太后赐药试探我", "scene": "慈宁宫·太后"},
        {"no": 2, "goal": "我在御花园撞见柳贵妃", "scene": "御花园·柳贵妃"},
        {"no": 3, "goal": "皇帝查太医院旧案", "scene": "御书房·皇帝"},
    ]}
    assert not plan_replay_risk(clean), "不同的戏不许误报"
    print("[ok] 分节计划重演风险：隔节同场戏报 / 不同戏不误报")


if __name__ == "__main__":
    code = main()
    _cleanup()
    sys.exit(code)
