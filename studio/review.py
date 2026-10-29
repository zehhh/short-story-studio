# -*- coding: utf-8 -*-
"""证据化评审：无证据不得分 + 覆盖率与质量分分离 + 未校准不得当门禁。

为什么需要（借鉴 openwrite 的评审 rubric，见 `docs/audit/2026-10-25_openwrite借鉴.md` B07）：
    旧实现只有「主编给一个 0-10 分 + 一串 issues」。这有三个结构性漏洞：

    1. **无证据也给分**。模型可以只回 `{"verdict":"pass","score":9}` 就拿到高分，
       报告里看不出它到底核对了哪几项。分数是"模型心情"，不是"稿件事实"。
    2. **"没评"被当成"好评"**。模型漏评 7 项、只评 3 项，最终分照样很高；读者无法
       区分「85 分且查了 100%」和「85 分但只查了 30%」。
    3. **换了模型阈值就失效**。阈值 7.5 是对着某个模型标定的，换模型后分布漂移，
       但代码仍把它当交付门禁——于是"上线标准"悄悄变了却没人知道。

    本模块把评审改成**评分卡（rubric）**：
      - 每个 `criterion` 必须带 `status` 与 `evidence[]`；`earned > 0` 而 `evidence`
        为空 ⇒ 强制 `inconclusive` 且 `earned = 0`（硬规则，无证据不得分）。
      - `evidence` 里的引文必须在正文里**真的出现**（归一化子串匹配），否则视为没证据。
        这条是本项目的加码：模型编造"原文"是最常见的作弊方式，光要求"给证据"挡不住。
      - `quality_score`（得分率）与 `coverage`（覆盖率）**分开报**：`coverage` 只统计
        真正被评估过的分值权重，模型少评几项只会掉覆盖率，不会伪装成"质量很好"。
      - `gate_status`（门禁）与 `quality_score`（分数）分离：`critical/blocker` 类问题
        （安全红线、禁用道具、思考泄漏）**只改门禁不改分**，避免"平均分还行就放行"。
      - 未校准（`calibration_status != "calibrated"`）时 `production_gate` 不得开启，
        开启即 `ValueError`；未校准时门禁结论降级为 `advisory`，只提示不阻断。

    确定性项（字数、安全红线）由本模块直接算，证据是机器可核验的事实（字数、命中位置），
    不依赖 LLM 自述——这样覆盖率的下限永远不为 0，评审卡不会因为模型偷懒而全盘失真。

用法：
    from . import review as review_mod
    rubric = review_mod.build(cfg, verdict, chapter_text, target_words=1500)
    print(review_mod.render_text(rubric))

输出结构（`reviews/chNN.json` 的 `rubric` 字段）：
    {schema_version, rubric_version, calibration_status, production_gate_enabled,
     criteria: [{id,name,domain,status,earned,max,evidence[],rationale,issues[]}],
     domains:  [{id,name,earned,max,coverage,score}],
     quality_score, coverage, gate_status, delivery_status, execution_status, notes[]}
"""
from __future__ import annotations

import re

from .langqa import banned_regex, scan as lang_scan, scan_banned, think_leak

SCHEMA_VERSION = 1
RUBRIC_VERSION = "2026-10-26"

# 评分卡定义：(id, 名称, 域, 满分)。满分合计必须为 100（静态检查会核对）。
# 安全项满分 0：它是 blocker，只改门禁不改分（openwrite :403-407 的做法）。
CRITERIA: list[tuple[str, str, str, int]] = [
    ("C01", "开头钩子", "plot", 12),
    ("C02", "卡点密度", "pacing", 12),
    ("C03", "章末钩子", "plot", 12),
    ("C04", "情节拍完整", "plot", 12),
    ("C05", "文风", "prose", 12),
    ("C06", "连续性", "coherence", 12),
    ("C07", "人物一致", "character", 10),
    ("C08", "字数", "canon", 6),
    ("C09", "题材符合度", "canon", 12),
    ("C10", "安全", "gate", 0),
]

GATE_ONLY = {"C10"}

DOMAIN_NAMES = {
    "plot": "情节",
    "pacing": "节奏",
    "prose": "文笔",
    "coherence": "连贯",
    "character": "人物",
    "canon": "设定契约",
    "gate": "红线门禁",
}

# 主编 prompt 里的 criterion 名 → 评分卡 id。模型常写简称（"开头"而不是"开头钩子"），
# 这里做同义归一，避免因为措辞差异把一条真实评估算成"未评估"。
_NAME_ALIASES = {
    "开头": "C01", "开头钩子": "C01", "开场": "C01",
    "卡点密度": "C02", "卡点": "C02", "节奏": "C02",
    "章末钩子": "C03", "钩子": "C03", "结尾钩子": "C03",
    "情节拍": "C04", "情节拍完整": "C04", "情节": "C04",
    "文风": "C05", "语言": "C05", "文笔": "C05",
    "连续性": "C06", "连贯": "C06", "逻辑": "C06",
    "人物一致": "C07", "人物": "C07", "人物一致性": "C07",
    "字数": "C08", "篇幅": "C08",
    "题材符合度": "C09", "题材": "C09", "题材元素": "C09",
    "安全": "C10", "红线": "C10",
}

# issue 严重度 → 该 criterion 的扣分比例（只用于 legacy 输出反推，新契约直接用 earned）
_SEVERITY_KEEP = {"high": 0.0, "mid": 0.4, "low": 0.75}

_MIN_EVIDENCE_CHARS = 6
_PUNCT_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")


# ---------------------------------------------------------------------------
# 证据核验
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """去掉空白与标点，只留汉字/字母/数字，用于引文比对。

    模型引用原文时常改标点（引号、破折号、省略号），原样子串匹配会大量误判为
    "编造证据"。归一化后比对既保留"是否真的存在这段文字"的语义，又容忍标点差异。
    """
    return _PUNCT_RE.sub("", text or "").lower()


def verify_evidence(quote: str, text_norm: str, min_chars: int = _MIN_EVIDENCE_CHARS) -> bool:
    """引文是否真的出现在正文里。

    两级核验（严格 → 宽容）：
      1. 归一化后**子串命中**：最强证据，直接过；
      2. 子串不中时按 6 字滑窗（步长 3）算命中率，≥70% 也算过。

    为什么要第二级：实测 e2e6 第 05 章，评审模型给的引文跟正文只差几个字
    （把「她把钱塞进围裙口袋」写成「她把钱塞进围裙里」），严格子串判失败 →
    该项记 «inconclusive、得分归零» → 10 项里 9 项作废、覆盖率只剩 6%、
    整章变成「未评级」。而「编造证据」的句子不可能有 70% 的 6 字 n-gram 落在正文里，
    所以宽容核验并不放水，只是容掉标点/个别虚词/轻微改写的差异。
    """
    q = normalize(quote)
    if len(q) < min_chars:
        return False
    if q in text_norm:
        return True
    if len(q) < 10:
        return False
    step = 3
    grams = [q[i:i + 6] for i in range(0, max(1, len(q) - 5), step)]
    if not grams:
        return False
    hit = sum(1 for g in grams if g in text_norm)
    return hit / len(grams) >= 0.70


def text_sha(text: str) -> str:
    """正文指纹（归一化后取 md5 前 12 位）。

    用途：判定"评审卡对应的正文"与"当前落盘正文"是否是同一版。
    归一化后再哈希，可以容忍排版层的空白/标点差异，只对真正的改写敏感。
    """
    import hashlib

    return hashlib.md5(normalize(text).encode("utf-8")).hexdigest()[:12]


def _clean_evidence(raw, text_norm: str) -> list[str]:
    """规整 evidence 字段：接受 str / [str] / [{"quote":...}]，只保留可核验的引文。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for it in items:
        quote = ""
        if isinstance(it, str):
            quote = it
        elif isinstance(it, dict):
            quote = str(it.get("quote") or it.get("text") or it.get("evidence") or "")
        quote = quote.strip()
        if quote and verify_evidence(quote, text_norm):
            out.append(quote[:120])
    return out


# ---------------------------------------------------------------------------
# criterion 归一
# ---------------------------------------------------------------------------
def _spec_index() -> dict[str, tuple[str, str, str, int]]:
    return {c[0]: c for c in CRITERIA}


def _resolve_id(raw: dict) -> str:
    """从模型给的一条 criterion 里解析出评分卡 id（先认 id，再认名称同义词）。"""
    rid = str(raw.get("id") or "").strip().upper()
    if rid in _spec_index():
        return rid
    name = str(raw.get("name") or raw.get("criterion") or "").strip()
    if name in _NAME_ALIASES:
        return _NAME_ALIASES[name]
    for alias, cid in _NAME_ALIASES.items():
        if alias and alias in name:
            return cid
    return ""


def _coerce_status(raw: dict, earned: float, evidence: list[str], notes: list[str], name: str) -> str:
    """定 status 并执行硬规则：earned > 0 但无有效证据 ⇒ inconclusive 且归零。"""
    status = str(raw.get("status") or "evaluated").strip().lower()
    if status not in ("evaluated", "not_applicable", "inconclusive"):
        status = "evaluated"
    if status == "not_applicable":
        return "not_applicable"
    if status == "evaluated" and earned > 0 and not evidence:
        notes.append(f"{name}：声称得 {earned:g} 分但无有效证据 → 记 inconclusive、得分归零")
        return "inconclusive"
    if status == "evaluated" and not evidence:
        # 0 分不需要证据（"这里有问题"本身就是结论，issues 会说明），保持 evaluated
        return "evaluated"
    return status


def _from_llm_criteria(raw_list, text_norm: str, notes: list[str]) -> dict[str, dict]:
    """把模型输出的 criteria 数组转成 {id: {status, earned, evidence, rationale}}。"""
    out: dict[str, dict] = {}
    for raw in raw_list or []:
        if not isinstance(raw, dict):
            continue
        cid = _resolve_id(raw)
        if not cid or cid in out:
            continue
        spec = _spec_index()[cid]
        maxp = spec[3]
        try:
            earned = float(raw.get("earned") or 0)
        except (TypeError, ValueError):
            earned = 0.0
        earned = max(0.0, min(earned, float(maxp)))
        evidence = _clean_evidence(raw.get("evidence"), text_norm)
        status = _coerce_status(raw, earned, evidence, notes, spec[1])
        if status != "evaluated":
            earned = 0.0
        out[cid] = {
            "status": status,
            "earned": round(earned, 2),
            "evidence": evidence,
            "rationale": str(raw.get("rationale") or "")[:200],
        }
    return out


def _from_legacy_issues(issues, text_norm: str, notes: list[str]) -> dict[str, dict]:
    """旧契约（只有 verdict/score/issues）反推：只把「带可核验引文的问题」算作已评估。

    模型说"pass 且无 issues"时**不**推导成"全部满分"——那正是要消灭的"无证据给分"。
    它只会让覆盖率停在确定性项（字数/安全）的水平，报告里一眼可见"这轮没真审"。
    """
    out: dict[str, dict] = {}
    for it in issues or []:
        if not isinstance(it, dict):
            continue
        cid = _resolve_id(it)
        if not cid:
            continue
        spec = _spec_index()[cid]
        maxp = spec[3]
        sev = str(it.get("severity") or "mid").strip().lower()
        keep = _SEVERITY_KEEP.get(sev, 0.4)
        note = str(it.get("note") or "")
        evidence = _clean_evidence(note, text_norm)
        if not evidence:
            notes.append(f"{spec[1]}：legacy issue 未引用可核验原文，不计入覆盖率")
            continue
        prev = out.get(cid)
        earned = round(maxp * keep, 2)
        if prev is not None:                      # 同一 criterion 多条 issue 取最重
            earned = min(prev["earned"], earned)
        out[cid] = {
            "status": "evaluated",
            "earned": earned,
            "evidence": evidence,
            "rationale": note[:200],
        }
    return out


# ---------------------------------------------------------------------------
# 确定性项：字数 / 安全红线
# ---------------------------------------------------------------------------
def _eval_word_count(text: str, target: int) -> dict:
    """字数项由机器算，证据是"X 字 / 目标 Y 字"这种可复核的事实。"""
    from .util import count_words_zh

    n = count_words_zh(text)
    maxp = _spec_index()["C08"][3]
    if target <= 0:
        # 没有目标字数时不能默认"达标"。旧实现 target=0 会让 ratio=1.0 直接给满分，
        # 于是"目标未知"被算成"字数完美"，覆盖率还因此虚高。这里明确记未评估。
        return {
            "status": "inconclusive",
            "earned": 0.0,
            "evidence": [f"正文 {n} 字，但未记录目标字数"],
            "rationale": "缺少目标字数，无法判定达成率（不计入覆盖率）",
        }
    ratio = n / target
    if ratio >= 0.8:
        earned, status = float(maxp), "evaluated"
    else:
        earned, status = round(maxp * max(0.0, ratio / 0.8), 2), "evaluated"
    return {
        "status": status,
        "earned": earned,
        "evidence": [f"正文 {n} 字，目标 {target} 字"],
        "rationale": f"字数达成率 {ratio:.0%}（红线：不低于目标八成）",
    }


def _eval_safety(cfg: dict | None, text: str, direction: str = "") -> tuple[dict, bool]:
    """安全红线：确定性扫描（语言质检 high/mid + 禁用道具 + 思考泄漏）。

    返回 (criterion 结果, 是否 blocked)。证据直接取命中项的位置与实例，
    这类证据不依赖模型，永远可核验。
    """
    hits: list[str] = []
    if think_leak(text):
        hits.append("思考泄漏：正文疑似混入思维链/提纲")
    if cfg is not None and banned_regex(cfg) is not None:
        brep = scan_banned(text, cfg)
        for h in brep.get("hits") or []:
            hits.append(f"禁用道具「{h['term']}」×{h['count']}")
    try:
        from .langqa import genre_tags as _gt

        lq = lang_scan(text, genres=_gt(direction))
    except Exception:  # noqa: BLE001
        lq = {"hits": []}
    for h in lq.get("hits") or []:
        if h.get("severity") == "high":
            hits.append(f"{h.get('label')}：{h.get('note')}")
    blocked = bool(hits)
    return (
        {
            "status": "evaluated",
            "earned": 0.0,                        # gate-only，满分 0，不影响分数
            "evidence": hits[:8] or ["确定性扫描（语言质检 high 项 + 禁用道具 + 思考泄漏）0 命中"],
            "rationale": "安全红线命中，门禁阻断" if blocked else "安全红线无命中",
        },
        blocked,
    )


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def _aggregate(criteria: list[dict]) -> tuple[list[dict], float | None, float]:
    """按域汇总，返回 (domains, quality_score, coverage)。"""
    doms: dict[str, dict] = {}
    for c in criteria:
        d = doms.setdefault(c["domain"], {"id": c["domain"],
                                          "name": DOMAIN_NAMES.get(c["domain"], c["domain"]),
                                          "earned": 0.0, "max": 0, "evaluated_max": 0})
        d["max"] += c["max"]
        if c["id"] in GATE_ONLY:
            continue
        if c["status"] == "evaluated":
            d["earned"] += c["earned"]
            d["evaluated_max"] += c["max"]
    out = []
    for d in doms.values():
        em = d["evaluated_max"]
        out.append({
            "id": d["id"],
            "name": d["name"],
            "earned": round(d["earned"], 2),
            "max": d["max"],
            "coverage": round(em / d["max"], 3) if d["max"] else 0.0,
            "score": round(d["earned"] / em * 100, 1) if em else None,
        })
    potential = sum(c["max"] for c in criteria if c["id"] not in GATE_ONLY)
    evaluated = sum(c["max"] for c in criteria
                    if c["id"] not in GATE_ONLY and c["status"] == "evaluated")
    earned = sum(c["earned"] for c in criteria
                 if c["id"] not in GATE_ONLY and c["status"] == "evaluated")
    quality = round(earned / evaluated * 100, 1) if evaluated else None
    coverage = round(evaluated / potential, 3) if potential else 0.0
    return out, quality, coverage


def _calibration(cfg: dict | None, strict: bool) -> tuple[str, bool, str]:
    """读校准状态，返回 (calibration_status, gate_enabled, warning)。"""
    rcfg = ((cfg or {}).get("review") or {}) if isinstance(cfg, dict) else {}
    status = str(rcfg.get("calibration_status") or "uncalibrated").strip().lower()
    want_gate = bool(rcfg.get("production_gate", False))
    if want_gate and status != "calibrated":
        msg = (f"评审卡未校准（calibration_status={status!r}），不得作为生产门禁。"
               "请先用已定稿样本标定阈值，再把 calibration_status 改为 'calibrated'。")
        if strict:
            raise ValueError(msg)
        return status, False, msg
    return status, (want_gate and status == "calibrated"), ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build(
    cfg: dict | None,
    verdict: dict | None,
    text: str,
    target_words: int = 0,
    strict: bool = False,
    direction: str = "",
) -> dict:
    """把一次主编审查结果规范成证据化评分卡。

    verdict 兼容两种契约：
      新：{"verdict":..., "criteria":[{id,name,status,earned,max,evidence[],rationale}]}
      旧：{"verdict":..., "score":0-10, "issues":[{criterion,note,severity,...}]}
    旧契约不会因为"没报问题"就推导满分——没证据就是没评估（见 _from_legacy_issues）。
    """
    verdict = verdict if isinstance(verdict, dict) else {}
    text = text or ""
    text_norm = normalize(text)
    notes: list[str] = []

    calib, gate_enabled, warn = _calibration(cfg, strict)
    if warn:
        notes.append(warn)

    raw_criteria = verdict.get("criteria")
    if isinstance(raw_criteria, list) and raw_criteria:
        # 证据核对的基准文本 = 主编真正看过的那一版（verdict.reviewed_text）。
        # 终稿复查（deterministic_recheck）可能在主编审完之后重写正文，用终稿去核对
        # 旧证据会把"审过了"误判成"编造证据"——实测覆盖率会从 100% 掉到 6%，
        # 真实缺陷被噪音淹没。字数/安全两项仍然必须用终稿算（见下方覆盖），
        # 因为那才是要交付的东西。
        reviewed = verdict.get("reviewed_text")
        evid_norm = normalize(reviewed) if isinstance(reviewed, str) and reviewed.strip() else text_norm
        evaluated = _from_llm_criteria(raw_criteria, evid_norm, notes)
        source = "criteria"
        reviewed_sha = str(verdict.get("reviewed_text_sha") or "")
        if reviewed_sha and reviewed_sha != text_sha(text):
            source = "criteria_stale"
            notes.append(
                "评审卡基于旧版正文：主编审完后正文被门禁复查重写，"
                "质量分与证据对旧版成立；字数与安全项已按终稿重算")
    else:
        evaluated = _from_legacy_issues(verdict.get("issues"), text_norm, notes)
        source = "legacy_issues"

    # 确定性项覆盖模型自述：字数与安全红线不接受模型"我觉得没问题"。
    evaluated["C08"] = _eval_word_count(text, int(target_words or 0))
    safety, blocked = _eval_safety(cfg, text, direction)

    rows: list[dict] = []
    for cid, name, domain, maxp in CRITERIA:
        # 安全项永远用确定性扫描结果，不接受模型自述
        got = safety if cid == "C10" else evaluated.get(cid)
        if got is None:
            rows.append({"id": cid, "name": name, "domain": domain, "status": "inconclusive",
                         "earned": 0.0, "max": maxp, "evidence": [],
                         "rationale": "本轮未评估（不计入覆盖率）", "issues": []})
            continue
        rows.append({"id": cid, "name": name, "domain": domain,
                     "status": got["status"], "earned": got["earned"], "max": maxp,
                     "evidence": got["evidence"], "rationale": got["rationale"], "issues": []})

    domains, quality, coverage = _aggregate(rows)

    # 执行状态：模型输出不可解析 / 完全没给 criteria 且没有可核验 issue
    execution = "ok"
    if str(verdict.get("verdict")).lower() in ("degraded", "failed"):
        execution = "failed"
    elif source == "legacy_issues" and not evaluated.get("C08") and coverage <= 0.0:
        execution = "degraded"

    rcfg = ((cfg or {}).get("review") or {}) if isinstance(cfg, dict) else {}
    score_floor = float(rcfg.get("rubric_score_threshold", 70))
    min_cov = float(rcfg.get("rubric_min_coverage", 0.80))

    if execution == "failed":
        delivery = "inconclusive"
    elif blocked:
        delivery = "blocked"
    elif coverage < min_cov:
        delivery = "inconclusive"
    elif quality is None or quality < score_floor:
        delivery = "revise"
    else:
        delivery = "pass"

    if coverage < min_cov and execution != "failed":
        notes.append(f"覆盖率 {coverage:.0%} < 门槛 {min_cov:.0%}：本轮只评估了部分检查项，"
                     f"结论记 inconclusive（分数仅代表已评估部分）")

    # 覆盖率不足时不得给出"看起来很好的总分"。
    # 实测踩坑：MockLLM 只答了 C08（覆盖率 6%），earned/evaluated 仍算出 95.8 分，
    # CLI 首行就是「质量 95.8 / 覆盖 6%」——读者只会看见 95.8。这正是本模块要
    # 消灭的"假高分"。所以覆盖率不达标时 quality_score 置 None，原始比值挪到
    # quality_partial 并在 note 里说明，让"没评"和"评得好"在数据上不可混淆。
    quality_partial = quality
    if coverage < min_cov:
        quality = None
        if quality_partial is not None:
            notes.append(f"已评估部分折算 {quality_partial} 分，因覆盖率不足不作为质量分")

    if blocked:
        gate = "blocked"
    elif not gate_enabled:
        gate = "advisory"                      # 未校准：只提示，不阻断生产
    elif delivery == "pass":
        gate = "pass"
    elif delivery == "revise":
        gate = "fail"
    else:
        gate = "inconclusive"

    return {
        "schema_version": SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "calibration_status": calib,
        "production_gate_enabled": gate_enabled,
        "criteria": rows,
        "domains": domains,
        "quality_score": quality,
        "quality_partial": quality_partial,
        "coverage": coverage,
        "gate_status": gate,
        "delivery_status": delivery,
        "execution_status": execution,
        "source": source,
        "notes": notes,
    }


def score_from_criteria(verdict: dict | None) -> float | None:
    """用模型自己的评分卡折算 0-10 分；评分卡不可用时返回 None。

    判据：status="not_applicable" 不进分母；"inconclusive" 进分母但算 0 分
    （和 _aggregate 同口径，避免两处算出不同数字）。
    至少 6 项 evaluated 才认——少于 6 项时得分率噪声太大。
    """
    v = verdict or {}
    rows = v.get("criteria") or []
    if not isinstance(rows, list):
        return None
    earn = mx = 0.0
    n_ok = 0
    for c in rows:
        if not isinstance(c, dict):
            continue
        st = str(c.get("status") or "evaluated")
        if st == "not_applicable":
            continue
        try:
            m = float(c.get("max") or 0)
            e = float(c.get("earned") or 0)
        except (TypeError, ValueError):
            continue
        if m <= 0:
            continue
        mx += m
        earn += max(0.0, min(e, m))
        if st == "evaluated":
            n_ok += 1
    if n_ok < 6 or mx <= 0:
        return None
    return round(earn / mx * 10, 1)


def has_high_issue(verdict: dict | None) -> bool:
    for it in ((verdict or {}).get("issues") or []):
        if isinstance(it, dict) and str(it.get("severity")).lower() == "high":
            return True
    return False


def summary_fields(rubric: dict | None) -> dict:
    """给 project.json.meta.reviews[no] 的精简字段（看板列表/汇总用）。"""
    r = rubric or {}
    return {
        "quality": r.get("quality_score"),
        "coverage": r.get("coverage"),
        "delivery": r.get("delivery_status"),
        "gate_status": r.get("gate_status"),
    }


def render_text(rubric: dict | None) -> str:
    """人读版：一行结论 + 逐项明细（runlog / CLI 输出用）。"""
    r = rubric or {}
    if not r.get("criteria"):
        return "（无评审卡）"
    q = r.get("quality_score")
    cov = float(r.get("coverage") or 0)
    if q is None:
        part = r.get("quality_partial")
        qtxt = f"未评级（覆盖率 {cov:.0%} 不足"
        qtxt += f"，已评估部分 {part}" if part is not None else ""
        qtxt += "）"
    else:
        qtxt = str(q)
    head = (f"评审卡：质量 {qtxt} / 覆盖 {cov:.0%} · "
            f"门禁 {r.get('gate_status')} · 交付 {r.get('delivery_status')}")
    if r.get("calibration_status") != "calibrated":
        head += f"（未校准：{r.get('calibration_status')}，门禁结论仅供参考）"
    lines = [head]
    for c in r["criteria"]:
        ev = c["evidence"][0] if c.get("evidence") else "—"
        lines.append(f"  {c['id']} {c['name']:<6} {c['status']:<13} "
                     f"{c['earned']:>5g}/{c['max']:<3g} {ev[:60]}")
    return "\n".join(lines)


def gate_failed(rubric: dict | None) -> bool:
    """门禁是否应判定为不通过（供流水线串联）。未校准的 advisory 一律不算不通过。"""
    r = rubric or {}
    return r.get("gate_status") in ("blocked", "fail")


def rollup(cfg: dict | None, project) -> dict:
    """全稿评审卡汇总（写入 report.json.rubric_rollup）。

    为什么需要：逐章 rubric 落在 reviews/chNN.json，而 report.json 才是看板详情、
    复盘、投稿包共同读取的"项目级账本"。缺了汇总，评审卡就只有跑 CLI 才看得见，
    等于给开发者看的调试信息，而不是交付证据。

    只读落盘数据，不调模型——因此任何历史项目（哪怕用旧版本跑的）都能补算。
    """
    from .util import count_words_zh

    rows: list[dict] = []
    for ch in project.read_chapters():
        rev = project.load_json(f"ch{ch['no']:02d}.json", subdir="reviews") or {}
        rub = rev.get("rubric")
        if not rub:
            # 没有评审卡（历史项目 / hybrid 模式 / --no-review）也要出现在汇总里，
            # 否则"未评审"会被误读成"全部合格"。
            rows.append({"no": ch["no"], "words": count_words_zh(ch["text"]),
                         "quality": None, "coverage": None, "delivery": "unreviewed",
                         "gate": rev.get("gate") or "unreviewed"})
            continue
        rows.append({"no": ch["no"], "words": count_words_zh(ch["text"]),
                     "quality": rub.get("quality_score"),
                     "coverage": rub.get("coverage"),
                     "delivery": rub.get("delivery_status"),
                     "gate": rub.get("gate_status")})

    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["delivery"])] = counts.get(str(r["delivery"]), 0) + 1

    covs = [r["coverage"] for r in rows if r["coverage"] is not None]
    qs = [r["quality"] for r in rows if r["quality"] is not None]
    reviewed = len([r for r in rows if r["delivery"] != "unreviewed"])

    rcfg = ((cfg or {}).get("review") or {}) if isinstance(cfg, dict) else {}
    return {
        "chapters": len(rows),
        "reviewed": reviewed,
        "coverage": round(sum(covs) / len(covs), 3) if covs else 0.0,
        "quality_avg": round(sum(qs) / len(qs), 1) if qs else None,
        "delivery_counts": counts,
        "min_coverage": float(rcfg.get("rubric_min_coverage", 0.80)),
        "rows": rows,
    }
