"""跨节连续性检查：人物一致 / 线索承接 / 跨节重复。

纯计算、零 token。判据只在这里实现一份，写前注入与写后检查都调它。

为什么需要这个模块（2026-11-01 女主后宫实测稿的三处硬伤）：
1. **人物断档**——第 03/05/08 节一直是陪嫁丫鬟「青禾」，第 11 节廊下回话的人
   突然变成「阿蘅」，前文零交代。逐节写每节是独立对话，写手看不到前文
   人物名册，于是随手编一个新名字顶上。
2. **线索断头**——第 09 节周奉站出来请求验看蜡丸字条，戏刚起，第 10 节整段
   消失，只剩一句「周奉仍站在原地，等着皇帝示下」，再无下文。
3. **跨节雷同**——第 10 节又写了一遍第 09 节的
   「凤冠上的珠翠剧烈晃动，厉声喝道：大胆贱婢」，16 字逐字重复。

三条判据的共同点：**它们都不是文字问题，是装配问题**。逐节写每节能拿 A，
拼起来就是 B——所以检查必须发生在装配之后，而不是每节写完就完事。

## 判据只对「已声明」的东西下判断

前两版试过从正文里猜人名（「动词前 2–3 字」「角色词 + 2 字」），真实稿上
一次抓出 30 多个假名字：「则阴狠」「立刻捧」「手中」「脸色」「浑身」……
中文里没有词典就分不清「侍女阿蘅」和「嬷嬷立刻」。误报多到人不敢看，
真正的「阿蘅」反而被一起忽略。

所以这一版**不做任何名字猜测**：
- 人物检查只针对 `section_plan.characters` 声明过的人物表——「谁在哪几节出现」
  用字符串精确匹配，零误报；能查出的最强信号是「青禾在第 09 节之后彻底消失」，
  而 11 节的新名字正是补这个洞来的。
- 线索检查只针对 `section_plan.sections[].carry_out` 声明过的交接项——没声明
  就不猜（v1 用 end_hook 关键词重叠，9 个交界抓出 5 个假断链，全是同义改写）。
- 跨节重复是纯文本比对，不依赖任何声明，直接判死。

未声明的一律返回 `skipped`，让报告诚实说「没查」，而不是给一堆假阳性。
"""

from __future__ import annotations

import re


def _sec_no(s: dict) -> str:
    return str(s.get("no") or "").strip()


def _n_gram_hit(a: str, b: str, n: int = 4) -> str:
    """a 里有没有长度 ≥n 的片段逐字出现在 b 里；返回最长命中片段（无则空串）。"""
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if len(a) < n or len(b) < n:
        return ""
    best = ""
    for i in range(len(a) - n + 1):
        for size in range(len(a) - i, n - 1, -1):
            seg = a[i:i + size]
            if seg in b:
                if len(seg) > len(best):
                    best = seg
                break
    return best


def _norm_cast(cast) -> list[dict]:
    """人物表归一：接受 `["青禾", ...]` 或 `[{"name","major","pov"}, ...]`。

    字符串一律按「重要配角」处理（会被断档检查覆盖）。`pov=True` 的是
    第一人称主角——短篇正文里她基本只以「我」出现，名字几乎不落地，
    拿名字去查必然误判「从未出场」，所以整条跳过。
    """
    out = []
    for c in cast or []:
        if isinstance(c, dict):
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            out.append({"name": name,
                        "major": bool(c.get("major", True)),
                        "pov": bool(c.get("pov", False))})
        else:
            name = str(c).strip()
            if name:
                out.append({"name": name, "major": True, "pov": False})
    return out


def cast_coverage(secs: list[dict], cast) -> dict:
    """声明过的人物表在哪几节出现——精确匹配，不猜名字。

    返回 `{name: {"sections": [...], "missing": bool, "dropped_at": str|None}}`。
    `dropped_at` = 最后一次出现的下一节（此后除结尾外再没露面）。
    """
    out: dict[str, dict] = {}
    for c in _norm_cast(cast):
        name = c["name"]
        where = [_sec_no(s) for s in secs if name in (s.get("text") or "")]
        out[name] = {"sections": where, "missing": not where, "dropped_at": None,
                     "major": c["major"], "pov": c["pov"]}
    last_seen = {n: r["sections"][-1] for n, r in out.items() if r["sections"]}
    if last_seen and secs:
        tail = _sec_no(secs[-1])
        latest = max(last_seen.values())
        for name, last in last_seen.items():
            rec = out[name]
            if last == latest or name == max(last_seen, key=lambda k: last_seen[k]):
                continue
            nxt = _next_sec(secs, last)
            if nxt and nxt != tail:
                rec["dropped_at"] = nxt
    return out


def _next_sec(secs: list[dict], no: str) -> str:
    for i, s in enumerate(secs):
        if _sec_no(s) == no:
            return _sec_no(secs[i + 1]) if i + 1 < len(secs) else ""
    return ""


def cast_gaps(secs: list[dict], cast) -> dict:
    """人物断档（硬判）：**重要且非视角**人物中途退场，或声明了却从未出场。

    只查这两类，是因为另外两类消失是正常的：
    - 视角主角（`pov`）通篇写作「我」，名字不落地；
    - 一场戏里的路人配角（`major=False`）本来就出场一次就走（翠儿、王太令）。

    真正要拦的是「青禾从第 09 节起消失，第 11 节换成了阿蘅」——那是人设换了人。
    """
    norm = _norm_cast(cast)
    if not norm:
        return {"gaps": [], "skipped": True, "declared": 0}
    cov = cast_coverage(secs, norm)
    gaps = []
    for name, rec in cov.items():
        if rec["pov"] or not rec["major"]:
            continue
        if rec["missing"]:
            gaps.append({"name": name, "kind": "missing",
                         "why": "计划人物表里的重要人物，正文从未出场"})
        elif rec["dropped_at"]:
            gaps.append({"name": name, "kind": "dropped",
                         "last": rec["sections"][-1], "at": rec["dropped_at"],
                         "why": f"最后一次出现在第 {rec['sections'][-1]} 节，"
                                f"第 {rec['dropped_at']} 节起再没露面"})
    return {"gaps": gaps, "skipped": False, "declared": len(cov)}


def thread_gaps(secs: list[dict], plan_sections: list[dict] | None = None,
                min_hit: int = 4) -> dict:
    """线索断头（硬判）：上一节声明要留给下一节的东西，下一节没有回应。

    只在计划写了 `carry_out` 时判——没声明就不猜。
    """
    plan_sections = plan_sections or []
    carry = {}
    for i, s in enumerate(plan_sections, 1):
        try:
            no = int(s.get("no") or i)
        except (TypeError, ValueError):
            continue
        c = (s.get("carry_out") or "").strip()
        if c:
            carry[no] = c
    gaps = []
    for idx, s in enumerate(secs[:-1]):
        try:
            no = int(_sec_no(s))
        except ValueError:
            continue
        c = carry.get(no)
        if not c:
            continue
        nxt = secs[idx + 1].get("text") or ""
        # 先按 4 字连续片段判；判不过再退到 3 字。carry_out 在计划里被要求写成
        # 6-12 字的具体名词（见 prompts/section_plan.md 铁律 9），本身就是「可被
        # 逐字复述」的短语，退一档不会误伤；而在旧格式（一句话概述）下 4 字几乎
        # 不可能连续命中，会制造大量假断头（实测 9 个交接项报 2 个假阳性）。
        if not (_n_gram_hit(c, nxt, min_hit) or _n_gram_hit(c, nxt, 3)):
            gaps.append({"no": _sec_no(s), "next": _sec_no(secs[idx + 1]),
                         "carry_out": c[:80]})
    return {"gaps": gaps, "declared": len(carry), "skipped": not carry}


def dup_across(secs: list[dict], min_len: int = 14) -> list[dict]:
    """跨节逐字重复：第 i 节与前面任意一节逐字相同、长度 ≥ min_len 的片段。"""
    from .langqa import cross_dup

    out: list[dict] = []
    prev: list[str] = []
    for s in secs:
        text = s.get("text") or ""
        for d in cross_dup(text, prev, n=min_len):
            out.append({"no": _sec_no(s), "prev": d.get("prev"),
                        "len": d.get("len"), "text": d.get("text")})
        prev.append(text)
    return out


def report(secs: list[dict], plan_sections: list[dict] | None = None,
           cast: list[str] | set[str] | None = None, min_dup: int = 14) -> dict:
    """三项检查一次跑完，返回可直接落盘/打印的结构。"""
    cg = cast_gaps(secs, cast or [])
    tg = thread_gaps(secs, plan_sections)
    dups = dup_across(secs, min_dup)
    hard = bool(cg["gaps"] or tg["gaps"] or dups)
    return {
        "pass": not hard,
        "cast": cg["gaps"],
        "cast_declared": cg["declared"],
        "cast_skipped": cg["skipped"],
        "threads": tg["gaps"],
        "threads_declared": tg["declared"],
        "threads_skipped": tg["skipped"],
        "dups": dups,
        "n": {"cast": len(cg["gaps"]), "threads": len(tg["gaps"]),
              "dups": len(dups)},
    }


def summary(rep: dict) -> str:
    """一行摘要，给日志用。"""
    n = rep.get("n") or {}
    parts = []
    for key, label in (("cast", "人物断档"), ("threads", "线索断头"),
                       ("dups", "跨节重复")):
        if n.get(key):
            parts.append(f"{label} {n[key]}")
    if rep.get("cast_skipped"):
        parts.append("人物：计划未声明人物表，跳过")
    if rep.get("threads_skipped"):
        parts.append("线索：计划未声明交接项，跳过")
    return "；".join(parts) if parts else "人物/线索/重复三项均无问题"
