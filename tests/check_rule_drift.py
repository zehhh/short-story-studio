# -*- coding: utf-8 -*-
"""规则单一权威源守卫：检测"同一规则被多处重复硬编码成不同数值/词表"。

运行：
    python tests/check_rule_drift.py

原则（见 docs/audit/2026-10-25_skill提炼.md 第 4 节）：
    L1 平台/结构参数 → data/platforms.json + config.yaml
    L2 语言规则       → data/lang_rules.json（词表/正则/阈值/severity 的唯一 owner）
    L3 判据/举证      → prompts/review.md（只写判据，不写词表数字）
    L4 写作技法       → prompts/draft.md + data/style_guide.md（不写禁用词数量线）
所以：prompt / style_guide 里出现的"≤N / ≥N / 全篇 N 次"都是**副本**，与 L2 不一致即为漂移。

本脚本策略：**先记录基线 + 报警，不硬红**。
    - 已确认的 K1–K9 漂移写在 KNOWN_DRIFTS 常量表（数量 + 位置 + 正确口径）。
    - 漂移数量**增加** → FAIL（防回潮：有人再写回旧数字立刻变红）。
    - 持平或减少 → WARN（提示修完记得下调基线）。
    - 只报告不改文件；基线不进 data/（本脚本无写权限）。

退出码：0 = 无失败项（可有 WARN）；1 = 漂移数量超过基线。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
DATA = ROOT / "data"

DRAFT = PROMPTS / "draft.md"
REVIEW = PROMPTS / "review.md"
STYLE = DATA / "style_guide.md"

# 当前已确认的漂移基线（2026-10-25 首轮，对应审计文档 4.1 的 K1–K9）。
# 字段：n=漂移实例数（防回潮比较口径）；where=位置与数值，供人读；owner=唯一权威源。
KNOWN_DRIFTS: dict[str, dict] = {
    "K1": {"n": 3, "where": "review.md:11 全篇≥4 / review.md:16 全篇≥4、全篇≥6", "owner": "lang_rules.freq.yixia=3（按 2000 字/章折算）"},
    "K2": {"n": 1, "where": "review.md:10 同一数字全篇≥3", "owner": "draft.md:81 同一数字全节≤1（与 langqa 节级口径一致）"},
    "K3": {"n": 3, "where": "review.md:8 当…的时候 / review.md:23 文艺腔组 / draft.md:64 文艺腔组", "owner": "lang_rules.blacklist.severity"},
    "K4": {"n": 2, "where": "config.kb.negative_terms 15 词 vs lang_rules 账本系 12 词；review.md:15 账本 mid→high", "owner": "config.kb.negative_terms（禁用道具唯一词表）"},
    "K5": {"n": 8, "where": "字数：platforms 4 平台上限 + uc 下限；style_guide:568 8000–20000；draft:143 / review:27「八成」", "owner": "config.review.word_floor/word_ceiling"},
    "K6": {"n": 1, "where": "卡点密度：platforms 500/600 字 vs review.md:6、draft.md:20 硬写每500字", "owner": "data/platforms.json.hook_density_chars"},
    "K7": {"n": 0, "where": "对话占比 ≥50% 在 draft.md:4、review.md:25 重复两处（数值一致，属重复硬编码，待收敛到 platforms.json）", "owner": "data/platforms.json.style（平台参数）"},
    "K8": {"n": 1, "where": "情绪直写：review.md:8「X很情绪词」标为命中即打回，lang_rules 是 mid", "owner": "lang_rules.blacklist.severity"},
    "K9": {"n": 1, "where": "硬禁词四副本：lang_rules 50 条 / draft 硬禁段 27 条 / review 3.5x 11 条 / style_guide §八 7 条", "owner": "data/lang_rules.json"},
}

_ok = 0
_warn = 0
_fail = 0


def item(status: str, msg: str) -> None:
    global _ok, _warn, _fail
    if status == "ok":
        _ok += 1
        print(f"[ok] {msg}")
    elif status == "WARN":
        _warn += 1
        print(f"[WARN] {msg}")
    else:
        _fail += 1
        print(f"[FAIL] {msg}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def load_cfg() -> dict:
    """读 config.yaml：优先 pyyaml，缺失时用极简正则抠出本脚本需要的 3 个键。"""
    text = read_text(ROOT / "config.yaml")
    try:
        import yaml  # noqa: PLC0415

        return yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        cfg: dict = {}
        floor = re.search(r"^\s*word_floor:\s*(\d+)", text, re.M)
        ceil = re.search(r"^\s*word_ceiling:\s*(\d+)", text, re.M)
        cfg["review"] = {
            "word_floor": int(floor.group(1)) if floor else None,
            "word_ceiling": int(ceil.group(1)) if ceil else None,
        }
        terms = []
        m = re.search(r"^  negative_terms:\s*$", text, re.M)
        if m:
            for line in text[m.end():].splitlines():
                if line and not line.startswith("    "):
                    break
                hit = re.match(r"\s+-\s*(\S+)\s*$", line)
                if hit:
                    terms.append(hit.group(1))
        cfg["kb"] = {"negative_terms": terms}
        print("[WARN] pyyaml 不可用，config 已降级为正则解析（仅 word_floor/word_ceiling/negative_terms）")
        return cfg


# ---------------------------------------------------------------------------
# 数字声明抽取（≤N / ≥N / 不超过N / 最多N 等）
# ---------------------------------------------------------------------------
OP_RE = re.compile(r"(≤|≥|<|>|不超过|最多|至少|不低于)\s*(\d+)")
SCOPE_RE = re.compile(r"(全篇|全章|通篇|整篇|全节|每节|本节|每章)")
WHOLE_SCOPE = {"全篇", "全章", "通篇", "整篇"}


def claims_in(text: str, kw_re: re.Pattern, label: str, window: int = 40) -> list[dict]:
    """在一份文档里找"关键词所在行 + 数字力度声明"，返回声明列表。

    window 只收关键词**之后** window 字符内的数字声明：同一行常常并列多条规则
    （如 review.md:16 同时有「全篇≥4次」和「全篇≥6次」），不收窗口外的数字才不会被
    邻近规则的阈值污染。关键词之前的数字一律视为别的规则的。
    """
    out: list[dict] = []
    for ln, line in enumerate(text.splitlines(), 1):
        for kw in kw_re.finditer(line):
            for m in OP_RE.finditer(line, kw.end()):
                if m.start() - kw.end() > window:
                    break
                op = "≤" if m.group(1) in ("≤", "<", "不超过", "最多") else "≥"
                num = int(m.group(2))
                scopes = SCOPE_RE.findall(line[max(0, m.start() - 24):m.start()])
                out.append(
                    {
                        "file": label,
                        "line": ln,
                        "op": op,
                        "num": num,
                        "scope": scopes[-1] if scopes else "未标注",
                        "text": line.strip(),
                    }
                )
    # 同一行多个关键词命中会重复收集，按 (行, 数值, 口径) 去重
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for c in out:
        key = (c["file"], c["line"], c["op"], c["num"], c["scope"])
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def same_claim(a: dict, b: dict) -> bool:
    """数值一致且口径不冲突（"未标注"视为通配，不算冲突）。"""
    if a["num"] != b["num"]:
        return False
    if a["scope"] == b["scope"] or "未标注" in (a["scope"], b["scope"]):
        return True
    return False


def fmt(c: dict) -> str:
    return f"{c['file']}:{c['line']} {c['scope']}{c['op']}{c['num']}"


# ---------------------------------------------------------------------------
# 1. 权威源抽取
# ---------------------------------------------------------------------------
def load_rules() -> tuple[dict, dict, dict, list]:
    """返回 (原始规则, freq 阈值表, freq 中文标签表, blacklist)。"""
    raw = json.loads(read_text(DATA / "lang_rules.json"))
    freq = {k: v for k, v in raw.get("freq", {}).items() if not k.startswith("_") and isinstance(v, int)}
    labels = {
        k[len("_label_"):]: v for k, v in raw.get("freq", {}).items() if k.startswith("_label_")
    }
    return raw, freq, labels, raw.get("blacklist", [])


# ---------------------------------------------------------------------------
# 2. 数字类双标（K1 / K2 / K7）
# ---------------------------------------------------------------------------
YIXIA_KW = re.compile(r"了一下|了一眼")
TONGTIAN_KW = re.compile(r"同一数字")
DIALOG_KW = re.compile(r"对话占比")


def check_numeric(freq: dict) -> list[dict]:
    print("\n== 1. 数字类：prompt/style_guide 的阈值副本 vs lang_rules ==")
    records: list[dict] = []
    draft, review, style = (read_text(p) for p in (DRAFT, REVIEW, STYLE))

    # K1：「XX了一下」——权威 3（按 2000 字/章，即节级）
    auth = freq.get("yixia", 3)
    for c in claims_in(draft, YIXIA_KW, "draft.md", 40) + claims_in(review, YIXIA_KW, "review.md", 40) + claims_in(style, YIXIA_KW, "style_guide.md", 40):
        if c["num"] == auth and c["scope"] not in WHOLE_SCOPE:
            continue
        records.append(
            {
                "k": "K1",
                "file": c["file"],
                "line": c["line"],
                "detail": f"规则双标：「XX了一下」 lang_rules={auth}/节 vs {fmt(c)}（口径不同或数值不同）",
                "text": c["text"][:80],
            }
        )

    # K2：「同一数字」——权威口径 = draft 节级 ≤1（与 langqa 节级扫描一致）
    same = claims_in(draft, TONGTIAN_KW, "draft.md", 20) + claims_in(review, TONGTIAN_KW, "review.md", 20) + claims_in(style, TONGTIAN_KW, "style_guide.md", 20)
    if same:
        canon = same[0]
        for c in same[1:]:
            if not same_claim(c, canon):
                records.append(
                    {
                        "k": "K2",
                        "file": c["file"],
                        "line": c["line"],
                        "detail": f"规则双标：「同一数字」 {fmt(canon)} vs {fmt(c)}",
                        "text": c["text"][:80],
                    }
                )

    # K7：「对话占比」——数值全一致，但属重复硬编码（single-owner 违规），记 0 实例待收敛
    dialog = claims_in(draft, DIALOG_KW, "draft.md", 20) + claims_in(review, DIALOG_KW, "review.md", 20)
    variants = {(c["num"], c["scope"]) for c in dialog}
    if len(variants) > 1:
        canon = dialog[0]
        for c in dialog[1:]:
            if not same_claim(c, canon):
                records.append(
                    {
                        "k": "K7",
                        "file": c["file"],
                        "line": c["line"],
                        "detail": f"规则双标：「对话占比」 {fmt(canon)} vs {fmt(c)}",
                        "text": c["text"][:80],
                    }
                )
    if len(dialog) > 1:
        print(f"        （K7 记录：对话占比在 {'/'.join(fmt(c) for c in dialog)} 重复 {len(dialog)} 处，数值一致但非单一 owner）")

    for r in records:
        item("WARN", f"{r['detail']}  〔{r['file']}:{r['line']}〕")
    if not records:
        item("ok", "数字类阈值未见双标")
    return records


# ---------------------------------------------------------------------------
# 3. severity 分层（K3 / K8 / K4 的 severity 部分）
# ---------------------------------------------------------------------------
HIGH_MARK = re.compile(r"命中即打回|出现即打回|即打回|硬禁")
SEVERITY_KW_OVERRIDE = {"很情绪词": "情绪"}  # label 匹配不到时的语义兜底短语


def severity_group(label: str) -> str:
    if "情绪" in label:
        return "K8"
    if "账本" in label or "道具" in label:
        return "K4"
    return "K3"


def check_severity(blacklist: list) -> list[dict]:
    print("\n== 2. severity：同一条规则在 prompt 里被升级为「命中即打回」 ==")
    docs = [("draft.md", read_text(DRAFT)), ("review.md", read_text(REVIEW))]
    records: dict[tuple, dict] = {}
    for entry in blacklist:
        sev = entry.get("severity")
        label = entry.get("label", "")
        if sev == "high":
            continue
        pat = entry.get("pattern", "")
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        for name, text in docs:
            for ln, line in enumerate(text.splitlines(), 1):
                if not HIGH_MARK.search(line):
                    continue
                hit = bool(rx.search(line)) or any(phrase in line for phrase in SEVERITY_KW_OVERRIDE if phrase in label and phrase in line)
                if not hit:
                    continue
                key = (severity_group(label), name, ln)
                rec = records.setdefault(
                    key,
                    {"k": key[0], "file": name, "line": ln, "labels": set(), "sev": sev},
                )
                rec["labels"].add(label)
    out: list[dict] = []
    for (group, name, ln), rec in records.items():
        names = "、".join(sorted(rec["labels"]))
        detail = f"severity 双标：{names} 在 lang_rules 是 {rec['sev']}，此处「命中即打回」（等价 high）"
        item("WARN", f"{detail}  〔{name}:{ln}〕")
        out.append({"k": group, "file": name, "line": ln, "detail": detail, "text": ""})
    if not out:
        item("ok", "severity 分层与 prompt 一致")
    return out


# ---------------------------------------------------------------------------
# 4. 词表类（K4 道具词表 / K9 硬禁词副本）
# ---------------------------------------------------------------------------
def check_wordlists(cfg: dict, blacklist: list) -> list[dict]:
    print("\n== 3. 词表类：禁用道具 / 硬禁词的重复副本 ==")
    records: list[dict] = []
    config_terms = set((cfg.get("kb") or {}).get("negative_terms") or [])
    # lang_rules 里的"账本系"词条：pattern 带 | 且含账/欠条类字样
    lang_terms: set[str] = set()
    for entry in blacklist:
        pat = entry.get("pattern", "")
        if any(w in pat for w in ("账", "欠条", "借条", "存折", "小本本")):
            lang_terms |= {w.strip() for w in pat.split("|") if w.strip()}
    only_cfg = sorted(config_terms - lang_terms)
    only_lang = sorted(lang_terms - config_terms)
    if only_cfg or only_lang:
        detail = (
            f"禁用道具词表不同步：config.kb.negative_terms={len(config_terms)} 词 / "
            f"lang_rules 账本系={len(lang_terms)} 词；仅 config 有 {only_cfg}；仅 lang_rules 有 {only_lang}"
        )
        item("WARN", f"{detail}  〔config.yaml / data/lang_rules.json〕")
        print("        建议统一：以 config.kb.negative_terms 为唯一词表，lang_rules 那条只留「引用 config」说明（审计 4.2 动作 2）")
        records.append({"k": "K4", "file": "config.yaml", "line": 0, "detail": detail, "text": ""})
    else:
        item("ok", "禁用道具词表已一致")

    # K9：硬禁词清单的副本数量（四份副本，任意一份更新都会漂移）
    draft = read_text(DRAFT)
    review = read_text(REVIEW)
    style = read_text(STYLE)
    n_lang = len(blacklist)
    seg = re.search(r"一、硬禁词.*?(?=二、)", draft, re.S)
    n_draft = len(re.findall(r"^\s*\d+\.", seg.group(0), re.M)) if seg else 0
    n_review = len(re.findall(r"^\s*3\.5\d?(?=\s|、|（)", review, re.M)) + len(re.findall(r"^\s*3\.6(?=\s)", review, re.M))
    n_style = len(re.findall(r"^禁用「", style, re.M))
    detail = (
        f"硬禁词清单存在多份副本：lang_rules {n_lang} 条 / draft 硬禁段 {n_draft} 条 / "
        f"review 3.5x–3.6 {n_review} 条 / style_guide §八 {n_style} 条（任一更新必然漂移）"
    )
    item("WARN", f"{detail}  〔data/lang_rules.json / prompts/draft.md / prompts/review.md / data/style_guide.md〕")
    records.append({"k": "K9", "file": "data/lang_rules.json", "line": 0, "detail": detail, "text": ""})
    return records


# ---------------------------------------------------------------------------
# 5. 字数类（K5）与卡点密度（K6）
# ---------------------------------------------------------------------------
def check_wordcount(cfg: dict) -> list[dict]:
    print("\n== 4. 字数区间：config vs platforms vs style_guide vs「八成」 ==")
    records: list[dict] = []
    rcfg = cfg.get("review") or {}
    floor, ceil = rcfg.get("word_floor"), rcfg.get("word_ceiling")
    if not floor or not ceil:
        item("WARN", f"config.review 缺 word_floor/word_ceiling（{floor}/{ceil}），跳过字数一致性检查")
        return records

    # platforms.json：投稿参考口径（每平台一行）
    try:
        platforms = json.loads(read_text(DATA / "platforms.json")).get("platforms", {})
    except Exception as exc:  # noqa: BLE001
        platforms = {}
        item("WARN", f"platforms.json 解析失败：{exc}")
    for pid, p in platforms.items():
        ideal = p.get("ideal_words") or [None, None]
        p_min, p_ceil = p.get("min_words"), ideal[1] if len(ideal) > 1 else None
        if p_min is not None and p_min < floor:
            detail = f"字数下限不一致：{pid}.min_words={p_min} < config.word_floor={floor}（签约线会被门禁判过宽）"
            item("WARN", f"{detail}  〔data/platforms.json:{pid}〕")
            records.append({"k": "K5", "file": "platforms.json", "line": 0, "detail": detail, "text": ""})
        if p_ceil is not None and p_ceil != ceil:
            detail = f"字数上限不一致：{pid}.ideal_words[1]={p_ceil} != config.word_ceiling={ceil}（投稿参考 vs 门禁口径）"
            item("WARN", f"{detail}  〔data/platforms.json:{pid}〕")
            records.append({"k": "K5", "file": "platforms.json", "line": 0, "detail": detail, "text": ""})

    # style_guide：正文里的字数表述
    style = read_text(STYLE)
    m = re.search(r"(\d{4,5})\s*字到\s*(\d{4,5})\s*字", style)
    if m:
        s_floor, s_ceil = int(m.group(1)), int(m.group(2))
        if (s_floor, s_ceil) != (floor, ceil):
            line_no = style[: m.start()].count("\n") + 1
            detail = f"字数区间不一致：style_guide={s_floor}–{s_ceil} vs config={floor}–{ceil}"
            item("WARN", f"{detail}  〔data/style_guide.md:{line_no}〕")
            records.append({"k": "K5", "file": "style_guide.md", "line": line_no, "detail": detail, "text": ""})

    # 「八成」：把硬下限再打八折，实际下限远低于 word_floor
    for name, path in (("draft.md", DRAFT), ("review.md", REVIEW)):
        for ln, line in enumerate(read_text(path).splitlines(), 1):
            if "八成" not in line:
                continue
            detail = f"字数下限被「八成」打折：{name}:{ln} 写成 {floor}×0.8={int(floor * 0.8)}，低于 word_floor={floor}"
            item("WARN", f"{detail}  〔{name}:{ln}〕")
            records.append({"k": "K5", "file": name, "line": ln, "detail": detail, "text": line.strip()[:80]})
    if not records:
        item("ok", "字数口径一致")
    return records


def check_hook_density() -> list[dict]:
    print("\n== 5. 卡点密度：platforms 参数 vs prompt 硬指标 ==")
    records: list[dict] = []
    try:
        platforms = json.loads(read_text(DATA / "platforms.json")).get("platforms", {})
    except Exception:  # noqa: BLE001
        platforms = {}
    values = {pid: p.get("hook_density_chars") for pid, p in platforms.items() if p.get("hook_density_chars")}
    # prompt 侧是"每 N 字一个卡点"的自然语言，不经 OP_RE（没有 ≤/≥），单独抽
    hard: list[tuple[str, int, int]] = []
    hook_kw = re.compile(r"卡点")
    for name, path in (("review.md", REVIEW), ("draft.md", DRAFT)):
        for ln, line in enumerate(read_text(path).splitlines(), 1):
            if not hook_kw.search(line):
                continue
            for m in re.finditer(r"每\s*(\d+)\s*字", line):
                hard.append((name, ln, int(m.group(1))))
    prompt_vals = {n for _, _, n in hard}
    conflict = bool(hard) and (len(set(values.values())) > 1 or prompt_vals - set(values.values()))
    if conflict:
        detail = (
            f"卡点密度口径不一致：platforms={values}（按平台） vs prompt 硬写 "
            + "、".join(f"{name}:{ln}={n}" for name, ln, n in hard)
            + "（应按所选平台注入，不该在 prompt 里写死；通用判据只描述「卡点密度」维度）"
        )
        item("WARN", f"{detail}  〔data/platforms.json / prompts/review.md / prompts/draft.md〕")
        records.append({"k": "K6", "file": "platforms.json", "line": 0, "detail": detail, "text": ""})
    elif hard:
        item("WARN", f"卡点密度在 prompt 里写死 {hard}（platforms={values}）——建议改引用 {{PLATFORM_SPEC}}")
        records.append({"k": "K6", "file": "prompts", "line": 0, "detail": f"卡点密度写死 {hard}", "text": ""})
    else:
        item("ok", "卡点密度未见写死 / 与 platforms 一致")
    return records


# ---------------------------------------------------------------------------
# 6. 基线对账（防回潮）
# ---------------------------------------------------------------------------
def check_baseline(records: list[dict]) -> None:
    print("\n== 6. 基线对账（数量增加 = 回潮，FAIL） ==")
    counts: dict[str, int] = {k: 0 for k in KNOWN_DRIFTS}
    for r in records:
        counts[r["k"]] = counts.get(r["k"], 0) + 1
    total_cur = sum(counts.values())
    total_base = sum(v["n"] for v in KNOWN_DRIFTS.values())

    worse = []
    for k in sorted(set(KNOWN_DRIFTS) | set(counts)):
        cur = counts.get(k, 0)
        base = KNOWN_DRIFTS.get(k, {}).get("n", 0)
        owner = KNOWN_DRIFTS.get(k, {}).get("owner", "（未登记，需补 owner）")
        if cur > base:
            worse.append(f"{k} 基线 {base} → 当前 {cur}（+{cur - base}）")
        elif cur < base:
            print(f"        [基线可下调] {k}: {base} → {cur}（修好了就同步改 KNOWN_DRIFTS，防止下轮回潮）")
        state = "（无漂移）" if base == 0 and cur == 0 else ""
        print(f"        {k}: 当前 {cur} / 基线 {base}{state} | owner 应为「{owner}」")
    if worse:
        item("FAIL", "规则漂移超过基线（有人把旧数字写回来了）：" + "；".join(worse))
    else:
        item("ok", f"漂移未超过基线（当前 {total_cur} / 基线 {total_base}），防回潮通过")

    print(f"\n当前漂移总数 {total_cur} / 基线数 {total_base}")
    print("修复指引：docs/audit/2026-10-25_skill提炼.md 第 4 节（4.1 冲突清单 / 4.2 合并策略），改完同步下调 KNOWN_DRIFTS。")


def main() -> int:
    print("短篇小说工作室 · 规则单一权威源守卫（零 token，只报告不改文件）")
    raw, freq, labels, blacklist = load_rules()
    item(
        "ok",
        f"权威源 data/lang_rules.json 解析成功：blacklist {len(blacklist)} 条 / freq {len(freq)} 项"
        f"（含 {len(labels)} 条中文标签）/ pitfalls {len(raw.get('pitfalls', []))} 条",
    )
    cfg = load_cfg()

    records: list[dict] = []
    records += check_numeric(freq)
    records += check_severity(blacklist)
    records += check_wordlists(cfg, blacklist)
    records += check_wordcount(cfg)
    records += check_hook_density()
    check_baseline(records)

    print(f"\n{_ok} 项通过 / {_fail} 项失败（另有 {_warn} 项警告）")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
