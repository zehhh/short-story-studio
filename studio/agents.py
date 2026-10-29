# -*- coding: utf-8 -*-
"""多 agent 协作：写手 agent、审稿主编 agent、读者评分团 agent。

流程（每章双门禁，不通过就打回重写，直到通过或用尽轮次上限）：
    写手写稿 → 主编审查 ──fail──▶ 带意见重写 ──▶ 再审 …
                    │pass
                    ▼
              读者团评分 ──低于阈值──▶ 带读者反馈重写 ──▶ 再评 …
                    │达标
                    ▼
                 定稿落盘
"""
from __future__ import annotations

import json
import re
import statistics

from .config import data_path
from .langqa import format_issues as lang_format
from .util import count_words_zh
from .langqa import scan as lang_scan
from .langqa import banned_regex, format_banned, scan_banned
from .langqa import think_leak, think_leak_hint
from .llm import BaseLLM
from .prompts import render_prompt
from . import review as review_mod

_TROPES_CACHE: dict | None = None


def _direction_keys(direction: str) -> set[str]:
    """题材名/组合方向 → 可用于亲和力匹配的键集合（id + 中文名）。

    `data/readers.json` 的 affinity 存的是梗库方向 **id**（poxi/caili…），
    而管线传进来的 direction 是中文**名**（婆媳大战/彩礼纠纷）。旧实现直接
    `direction in affinity` 永远不成立 → 读者团永远取前 N 个画像（缺陷 P1-3）。
    """
    global _TROPES_CACHE
    if _TROPES_CACHE is None:
        try:
            _TROPES_CACHE = json.loads(data_path("tropes.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _TROPES_CACHE = {"directions": []}
    dirs = _TROPES_CACHE.get("directions") or []
    out: set[str] = set()
    for part in re.split(r"[×·+*]", direction or ""):
        p = (part or "").strip()
        if not p:
            continue
        out.add(p)
        for d in dirs:
            if p in (d.get("id"), d.get("name")):
                if d.get("id"):
                    out.add(str(d["id"]))
                if d.get("name"):
                    out.add(str(d["name"]))
    return out


# ---------------------------------------------------------------------------
# Agent 定义（同一 LLM，不同人格与职责）
# ---------------------------------------------------------------------------

class WriterAgent:
    """写手 agent：负责初稿与打回重写。"""

    def __init__(self, llm: BaseLLM):
        self.llm = llm

    def write(self, ctx: dict) -> str:
        system, user = render_prompt("draft", **ctx)
        return self.llm.chat(system, user)

    def write_full(self, ctx: dict, history: list[dict]) -> str:
        """hybrid 模式：同一上下文续写。history 为前面章节的 assistant 正文累积，
        系统消息只带首章（含语言铁律），每章任务消息全量渲染（含铁律+大纲+衔接）。"""
        system, user = render_prompt("draft", **ctx)
        if history:
            msgs = [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]
            return self.llm.chat_messages(msgs)
        return self.llm.chat(system, user)

    def rewrite(self, ctx: dict, issues: str, round_no: int, kind: str = "fix") -> str:
        system, user = render_prompt(
            "rewrite",
            TITLE=ctx["TITLE"],
            NO2=f"{int(ctx['NO']):02d}",
            ROUND=round_no,
            CH_TITLE=ctx["CH_TITLE"],
            BIBLE=ctx["BIBLE"],
            BEATS=ctx["BEATS"],
            OPEN_HOOK=ctx["OPEN_HOOK"],
            END_HOOK=ctx["END_HOOK"],
            WORDS=ctx["WORDS"],
            CHAPTER_TEXT=ctx["_prev_text"],
            ISSUES=issues,
            BAN_PROPS=ctx.get("BAN_PROPS", "（无）"),
        )
        out = self.llm.chat(system, user)
        return _guard_rewrite(ctx.get("_prev_text", ""), out, kind)

    def rewrite_full(self, ctx: dict, issues: str, round_no: int, kind: str = "fix") -> str:
        """整稿级定点修复（成稿/全文复查用）。

        与 rewrite 的区别：rewrite 是「按章节 beat 重写某一章」，需要 NO/CH_TITLE/
        BEATS/OPEN_HOOK/END_HOOK 等章级字段；成稿级复查手里只有一个 full 字符串，
        没有章级上下文，硬塞 rewrite 会 KeyError。这里走 polish_patch 同源的
        「只修点名问题、骨架不动」提示词，篇幅与结构铁律也一并给出。
        """
        system, user = render_prompt(
            "polish_final",
            IDEA=ctx.get("IDEA", ""),
            ISSUES=issues,
            FULL_TEXT=ctx.get("_prev_text", ""),
            GENRE_GUIDE=ctx.get("GENRE_GUIDE", "（按下沉市场短篇口语风格）"),
            STYLE_SAMPLE=ctx.get("STYLE_SAMPLE", ""),
        )
        user += (
            f"\n\n**字数铁律：当前全稿 {count_words_zh(ctx.get('_prev_text', ''))} 字。"
            "只修上面点名的问题，情节骨架/人物/小节顺序一律不动；"
            "输出不得低于原稿 90% 篇幅，也不得增删小节。**"
            f"\n\n**这是第 {round_no} 轮修复。**"
            # 实测：模型经常只把"改过的那几段"回给你（e2e13 全稿 8111 字，
            # 三轮分别只回 556/495/462 字），长度守卫把片段全判为坍缩丢掉，
            # 三轮全空转。必须显式要求完整正文，并说明"不回全文=无效"。
            "\n**必须是完整正文：从第一行标题/导语到最后一个小节结束，一字不漏地"
            "重新输出全篇。只输出修改片段视为无效，会被退回。**"
        )
        out = self.llm.chat(system, user)
        return _guard_rewrite(ctx.get("_prev_text", ""), out, kind)


class ChiefEditor:
    """审稿主编 agent：毒舌门禁，只审不改。"""

    def __init__(self, llm: BaseLLM):
        self.llm = llm

    def review(self, ctx: dict, chapter_text: str, round_no: int, max_rounds: int) -> dict:
        system, user = render_prompt(
            "review",
            TITLE=ctx["TITLE"],
            NO2=f"{int(ctx['NO']):02d}",
            ROUND=round_no,
            MAX_ROUNDS=max_rounds,
            BIBLE=ctx["BIBLE"],
            BEATS=ctx["BEATS"],
            OPEN_HOOK=ctx["OPEN_HOOK"],
            END_HOOK=ctx["END_HOOK"],
            WORDS=ctx["WORDS"],
            CHAPTER_TEXT=chapter_text,
            CHAIN_ERRORS=ctx.get("CHAIN_ERRORS") or "（本题材无专属红线，按通用标准审）",
        )
        try:
            return self.llm.chat_json(system, user)
        except Exception as e:  # noqa: BLE001
            # 历史缺陷（docs/audit/2026-10-25_代码审计.md P0-3）：解析失败曾直接
            # return {"verdict": "pass"}，等于「LLM 坏了 = 稿件合格」，无人值守时
            # 会产出假合格稿且 report 里看不出来。现在改为显式降级：verdict 置
            # degraded（绝不等同 pass），并带上 error 原因，供上游把门禁标记成
            # degraded_review 而不是 pass。不阻断流水线，但绝不谎报通过。
            print(f"    [review] 审查输出解析失败，标记降级（{e}）")
            return {
                "verdict": "degraded",
                "score": None,
                "issues": [],
                "error": f"{type(e).__name__}: {e}"[:200],
            }

    def rubric(self, cfg: dict, verdict: dict, chapter_text: str, target_words: int = 0,
               direction: str = "") -> dict:
        """把本轮审查结果规范成证据化评分卡（见 studio/review.py 的模块说明）。

        为什么放在 agent 上：门禁的「怎么算分」必须和「谁来审」绑在一起，否则流水线
        里再实现一遍就会漂移；主编换模型时只需换 review.md 的输出契约，聚合逻辑不动。
        """
        return review_mod.build(cfg, verdict, chapter_text, target_words=target_words,
                                direction=direction)


class ReaderPanel:
    """读者评分团 agent：按题材亲和力挑选画像，低于阈值即打回。"""

    def __init__(self, llm: BaseLLM, direction: str, size: int):
        self.llm = llm
        data = json.loads(data_path("readers.json").read_text(encoding="utf-8"))["readers"]
        keys = _direction_keys(direction)
        prefer = [r for r in data if keys & {str(x) for x in (r.get("affinity") or [])}]
        others = [r for r in data if r not in prefer]
        self.personas = (prefer + others)[:size]
        self.affinity_hit = len(prefer)
        if direction and not prefer:
            # 不静默降级：题材亲和力没命中会退化成"永远前 N 个画像"，
            # 必须让人看到（缺陷 P1-3：affinity 存 id、代码比 name，永远失配）。
            print(f"    [reader] ⚠ 题材「{direction}」无画像亲和力命中"
                  f"（已解析键 {sorted(keys)}），本次按默认画像顺序取样")

    def score(self, ctx: dict, chapter_text: str, round_no: int) -> list[dict]:
        out = []
        for p in self.personas:
            persona = f"{p['name']}（{p['age']}岁）：{p['profile']}；讨厌：{p['hates']}；最爱：{p['loves']}"
            system, user = render_prompt(
                "reader_score",
                ROUND=round_no,
                PERSONA=persona,
                PERSONA_NAME=p["name"],
                TITLE=ctx["TITLE"],
                SCOPE=ctx["SCOPE"],
                CHAPTER_TEXT=chapter_text,
            )
            try:
                r = self.llm.chat_json(system, user)
            except Exception:  # noqa: BLE001
                r = {"persona": p["name"], "score": 7.2, "feel": "（读机临时故障，按中下分保守计）", "advice": ""}
            r.setdefault("persona", p["name"])
            r["score"] = float(r.get("score", 7.0))
            out.append(r)
        return out
    @property
    def names(self) -> list[str]:
        return [p["name"] for p in self.personas]


# ---------------------------------------------------------------------------
# 双门禁循环
# ---------------------------------------------------------------------------

def fmt_issues(verdict: dict) -> str:
    issues = verdict.get("issues") or []
    if not issues:
        return f"（主编评分 {verdict.get('score')}，未给出具体意见，请整体提升）"
    lines = []
    for i, it in enumerate(issues, 1):
        if isinstance(it, str):
            # 部分模型把 issue 输出成纯字符串，保留原意
            lines.append(f"{i}. {it}")
            continue
        if not isinstance(it, dict):
            continue
        lines.append(
            f"{i}. [{it.get('criterion','')}/{it.get('severity','')}] {it.get('location','')}："
            f"{it.get('note','')} → 改法：{it.get('fix','')}"
        )
    return "\n".join(lines) or f"（主编评分 {verdict.get('score')}，请整体提升）"


def fmt_rubric_gaps(verdict: dict, ratio_cut: float = 0.75) -> str:
    """把评分卡里"明显失分项"翻译成写手能执行的具体指令。

    为什么需要：模型给的 issues 常常只有一两条泛泛而谈（"节奏偏慢""语言一般"），
    而同一份输出里的 criteria 明明写着 C02 卡点密度 4/12、C05 文风 6/12。
    只把 issues 喂回去 → 重写目标空泛 → 实测三轮 62→66→68 空转（等于白烧两轮）。
    把失分项连同 id/名称/得分率/判语一起喂回去，写手才知道"补哪几项"。
    """
    crit = verdict.get("criteria") or []
    rows = []
    for c in crit:
        if not isinstance(c, dict):
            continue
        if str(c.get("status")) != "evaluated":
            continue
        try:
            earned = float(c.get("earned") or 0)
            mx = float(c.get("max") or 0)
        except (TypeError, ValueError):
            continue
        if mx <= 0:
            continue
        ratio = earned / mx
        if ratio >= ratio_cut:
            continue
        rows.append((ratio, earned, mx, c))
    if not rows:
        return ""
    rows.sort(key=lambda x: x[0])
    lines = ["【评分卡失分项（这是主编给分的依据，逐项补足，别重写全章）】"]
    for ratio, earned, mx, c in rows[:5]:
        line = f"- {c.get('id', '')} {c.get('name', '')}：{earned:g}/{mx:g}（{ratio:.0%}）"
        why = str(c.get("rationale") or "").strip()
        if why:
            line += f" 判语：{why[:40]}"
        ev = c.get("evidence") or []
        if ev:
            line += f" 证据原句：「{str(ev[0])[:40]}」"
        lines.append(line)
    lines.append("补法：每项至少落到一处具体动作或一句对白上；分数已够的项不要动。")
    return "\n".join(lines)


def fmt_reader_feedback(scores: list[dict]) -> str:
    lines = []
    for s in scores:
        line = f"- {s.get('persona')}（{s.get('score')}分）：{s.get('feel','')}"
        if s.get("drop_point") and s.get("drop_point") != "无":
            line += f"；想弃读处：{s.get('drop_point')}"
        if s.get("advice"):
            line += f"；建议：{s.get('advice')}"
        lines.append(line)
    return "\n".join(lines)


def _round_marker(round_no: int, max_rounds: int) -> str:
    return f"{round_no}/{max_rounds}"


_FIX_MIN_KEEP = 0.75
_FIX_MAX_KEEP = 1.60


def _guard_rewrite(old: str, new: str, kind: str = "fix") -> str:
    """定点重写的长度守卫（所有 `WriterAgent.rewrite` 出口统一过这道）。

    为什么必须在**出口**统一守，而不是各调用点自己判：历史上只有「思考泄漏」那一处
    接了 `_no_shrink`，其余 6 处（禁用道具 / 语言质检 / 语言病 / 读者团 / 情绪门 /
    终稿复查）都没有长度校验。实测 e2e6 第 05 章：终稿复查一次重写只回了 458 token，
    1100 字的章节被替换成 583 字的片段，整章直接报废，而门禁仍报 `maxed_review`。
    同一类事故在历史项目里出现过（成稿被切碎后重写放大到 9.9 万字），
    所以长度守必须做成单一权威点，跟着写手出口走。

    kind="fix"（默认）：只允许 ±25% 内的收缩、1.6 倍内的膨胀；
    kind="expand"：扩写场景，上限放宽到 2.2 倍、不限收缩下限之外（仍防坍缩）；
    kind="off"：不判（历史调用点保留原语义）。
    """
    if kind == "off":
        return new
    if not new or not new.strip():
        print("    [guard] 重写稿为空，保留原稿并标记")
        return old
    o, n = len(old.strip()), len(new.strip())
    if not o:
        return new
    ratio = n / o
    lo = _FIX_MIN_KEEP
    hi = 2.2 if kind == "expand" else _FIX_MAX_KEEP
    if ratio < lo:
        print(f"    [guard] 重写稿坍缩（{n}/{o} = {ratio:.0%}），保留原稿并标记")
        return old
    if ratio > hi:
        print(f"    [guard] 重写稿暴涨（{n}/{o} = {ratio:.0%}），保留原稿并标记")
        return old
    return new


def _no_shrink(old: str, new: str) -> str:
    """重写防坍缩：新稿长度不足原稿 65%（疑似偷懒摘要）时保留原稿。"""
    if new.strip() and len(new.strip()) < 0.65 * len(old.strip()):
        print("    [guard] 重写稿长度不足原稿 65%，疑似坍缩，保留原稿并标记")
        return old
    return new


def emotion_check(
    llm: BaseLLM, ctx: dict, chapter_text: str, round_no: int, max_rounds: int, prev_emotion: str
) -> dict:
    """情绪量化审查（对应手册的'情绪拉扯检测官'，通用化）。"""
    system, user = render_prompt(
        "emotion",
        TITLE=ctx["TITLE"],
        NO2=f"{int(ctx['NO']):02d}",
        ROUND=round_no,
        MAX_ROUNDS=max_rounds,
        GOAL=ctx["GOAL"],
        BEATS=ctx["BEATS"],
        PREV_EMOTION=prev_emotion or "（第一章，无）",
        CHAPTER_TEXT=chapter_text,
    )
    try:
        r = llm.chat_json(system, user)
    except Exception as e:  # noqa: BLE001
        # 同 ChiefEditor.review：解析失败不能当"情绪合格"。给 degraded，
        # 由 _gate_emotion 记 degraded_emotion，避免无人值守时假通过。
        print(f"    [emotion] 情绪审查输出解析失败，标记降级（{e}）")
        return {
            "verdict": "degraded",
            "emotion_score": None,
            "hook_score": None,
            "issues": [],
            "error": f"{type(e).__name__}: {e}"[:200],
        }
    r.setdefault("verdict", "pass")
    return r


def emotion_verdict_ok(emo: dict, rcfg: dict) -> tuple[bool, str]:
    """按量化阈值判定情绪门。degraded 一律不算通过。"""
    if emo.get("verdict") == "degraded":
        return False, f"情绪审查降级（{str(emo.get('error') or 'LLM 输出不可解析')[:60]}）"
    score = float(emo.get("emotion_score", 7) or 0)
    hook = float(emo.get("hook_score", 7) or 0)
    e_th = float(rcfg.get("emotion_threshold", 7.0))
    h_th = float(rcfg.get("hook_threshold", 7.0))
    if emo.get("verdict") == "fail":
        return False, "审查官判定 fail"
    if score < e_th:
        return False, f"情绪分 {score} < {e_th}"
    if hook < h_th:
        return False, f"钩子强度 {hook} < {h_th}"
    ratio = emo.get("nue_ratio")
    if ratio is not None:
        try:
            r = float(ratio)
            if r > 0.6:
                return False, f"虐占比 {r:.0%} 过高，读者会弃"
            if r < 0.15:
                return False, f"虐占比 {r:.0%} 过低没张力"
        except (TypeError, ValueError):
            pass
    if emo.get("toxic"):
        return False, f"毒点：{'; '.join(map(str, emo['toxic'][:2]))}"
    return True, ""


def write_chapter_with_gates(
    cfg: dict,
    llm: BaseLLM,
    writer: WriterAgent,
    editor: ChiefEditor,
    panel: ReaderPanel | None,
    ctx: dict,
    chapter_no: int,
    prev_emotion: str = "",
    *,
    project=None,
) -> tuple[str, str, dict]:
    """返回 (正文, 本章摘要, 门禁报告)。串行版（保留原语义，供兼容调用）。

    并行版见 pipeline.stage_write：把本函数拆为
    _chapter_core（串行写稿）→ _gate_review_reader（章间并行）→ _gate_emotion（串行收尾）。
    """
    text, r1 = _chapter_core(cfg, writer, ctx, chapter_no, project=project)
    text, r2 = _gate_review_reader(cfg, writer, editor, panel, ctx, text, chapter_no, project=project)
    text, r3 = _gate_emotion(cfg, editor, writer, ctx, text, prev_emotion, chapter_no, project=project)
    report = {**r1, **r2, **r3}
    gate = "pass"
    for part in (r3, r2, r1):
        g = part.get("gate")
        if g and g != "pass":
            gate = g
            break
    report["gate"] = gate
    body, summary = split_summary(text)
    return body, summary, report


def _gt(direction) -> tuple:
    """题材标签（薄封装，只为让调用点读起来不啰嗦）。"""
    from .langqa import genre_tags

    return genre_tags(direction or "")


def _chapter_core(
    cfg: dict, writer: WriterAgent, ctx: dict, chapter_no: int, *, project=None
) -> tuple[str, dict]:
    """阶段一：写稿 + 字数兜底 + 语言质检（确定性，零 token 成本）。

    章节间存在衔接依赖（上一章摘要/结尾已注入 ctx），此阶段保持串行。
    返回 (正文, 报告片段)。gate 初值 pass，langqa 打满时置 maxed_langqa。

    project 传入时每轮重写后保一份草稿（B03 断点保稿）：本章还没提交，中断后
    靠 `.drafts/` 能捡回最近一版，不必从初稿重烧 token。
    """
    rcfg = cfg["review"]
    report = {"no": chapter_no, "gate": "pass"}
    # --- 写初稿 ---
    text = writer.write(ctx)
    _save_draft(project, chapter_no, text, stage="初稿")
    # --- 门禁零·零：思考泄漏硬检（确定性零 token）---
    # 思考型模型偶发把思维链当正文返回（历史事故：8 章落盘 19 万字）。
    # 命中即带提示重写一次；重写仍泄漏 → 标记 maxed_thinkleak 交上游淘汰/留痕。
    if think_leak(_split_body(text)):
        print("    [write] ⚠ 检测到思考泄漏（疑似思维链/提纲当正文），重写一次 …")
        ctx["_prev_text"] = _split_body(text)
        text2 = writer.rewrite(ctx, think_leak_hint(), 2)
        if not think_leak(_split_body(text2)):
            text = text2
            print("    [write] 重写后恢复正常正文")
        else:
            report["think_leak"] = True
            report["gate"] = "maxed_thinkleak"
            print("    [write] ⚠ 重写后仍疑似思考泄漏，按现稿放行并标记（上游会淘汰）")
    # --- 字数底线：低于目标七成 → 扩写一轮（带天花板 1.4 倍，防失控）---
    target_w = int(ctx.get("WORDS", 1500) or 1500)
    min_w = int(target_w * 0.7)
    if count_words_zh(_split_body(text)) < min_w:
        print(
            f"    [write] 字数 {count_words_zh(_split_body(text))} < 底线 {min_w}，扩写中 …"
        )
        ctx["_prev_text"] = _split_body(text)
        text2 = writer.rewrite(
            ctx,
            f"篇幅不足：当前约 {count_words_zh(_split_body(text))} 字，目标约 {target_w} 字。"
            f"请扩写到 {int(target_w * 0.95)}-{int(target_w * 1.4)} 字之间（严禁超过 {int(target_w * 1.4)} 字）："
            "把每个情节拍写足，补充对话、动作与细节，不改变情节走向、不删减现有内容、不要概括总结。",
            2,
            "expand",
        )
        if count_words_zh(_split_body(text2)) > int(target_w * 1.8):
            print(f"    [write] 扩写稿 {count_words_zh(_split_body(text2))} 字严重超标，保留原稿")
        else:
            text = _no_shrink(text, text2)
    # --- 门禁零·一：用户禁用道具硬检（确定性，零 token 成本；置于语言质检之前，
    #     先清道具再修语言，避免道具重写引入新的语言问题逃过质检）---
    if banned_regex(cfg) is not None:
        b_max = int(rcfg.get("ban_props_max_rounds", 2))
        b_rnd = 1
        while True:
            brep = scan_banned(_split_body(text), cfg)
            if brep["pass"]:
                report.pop("ban_hits", None)   # 后续轮次修好了：清掉旧命中记录，不留 stale
                break
            report["ban_hits"] = brep["hits"]
            if b_rnd >= b_max:
                if report["gate"] == "pass":
                    report["gate"] = "maxed_banprops"
                print(
                    f"    [banprops] ⚠ 仍命中 {len(brep['hits'])} 种禁用道具"
                    f"（{b_rnd}/{b_max} 轮），已达上限按现稿放行并记录"
                )
                break
            print(
                f"    [banprops] 命中 {len(brep['hits'])} 种禁用道具"
                f"（{_round_marker(b_rnd, b_max)}），带清单重写 …"
            )
            ctx["_prev_text"] = _split_body(text)
            text = writer.rewrite(ctx, format_banned(brep), b_rnd + 1)
            b_rnd += 1
            _save_draft(project, chapter_no, text, stage=f"禁道具第{b_rnd - 1}轮", gate="maxed_banprops")
    # --- 门禁零·二：语言质检（确定性，零 token 成本）---
    if rcfg.get("langqa_enabled", True):
        lq_max = int(rcfg.get("langqa_max_rounds", 2))
        rnd = 1
        while True:
            lqrep = lang_scan(_split_body(text), genres=_gt(ctx.get("DIRECTION")))
            report["langqa_rounds"] = rnd
            report["langqa_hits"] = len(lqrep["hits"])
            report["langqa_pass"] = lqrep["pass"]
            if lqrep["pass"]:
                print(f"    [langqa] 语言质检通过（第{rnd}轮扫描，0 阻断性命中）")
                break
            if rnd >= lq_max:
                if report["gate"] == "pass":
                    report["gate"] = "maxed_langqa"
                print(f"    [langqa] ⚠ 仍 {len(lqrep['hits'])} 处命中，已达上限按现稿放行并记录")
                break
            print(f"    [langqa] {len(lqrep['hits'])} 处命中（{_round_marker(rnd, lq_max)}），带清单重写 …")
            ctx["_prev_text"] = _split_body(text)
            text = writer.rewrite(
                ctx, "语言质检命中清单（逐条修复，情节骨架不动）：\n" + lang_format(lqrep)
                + "\n重写铁律：严格遵守你写稿提示词里的【语言铁律】全部条款——硬禁词一个都不能留、"
                  "限量词按节自查、节奏结构照做；只修语言问题，不得新增情节、不得删减原有内容。", rnd + 1
            )
            rnd += 1
            _save_draft(project, chapter_no, text, stage=f"语言质检第{rnd - 1}轮", gate="maxed_langqa")
    _save_draft(project, chapter_no, text, stage="初稿完成")
    # —— 语言病门（确定性，零 token 判定）——
    # 四类语言病（碎句链 / 语序 / 冗余复读 / 详略失衡）此前只在最终 lang_polish 阶段处理，
    # 而那时章节已经被主编/读者/情绪三门按"情节"放行过了——语言病等于没门。
    # 用户的抱怨正是这四类，所以必须在章节落盘前就地拦一次，且带上确定性清单定点修。
    if rcfg.get("langdefect_enabled", True) and \
            type(getattr(writer, "llm", writer)).__name__ != "MockLLM":
        from . import language as _L

        # 先跑一遍**零 token** 的确定性返修再判分。
        # WHY：合并碎句链、去罗列重复、切超长段这些活根本不需要模型——
        # 之前章级门直接把"机器能改的"交给模型重写，实测每章白烧 1-2 轮
        # （e2e7 日志：28.68 → 11.82 → 8.63，两轮全烧在机械可修的问题上）。
        if rcfg.get("langrepair_enabled", True):
            try:
                _fixed, _rp = _L.deterministic_repair(text)
                if any(int(v or 0) for v in _rp.values() if isinstance(v, (int, float))):
                    text = _fixed
                    report["langrepair"] = _rp
                    _save_draft(project, chapter_no, text, stage="确定性返修", gate="")
            except Exception as e:  # noqa: BLE001
                report["langrepair_error"] = f"{type(e).__name__}: {e}"[:160]

        dmax = int(rcfg.get("langdefect_max_rounds", 2))
        # 章级用放宽门槛：初稿不是终稿，投稿线留给 final polish + 整稿体检把关。
        _ctg = _L.chapter_targets(rcfg.get("langdefect_scale"))
        drows = 0
        while True:
            sc = _L.defect_score(text)
            bad = _L.language_fails(text, targets=_ctg)
            if not bad:
                report["langdefect"] = {"pass": True, "score": sc["score"], "rounds": drows,
                                        "fails": []}
                break
            if drows >= dmax:
                if report["gate"] == "pass":
                    report["gate"] = "maxed_langdefect"
                report["langdefect"] = {"pass": False, "score": sc["score"], "rounds": drows,
                                        "fails": bad}
                print(f"    [语言病] ⚠ {len(bad)} 项超标（{'；'.join(bad[:3])}），"
                      f"已达 {dmax} 轮上限按现稿放行记 maxed_langdefect")
                break
            drows += 1
            print(f"    [语言病] {len(bad)} 项超标（{'；'.join(bad[:3])}），定点修复 …")
            ctx["_prev_text"] = _split_body(text)
            # 只给"哪里有病"不够：同一个模型重写自己的病句，往往把短句拆得更碎
            # （实测 2 轮定点修复后 chain 仍 6.92/千字）。所以同时给范例对照
            # ——按检出的病种取 data/craft.json 里同类的「病句→改后」实例。
            text = writer.rewrite(
                ctx, "语言病定点修复（只改下列问题，情节/人物/对白内容一字不动）：\n"
                     + _L.defect_brief(text)
                     + "\n" + _L.craft_hint(sc)
                     + "\n重写铁律：合并碎句链（同一主语连排的短句合成一句，主语只留一次）；"
                       "语序按中文习惯（数量词前置、AA 叠词每节≤1）；"
                       "同一信息只说一遍；该详的写细、该略的带过。"
                       "禁止新增情节、禁止删减已有事件、字数变化控制在 ±10% 以内。", drows + 1
            )
            _save_draft(project, chapter_no, text, stage=f"语言病第{drows}轮", gate="maxed_langdefect")

    # —— 对白门（确定性，零 token 判定）——
    # 用户诉求里的「详略不当」「冗余」有一半不是句子问题，而是**场面被叙述替代**：
    # 该由两个人当场对峙的地方，被写成三段说明文。实测最近一批成稿对白字占比
    # 0.22–0.32（番茄/七猫短篇常见 0.30–0.40），差的不是"有没有对话"。
    # 判据与体检同源（`language.dialog_ratio` + `DIALOG_MIN_CHAPTER`）。
    if rcfg.get("dialog_gate_enabled", True) and \
            type(getattr(writer, "llm", writer)).__name__ != "MockLLM":
        from . import language as _L2

        _dmin = float(rcfg.get("dialog_ratio_min", _L2.DIALOG_MIN_CHAPTER))
        _dmax = int(rcfg.get("dialog_max_rounds", 2))
        _dr = 0
        while True:
            _rt = _L2.dialog_ratio(text)
            if _rt >= _dmin or _dr >= _dmax:
                report["dialog"] = {"pass": bool(_rt >= _dmin), "ratio": _rt,
                                    "min": _dmin, "rounds": _dr}
                if _rt < _dmin:
                    if report["gate"] == "pass":
                        report["gate"] = "maxed_dialog"
                    print(f"    [对白] ⚠ 对白占比 {_rt:.2%} < {_dmin:.0%}，"
                          f"已达 {_dmax} 轮上限按现稿放行记 maxed_dialog")
                break
            _dr += 1
            print(f"    [对白] 对白占比 {_rt:.2%} < {_dmin:.0%}，改写成场面 …")
            _prev = text
            ctx["_prev_text"] = _split_body(text)
            text = writer.rewrite(
                ctx, "场面化改写（只改表达方式，不动情节）：\n"
                     f"当前全章对白字占比 {_rt:.1%}，低于 {_dmin:.0%}——说明有几处"
                     "该由角色当场说的话，被你写成了旁白说明。\n"
                     f"目标：改写后对白字占比 ≥ {_dmin + 0.05:.0%}。\n"
                     "做法：挑 2–3 处最该吵架/最该摊牌/最该讨价还价的段落，改成"
                     "两人（或多人）当场对话——每句台词带态度和目的，不是把信息"
                     "复述一遍；叙述只保留交代时空与动作的最少字数。\n"
                     "禁止新增事件、禁止改变结局、禁止让角色说出他不可能知道的事。\n"
                     "重写铁律：字数变化控制在 ±15% 以内；输出仍以原小节编号单独一行开头。",
                _dr + 1
            )
            # 单调性守卫：实测有 23.4% 被改成 18.1% 的案例（模型把两处对峙
            # 又通并回了概述）。改不动/改反了就保留原稿，别让一轮预算花成负收益。
            _new_rt = _L2.dialog_ratio(text)
            if _new_rt <= _rt + 0.01:
                print(f"    [对白] 第{_dr}轮改写未提升（{_rt:.1%}→{_new_rt:.1%}），保留原稿")
                text = _prev
                break
            _save_draft(project, chapter_no, text, stage=f"对白第{_dr}轮", gate="maxed_dialog")
    return text, report


def _gate_review_reader(
    cfg: dict,
    writer: WriterAgent,
    editor: ChiefEditor,
    panel: ReaderPanel | None,
    ctx: dict,
    text: str,
    chapter_no: int,
    *,
    project=None,
) -> tuple[str, dict]:
    """阶段二：主编审查 + 读者团评分（章间无依赖，可并行）。

    返回 (可能被重写后的正文, 报告片段)。gate 状态在此初始化并在打回/未达标时标记。
    """
    rcfg = cfg["review"]
    max_review = int(rcfg.get("max_review_rounds", 3))
    max_reader = int(rcfg.get("max_reader_rounds", 2))
    threshold = float(rcfg.get("score_threshold", 7.5))
    report = {"no": chapter_no, "gate": "pass"}
    # --- 门禁一：主编审查 ---
    ctx["_prev_text"] = ""
    verdict = {"verdict": "pass", "score": None}
    degraded_review = False
    for rnd in range(1, max_review + 1):
        if rnd > 1:
            print(f"    [review] 第{rnd-1}轮被主编打回（{_round_marker(rnd-1, max_review)}），重写中 …")
            _fb = fmt_issues(verdict)
            _gaps = fmt_rubric_gaps(verdict)
            if _gaps:
                _fb = _fb + "\n" + _gaps
            text = _no_shrink(text, writer.rewrite(ctx, _fb, rnd))
            _save_draft(project, chapter_no, text, stage=f"主编打回第{rnd - 1}轮", gate="maxed_review")
        body = _split_body(text)
        verdict = editor.review(ctx, body, rnd, max_review)
        report["review_rounds"] = rnd
        # 主编的 0-10 分要与它自己的评分卡自洽：实测 7 章里模型手填 4.5-6.2，
        # 而同一份输出的 earned/max 折算却是 5.9-8.0（系统性低 1.5-2 分）。
        # 有评分卡时以得分率为准，手填分留档在 review_score_raw 供比对。
        _rubric_sc = review_mod.score_from_criteria(verdict)
        _raw_sc = verdict.get("score")
        report["review_score_raw"] = _raw_sc
        report["review_score"] = _rubric_sc if _rubric_sc is not None else _raw_sc
        report["review_score_src"] = "rubric" if _rubric_sc is not None else "model"
        if str(verdict.get("verdict")).lower() == "degraded":
            # LLM 解析失败：不重写（重写只会再喂一次坏输出），也不当 pass。
            # 记 degraded_review 交上游，report 里能一眼看出"这门没真正审过"。
            degraded_review = True
            report["review_error"] = verdict.get("error", "")
            print("    [review] ⚠ 主编审查降级（LLM 输出不可解析），本门不计通过")
            break
        # 通过判据：无 high 级问题 + 分数达阈值。分数优先用评分卡折算值，
        # 因为它是"证据驱动"的；模型手填的 verdict 字段只在没有评分卡时才作数。
        if _rubric_sc is not None:
            _ok = (_rubric_sc >= threshold) and not review_mod.has_high_issue(verdict)
            if _ok and str(verdict.get("verdict")).lower() != "pass":
                report["review_verdict_overridden"] = (
                    f"模型判 fail/{_raw_sc}，但评分卡 {_rubric_sc} 达阈值 {threshold} 且无 high 问题")
                print(f"    [review] 评分卡 {_rubric_sc} ≥ {threshold} 且无 high 问题，本门通过"
                      f"（模型手填 {_raw_sc}，已留档比对）")
                break
            if not _ok:
                continue
            break
        if str(verdict.get("verdict")).lower() == "pass":
            break

    else:
        report["gate"] = "maxed_review"
        print(f"    [review] ⚠ 已达审查轮次上限，按第{max_review}稿放行并记录")
    if degraded_review:
        report["gate"] = "degraded_review"
    # 保留最后一轮主编原始输出：证据化评审卡（studio/review.py）要用它反推
    # criteria/evidence。只留 issues 会丢 status/evidence，重算覆盖率就失真了。
    # reviewed_text_sha 记录"这轮 verdict 是对哪一版正文给的"：终稿复查可能重写
    # 正文，届时评审卡要能识别出"证据失效是版本漂移，不是模型编造"。
    report["review_verdict"] = {
        "verdict": verdict.get("verdict"),
        "score": verdict.get("score"),
        "issues": verdict.get("issues") or [],
        "criteria": verdict.get("criteria") or [],
        # 指纹取"主编最后看到的那一版正文"（body），不是外层 text：
        # 两者在"重写后又剥掉摘要行"的场景下会不同，评审卡核对的是前者。
        # reviewed_text 只留 4000 字：足够覆盖正文引文，又不至于把每章 json 撑爆。
        "reviewed_text_sha": review_mod.text_sha(body),
        "reviewed_text": body[:4000],
    }
    _sc_note = ""
    if report.get("review_score_src") == "rubric" and report.get("review_score_raw") is not None:
        _sc_note = f"，评分卡折算 {report['review_score']}（模型手填 {report['review_score_raw']}）"
    print(
        f"    [review] 主编第{report['review_rounds']}轮审查：{verdict.get('verdict')}"
        f"（{verdict.get('score')}分{_sc_note}）"
    )
    # --- 门禁二：读者团评分 ---
    report["reader_scores"] = {}
    report["reader_avg"] = None
    if panel is not None:
        body = _split_body(text)
        scores = panel.score(ctx, body, 1)
        avg = statistics.mean(s["score"] for s in scores)
        rnd = 1
        while avg < threshold and rnd < max_reader:
            print(
                f"    [reader] 读者团均分 {avg:.1f} < {threshold}（{_round_marker(rnd, max_reader)}），带反馈重写 …"
            )
            ctx["_prev_text"] = body
            text = writer.rewrite(ctx, "读者团反馈（逐条吸收）：\n" + fmt_reader_feedback(scores), rnd + 1)
            _save_draft(project, chapter_no, text, stage=f"读者团第{rnd}轮", gate="maxed_reader")
            body = _split_body(text)
            rnd += 1
            scores = panel.score(ctx, body, rnd)
            avg = statistics.mean(s["score"] for s in scores)
        report["reader_rounds"] = rnd
        report["reader_scores"] = {s.get("persona", "?"): s["score"] for s in scores}
        report["reader_avg"] = round(avg, 2)
        if avg < threshold:
            report["gate"] = "maxed_reader"
            print(f"    [reader] ⚠ 读者均分 {avg:.1f} 仍未达标，按现稿放行并记录")
        else:
            print(
                f"    [reader] 读者团第{rnd}轮：均分 {avg:.1f} ✔（{panel.names}）"
            )
    return text, report


def _gate_emotion(
    cfg: dict,
    editor: ChiefEditor,
    writer: WriterAgent,
    ctx: dict,
    text: str,
    prev_emotion: str,
    chapter_no: int,
    *,
    project=None,
) -> tuple[str, dict]:
    """阶段三：情绪量化门。依赖上一章 dominant（prev_emotion），保持串行收尾。

    返回 (可能被重写后的正文, 报告片段)。gate 在此可能置 maxed_emotion。
    """
    rcfg = cfg["review"]
    # 情绪门轮次独立配置；未配置时回退读者门轮次（向后兼容旧配置）
    e_max = int(rcfg.get("max_emotion_rounds", rcfg.get("max_reader_rounds", 2)))
    report = {"no": chapter_no, "gate": "pass"}
    report["emotion"] = None
    if rcfg.get("emotion_enabled", True):
        body_now = _split_body(text)
        rnd = 1
        while True:
            emo = emotion_check(editor.llm, ctx, body_now, rnd, e_max, prev_emotion)
            ok, why = emotion_verdict_ok(emo, rcfg)
            degraded = emo.get("verdict") == "degraded"
            report["emotion"] = {
                "round": rnd,
                "emotion_score": emo.get("emotion_score"),
                "hook_score": emo.get("hook_score"),
                "nue_ratio": emo.get("nue_ratio"),
                "dominant": emo.get("dominant"),
                "verdict": "degraded" if degraded else ("pass" if ok else ("fail" if rnd < e_max else "maxed")),
            }
            if degraded:
                # LLM 不可解析：重写也是拿同一份坏输出做判断，直接记降级交上游。
                report["emotion_error"] = emo.get("error", "")
                if report["gate"] == "pass":
                    report["gate"] = "degraded_emotion"
                print(f"    [emotion] ⚠ {why}，本门不计通过（已记降级）")
                break
            if ok:
                print(
                    f"    [emotion] 情绪门通过：情绪分 {emo.get('emotion_score')} · "
                    f"钩子 {emo.get('hook_score')} · 虐占比 {emo.get('nue_ratio')}"
                )
                break
            if rnd >= e_max:
                if report["gate"] == "pass":
                    report["gate"] = "maxed_emotion"
                print(f"    [emotion] ⚠ {why}，已达上限按现稿放行并记录")
                break
            print(f"    [emotion] {why}（{_round_marker(rnd, e_max)}），重写情绪线 …")
            ctx["_prev_text"] = body_now
            emo_issues = "\n".join(
                f"- {i.get('note', '')} → {i.get('fix', '')}" for i in (emo.get("issues") or [])
            )
            text = writer.rewrite(
                ctx, "情绪审查意见（只重写情绪线：加虐点/爽点/强化钩子，情节骨架不动）：\n"
                + (emo_issues or why),
                rnd + 1,
            )
            _save_draft(project, chapter_no, text, stage=f"情绪门第{rnd}轮", gate="maxed_emotion")
            body_now = _split_body(text)
            rnd += 1
    return text, report


def _save_draft(project, chapter_no: int, text: str, *, stage: str = "",
                gate: str = "") -> None:
    """保一份章节草稿（B03）。project 为 None 时静默跳过（纯函数调用/单测路径）。

    为什么容错而不是抛错：保稿是**增强**，不是正确性前提。磁盘满、权限不足时
    不能因为"存不下草稿"就把整章写作搞崩——正文还在内存里，继续跑完才有机会
    正常提交。失败只记一行日志，不打断流程。
    """
    if project is None:
        return
    try:
        from . import drafts

        body = _split_body(text)
        drafts.save_draft(project, chapter_no, body, stage=stage, gate=gate,
                          model=getattr(project, "_draft_model", ""))
    except Exception as e:  # noqa: BLE001
        print(f"    [draft] ⚠ 草稿保存失败（不影响写作）：{type(e).__name__}: {e}")


def _split_body(text: str) -> str:
    """去掉 [本章摘要] 尾巴，供审查/评分用。"""
    marker = "[本章摘要]"
    return text.partition(marker)[0].strip() if marker in text else text.strip()


# ---------------------------------------------------------------------------
# 终稿确定性门禁复查（串联修复）
# ---------------------------------------------------------------------------
# 缺陷背景（2026-10-26 实测）：确定性门禁（思考泄漏 / 禁用道具 / 语言质检）只在
# _chapter_core 里跑一次，之后的主编审查、读者团、情绪门每一轮都是**LLM 重写**，
# 重写会把已经清掉的禁用道具、思考泄漏原样写回来，而没有任何门再复查：
#   · 第 8 章初稿 ban 门命中「欠条」→ 重写清除 → 主编打回重写 → 终稿又出现「欠条」，
#     ch08.json 却记录 ban_hits=0；
#   · 8 章合计 12 万字里混入 912 行英文思维链（第 2 章 34.7% 行是英文），
#     langqa/think_leak 判定完全没被调用到终稿上。
# 因此终稿必须按「最后一轮重写之后」的文本重新跑全套确定性检查，且三项要串联：
# 修 A 的 LLM 重写可能引入 B，所以要循环到全清或轮次用尽。
_DETERMINISTIC_MAX_ROUNDS = 3


def deterministic_recheck(
    cfg: dict,
    writer: WriterAgent,
    ctx: dict,
    text: str,
    max_rounds: int = _DETERMINISTIC_MAX_ROUNDS,
    full_scope: bool = False,
) -> tuple[str, dict]:
    """对终稿重跑全套确定性门禁，命中就带清单重写，循环到全清或用尽轮次。

    检查项（零 token 判定，命中才花 token 重写）：
      ① 思考泄漏 think_leak —— 思维链/英文提纲当正文（历史事故：19 万字）
      ② 禁用道具 scan_banned —— config.kb.negative_terms
      ③ 语言质检 lang_scan —— 黑名单/词频

    full_scope=False（章级）：走 writer.rewrite（按章 beat 重写）。
    full_scope=True（成稿级）：走 writer.rewrite_full（无章级上下文的定点修复）。

    返回 (修复后的文本, {"pass": bool, "rounds": n, "hits": {...}})。
    """
    # 防御：调用方若传了裸 LLM（resolve_llm 的返回值）而不是 WriterAgent，
    # 这里自动包一层。历史 bug：stage_polish 传 LLM 进来，mock 模式下
    # AttributeError 被上层 except 吞掉，成稿复查静默失效。
    if not hasattr(writer, "rewrite_full"):
        if hasattr(writer, "chat"):
            writer = WriterAgent(writer)  # type: ignore[arg-type]
        else:
            raise TypeError(f"writer 需为 WriterAgent 或 BaseLLM，收到 {type(writer).__name__}")
    rcfg = cfg["review"]
    report: dict = {"pass": True, "rounds": 0, "hits": {}}
    # mock/离线模式：MockLLM 永远返回同一段模板文本，重写不可能消除命中，
    # 只会空转 3 轮拖慢冒烟测试。直接跳过（与 word_gate 的 skipped_mock 一致）。
    if type(getattr(writer, "llm", writer)).__name__ == "MockLLM":
        return text, {"pass": True, "rounds": 0, "hits": {}, "gate": "skipped_mock"}
    for rnd in range(1, max_rounds + 1):
        body = _split_body(text)
        fixes: list[str] = []
        # ① 思考泄漏优先级最高：整篇不是小说正文时，语言/道具统计都无意义
        if think_leak(body):
            fixes.append(think_leak_hint())
            report["hits"]["think_leak"] = True
        # ② 禁用道具
        if banned_regex(cfg) is not None:
            brep = scan_banned(body, cfg)
            if not brep["pass"]:
                fixes.append(format_banned(brep))
                report["hits"]["banned"] = brep["hits"]
        # ③ 语言质检
        if rcfg.get("langqa_enabled", True):
            lqrep = lang_scan(body, genres=_gt(ctx.get("DIRECTION")))
            if not lqrep["pass"]:
                fixes.append("语言质检命中清单（逐条修复，情节骨架不动）：\n" + lang_format(lqrep))
                report["hits"]["langqa"] = len(lqrep["hits"])
        report["rounds"] = rnd
        if not fixes:
            report["pass"] = True
            report["hits"] = {}
            break
        if rnd == max_rounds:
            report["pass"] = False
            print(f"    [recheck] ⚠ 终稿复查 {max_rounds} 轮仍有命中，按现稿放行并记录")
            break
        print(f"    [recheck] 终稿复查命中 {len(fixes)} 类问题（第{rnd}/{max_rounds}轮），重写 …")
        ctx = {**ctx, "_prev_text": body}
        prompt = (
            "\n\n".join(fixes)
            + "\n\n重写铁律：只修上面点名的东西，情节/人物/对话/节末钩子一律不动；"
              "全中文输出，不得出现英文分析、提纲或自我检查。"
        )
        if full_scope:
            text = writer.rewrite_full(ctx, prompt, rnd + 1)
        else:
            text = writer.rewrite(ctx, prompt, rnd + 1)
        # 无进展即止损：`_guard_rewrite` 会把"只输出修改片段"的回应判为坍缩并
        # 保留原稿，此时 `text` 与 `body` 一模一样——继续下一轮只会再烧一次调用
        # 得到同样的片段。实测 e2e13 终稿复查连打 3 轮（每轮 ~900 token），
        # 稿子一个字没改，最后仍记 `pass=false`。这里直接以 no_progress 退出，
        # 把"改不动"这件事如实记进 report，而不是伪装成"修了 3 轮"。
        if text.strip() == body.strip():
            report["pass"] = False
            report["gate"] = "no_progress"
            print(f"    [recheck] ⚠ 第{rnd}轮重写无实际改动（只回了片段或触发长度守卫），"
                  "停止空转并记录")
            break
    return text, report


def split_summary(text: str) -> tuple[str, str]:
    body, _, summ = text.partition("[本章摘要]")
    return body.strip(), summ.strip()[:300]


__all__ = [
    "WriterAgent",
    "ChiefEditor",
    "ReaderPanel",
    "write_chapter_with_gates",
    "_split_summary",
    "deterministic_recheck",
]
