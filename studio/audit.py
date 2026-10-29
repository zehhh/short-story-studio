"""成稿体检（单一权威源）。

为什么必须有这一层：体检判据此前只存在于 `tools/manuscript_audit.py` 一个脚本里，
流水线自己另算 `platform_check` + `market_lang` 两个并列字段，看板再读第三份。
结果是同一篇稿子"体检说 B、投稿判定说 A"（e2e8/e2e9/e2e11 实测）。

本模块只做**纯计算**（输入文本 + 可选 report，输出 checks/grade/fails/warns），
不碰文件系统：
  · `tools/manuscript_audit.py` 负责读文件、渲染、写复检结论；
  · `studio/pipeline.py` 在成稿落盘后调 `audit_text(submit_text, report)`，
    把结果写进 `report["audit"]` 并作为 `submission()` 的硬线输入。

阈值全部来自 `studio/language.py::MARKET_TARGET`，本模块不写任何数字。
"""

from __future__ import annotations

import re

from . import langqa, language
from .util import sec_head_no

# —— 判据阈值：全部来自 MARKET_TARGET（单一权威源）——
MT = language.MARKET_TARGET
SCORE_PER_K = MT["score_per_k"]
SCORE_PER_K_BAD = MT["score_per_k"] * 4.5
CHAIN_PER_K_MAX = MT["chain_per_k"]
FRAG_PER_K_MAX = MT["frag_per_k"]
LONG_PER_K_MAX = MT["longsen_per_k"]
ORDER_PER_K_MAX = MT["order_per_k"]
EURO_PER_K_MAX = MT["euro_per_k"]
REDUN_PER_K_MAX = MT["redundancy_per_k"]
DETAIL_PER_K_MAX = MT["detail_per_k"]
DIALOG_RATIO_MIN = MT["dialog_ratio_min"]
SECTION_RATIO_MAX = MT["section_ratio_max"]
WORDS_MIN, WORDS_MAX = MT["words_min"], MT["words_max"]
LONG_PARA_CHARS = MT["long_para_chars"]
REVIEW_PASS = 7.0


def sections(text: str) -> list[tuple[str, str]]:
    """按权威节头切成 [(no, body)]；节头判据只走 util.sec_head_no。"""
    out: list[tuple[str, str]] = []
    cur_no, buf = "", []
    for ln in text.split("\n"):
        no = sec_head_no(ln)
        if no is not None:
            if cur_no or buf:
                out.append((cur_no, "\n".join(buf).strip()))
            cur_no = f"{no:02d}"
            buf = []
        else:
            buf.append(ln)
    if cur_no or buf:
        out.append((cur_no, "\n".join(buf).strip()))
    if out and out[0][0] == "":
        # 节头之前的内容是「标题 + 导语」，不属于任何小节：不能并进 01，
        # 否则 01 凭空多出 400 字，节长极差被虚报（e2e5 实测 1.36× 变 1.69×）。
        out.pop(0)
    return out


def audit_text(text: str, report: dict | None = None, direction: str = "") -> dict:
    """体检一段成稿文本（标题+导语+正文的**投稿全文**）。

    `report` 可选：带上就能多评 E 门禁组（主编分/读者团/平台检查/语言打磨执行）。
    `direction` 可选：题材名，决定哪一组 `genre` 语言规则生效（古言/都市的 AI 味
    套词不是同一批；不带题材时只跑通用规则，避免跨题材噪音）。
    """
    text = text or ""
    rep = report or {}
    if not direction:
        direction = str(rep.get("direction") or rep.get("genre") or "")
    n_chars = len(re.sub(r"\s", "", text))
    secs = sections(text)
    bodies = [(no, b) for no, b in secs if b]
    lens = [len(re.sub(r"\s", "", b)) for _, b in bodies] or [0]

    sc = language.defect_score(text)
    k = max(n_chars, 1) / 1000.0
    dens = {key: round(float(sc.get(key) or 0) / k, 2) for key in
            ("score", "frag", "longsen", "chain", "order", "euro", "redundancy",
             "detail", "dup")}

    paras = [p.strip() for p in text.split("\n") if p.strip()]
    # 对白占比与章级门同源：按引号内**字数**算，不数"含引号的段落"
    # （后者对长段落天然偏高，同一篇稿子两个数——正是要消灭的判据分裂）。
    ratio = language.dialog_ratio(text)
    long_paras = [i for i, p in enumerate(paras)
                  if len(p) > LONG_PARA_CHARS and not langqa._DIALOG_MARK.search(p)]

    curly = len(re.findall(r"[“”]", text))
    think = langqa.think_leak(text)
    heads = [no for no, _ in bodies]
    seq_ok = heads == [f"{i:02d}" for i in range(1, len(heads) + 1)]

    checks: list[dict] = []

    def add(group: str, name: str, ok: bool, got, want: str, level: str = "fail"):
        checks.append({"group": group, "name": name, "ok": bool(ok),
                       "got": got, "want": want, "level": level})

    # A 结构
    add("A结构", "字数", WORDS_MIN <= n_chars <= WORDS_MAX, n_chars, f"{WORDS_MIN}—{WORDS_MAX}")
    add("A结构", "节数", 5 <= len(bodies) <= 12, len(bodies), "5—12 节")
    ratio_len = round(max(lens) / max(min(lens), 1), 2)
    add("A结构", "节长极差", ratio_len <= SECTION_RATIO_MAX, ratio_len,
        f"≤{SECTION_RATIO_MAX}×（最短 {min(lens)} / 最长 {max(lens)}）")
    head_txt = "\n".join(text.split("\n")[:6])
    add("A结构", "导语", "导语" in head_txt, "有" if "导语" in head_txt else "无", "开头有「导语：」")

    # B 语言
    add("B语言", "缺陷总分/千字", dens["score"] <= SCORE_PER_K, dens["score"],
        f"≤{SCORE_PER_K}（>{round(SCORE_PER_K_BAD, 2)} 不可投）")
    add("B语言", "碎句/千字", dens["frag"] <= FRAG_PER_K_MAX, dens["frag"], f"≤{FRAG_PER_K_MAX}")
    add("B语言", "长句/千字", dens["longsen"] <= LONG_PER_K_MAX, dens["longsen"],
        f"≤{LONG_PER_K_MAX}（与碎句相反方向的病，单独计量）")
    add("B语言", "碎句链/千字", dens["chain"] <= CHAIN_PER_K_MAX, dens["chain"], f"≤{CHAIN_PER_K_MAX}")
    add("B语言", "语序/千字", dens["order"] <= ORDER_PER_K_MAX, dens["order"], f"≤{ORDER_PER_K_MAX}")
    add("B语言", "欧化/千字", dens["euro"] <= EURO_PER_K_MAX, dens["euro"],
        f"≤{EURO_PER_K_MAX}", "warn")
    add("B语言", "冗余/千字", dens["redundancy"] <= REDUN_PER_K_MAX, dens["redundancy"],
        f"≤{REDUN_PER_K_MAX}")
    add("B语言", "详略/千字", dens["detail"] <= DETAIL_PER_K_MAX, dens["detail"],
        f"≤{DETAIL_PER_K_MAX}")

    # C 节奏
    add("C节奏", "对话段占比", ratio >= DIALOG_RATIO_MIN, round(ratio, 3), f"≥{DIALOG_RATIO_MIN}")
    add("C节奏", "超长无对话段", not long_paras, len(long_paras),
        f"没有 >{LONG_PARA_CHARS} 字纯叙述段")

    # D 规范
    add("D规范", "引号统一", curly == 0, f"“”×{curly}", "不得出现 “ ”（应统一为「」）")
    add("D规范", "思考泄漏", not think, think, "无思维链残留")
    add("D规范", "节头编号", seq_ok, heads[:6], "01…N 严格递增无跳号")
    # AI 味口癖 / 黑名单道具：mid 级拉低观感但可读，判 warn（→ B 可投）；
    # high 级判 fail（→ C 不可投）。体检必须与 `report["langqa_full"]` 同源，
    # 否则会出现"体检 A、report 里 langqa_full.pass=false"（e2e13）。
    lq = langqa.scan(text, genres=langqa.genre_tags(direction))
    hi = [h for h in lq["hits"] if h.get("severity") == "high"]
    mid = [h for h in lq["hits"] if h.get("severity") != "high"]
    add("D规范", "AI味/黑名单(硬)", not hi,
        "；".join(h["label"] for h in hi[:3]) or "无", "无 high 级命中（AI 味模板/硬性黑名单）")
    add("D规范", "AI味/黑名单", not mid,
        "；".join(h["label"] for h in mid[:3]) or "无",
        "无 mid 级命中（口癖、记仇道具等措辞瑕疵）", "warn")

    # E 门禁（有 report 才评）
    if rep:
        rs = rep.get("review_score")
        add("E门禁", "主编评分", (rs or 0) >= REVIEW_PASS, rs,
            f"≥{REVIEW_PASS}（rubric 折算）", "warn")
        mr = rep.get("maxed_reader") or []
        add("E门禁", "读者团", not mr, "有 maxed_reader" if mr else "通过", "全部 ≥7.5", "warn")
        pc = rep.get("platform_check") or {}
        add("E门禁", "平台检查", bool(pc.get("pass")), pc.get("pass"), "pass=true")
        lp = rep.get("lang_polish") or {}
        add("E门禁", "语言打磨执行", bool(lp.get("enabled")) and not lp.get("error"),
            lp.get("before"), "before/after 有记录且无 error", "warn")

    fails = [c for c in checks if not c["ok"] and c["level"] == "fail"]
    warns = [c for c in checks if not c["ok"] and c["level"] == "warn"]
    from . import verdict as _verdict

    grade = _verdict.grade(fails, warns)

    per_sec = []
    for no, body in bodies:
        s = language.defect_score(body)
        per_sec.append({"no": no, "chars": len(re.sub(r"\s", "", body)), "score": s["score"],
                        "chain": s["chain"], "frag": s["frag"], "longsen": s["longsen"],
                        "order": s["order"], "redundancy": s["redundancy"],
                        "detail": s["detail"], "euro": s["euro"]})
    per_sec.sort(key=lambda x: -x["score"])
    return {
        "chars": n_chars, "sections": [no for no, _ in bodies],
        "density": dens, "dialog_ratio": round(ratio, 3), "long_paras": len(long_paras),
        "curly_quotes": curly, "level_ratio": ratio_len,
        "checks": checks, "grade": grade,
        "fails": [f"{c['group']}/{c['name']}" for c in fails],
        "warns": [f"{c['group']}/{c['name']}" for c in warns],
        "worst_sections": per_sec[:4],
    }
