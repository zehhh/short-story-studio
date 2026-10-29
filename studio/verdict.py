"""投稿判定（单一权威源）。

为什么要单独一个模块：同一件事——"这篇能不能投"——此前有三处**各自实现**：

1. `studio/cli.py` 只看 `word_gate`/字数上下限（历史事故：99617 字的稿子
   `platform_check.pass=true` 留在工作台）；
2. `studio/pipeline.py` 只写 `platform_check` 与 `market_lang` 两个**并列**字段，
   谁也没合成一个结论，看板/复盘只能各读一半；
3. `tools/manuscript_audit.py` 自己又算了一套 A/B/C 等级。

三处口径不同 → 看板说"通过"、体检工具说"C 不可投"、主编给 7.8 分，用户没法判断。
本模块把"判定"收敛成两个函数，三处都调用它。
"""

from __future__ import annotations

# 短篇网文市场线：主编评分 7.0 是"合格可发"，8.0 是"强推"（见 prompts/review.md）
REVIEW_PASS = 7.0
GRADE_A, GRADE_B, GRADE_C = "A 可投", "B 可投（有瑕疵）", "C 不可投"


def grade(fails: list[str], warns: list[str]) -> str:
    """体检项 → 等级。fails 为空且无 warn 才是 A。"""
    if fails:
        return GRADE_C
    return GRADE_B if warns else GRADE_A


def submission(platform_check: dict | None = None,
               market_lang: dict | None = None,
               review_score: float | None = None,
               words: int | None = None,
               audit_fails: list[str] | None = None,
               soft_issues: list[str] | None = None,
               recheck_pass: bool | None = None,
               title_score: int | None = None) -> dict:
    """合成投稿结论。

    返回 `{ready, grade, reasons, checks}`：
      · `ready` 是**能不能投**（全部硬线通过）；
      · `grade` 是三档等级（软瑕疵降 B，不算不可投）；
      · `reasons` 逐条人话原因，看板/CLI 直接展示。

    判定优先级（先硬后软）：
      字数（platform_check）→ 语言（market_lang）→ 体检（audit_fails，可选）
      → 主编分与 soft_issues（AI 味口癖、禁用道具残留等措辞级瑕疵）。
    """
    pc = platform_check or {}
    ml = market_lang or {}
    reasons: list[str] = []
    soft: list[str] = []

    # 1) 字数：平台硬线，不通过就是不可投（读者视角：太短没内容、太长平台不收）
    if pc.get("pass") is False:
        lo, hi = pc.get("min_words"), pc.get("max_words")
        got = words if words is not None else pc.get("submit_words")
        reasons.append(f"字数 {got} 不在 {lo}—{hi}（平台硬线）")

    # 2) 语言：四类语言病密度（碎句/碎句链/语序/欧化/冗余/详略）
    if ml.get("pass") is False:
        if recheck_pass is True:
            # 判据修好之后老报告的 market_lang 会永远停在旧结论（e2e7 因"台词被
            # 句号切断误判碎句"判成 False）。有复检结论且达标时按复检走，并注明
            # 原始判定未改——既不篡改历史，也不让旧结论否掉已经修好的稿子。
            soft.append("原始语言判定未过，按当前判据复检达标")
        else:
            fails = ml.get("fails") or []
            reasons.append("语言未达投稿线：" + ("；".join(fails) if fails else "（未列出明细）"))

    # 3) 体检（tools/manuscript_audit.py 的 A/B/C，若调用方已算）
    for f in (audit_fails or []):
        reasons.append(f"体检未过：{f}")

    # 4) 措辞级瑕疵：AI 味口癖、禁用道具残留——改得动，但不该拦投稿
    for s in (soft_issues or []):
        soft.append(str(s))

    # 5) 主编评分：软线，只降等级不判死
    if review_score is not None and float(review_score) < REVIEW_PASS:
        soft.append(f"主编评分 {review_score} < {REVIEW_PASS}")

    # 6) 书名卖点分：软线。签约被拒的第一大显性原因是书名没卖点，不是正文差，
    #    所以它必须出现在投稿结论里，否则复盘会一直盯着字数调。
    if title_score is not None and int(title_score) < 5:
        soft.append(f"书名卖点分 {title_score}/10 偏低（签约第一道门槛）")

    ready = not reasons
    if not ready:
        g = GRADE_C
    elif soft:
        g = GRADE_B
    else:
        g = GRADE_A

    return {
        "ready": ready,
        "grade": g,
        "reasons": reasons + soft,
        "checks": {
            "words": pc.get("pass"),
            "lang": ml.get("pass"),
            "review": (None if review_score is None
                       else float(review_score) >= REVIEW_PASS),
            "title": (None if title_score is None else int(title_score) >= 5),
        },
    }


def brief(sub: dict) -> str:
    """一行摘要，供 CLI 打印与 runlog 记录。"""
    if not sub:
        return "投稿判定：—"
    if sub.get("ready"):
        tail = "；".join(sub.get("reasons") or [])
        return f"投稿判定：{sub['grade']}" + (f"（{tail}）" if tail else "（硬线全过）")
    return f"投稿判定：{sub['grade']}　" + "；".join(sub.get("reasons") or ["（未列出原因）"])
