# -*- coding: utf-8 -*-
"""语言质检（Language QA）：零 LLM 成本的确定性扫描门。

来源：18-Agent 创作流水线的"语言质检员 + 句式词频审查官"两道门的通用化。
只做正则与统计，不做语义判断；规则全部在 data/lang_rules.json，可按平台/题材增删。

扫描维度：
1. 黑名单（AI 口癖/情绪直白/句式黑名单/上帝视角）——命中即标
2. 句式词频（XX了很久/地说/了一下/对话标签/心理标签）——按章字数折算阈值
3. 结构统计（段落内短语重复/连续同主语句/连续短句）——疲劳感检测

门禁语义：黑名单 high 或 mid 命中总数 > 上限、或任一 freq 超阈值 → 不通过，
打回写手带命中清单重写。确定性检查不花一分钱 token，所以放在所有 LLM 门之前。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .config import data_path

_CACHE: dict | None = None


def load_rules(force: bool = False) -> dict:
    global _CACHE
    if _CACHE is None or force:
        _CACHE = json.loads(data_path("lang_rules.json").read_text(encoding="utf-8"))
    return _CACHE


# ---------------------------------------------------------------------------
# 思考泄漏硬检（零 token，确定性）
#
# 思考型模型（glm/deepseek 等）偶发把 chain-of-thought 当正文返回。
# 历史事故：glm-5.3 把 16384 输出预算全烧在英文思维链上，旧判据只看开头 400 字
# 且要求"中文 < 10 字"，思维链里夹了中文 beat 就漏检 → 8 章落盘 19 万字（目标 9000）。
# 本函数给写稿、hybrid 续写、润色三处共用，判据是全文统计而非开头采样。
# ---------------------------------------------------------------------------

_THINK_MARK = re.compile(
    r"(?i)(let me analyze|let me think|let's |i need to|i'll |^first, |wait, |"
    r"hmm,|actually,? |requirements:|section \d|beat \d|options:|the task|we need )",
    re.M,
)
_PLAN_LINE = re.compile(
    r"(?i)^(?:#{1,4}\s*)?(?:section|beat|beats|step|opening hook|ending hook|"
    r"chapter outline|outline|word count|target|要求|提纲)\b"
)
_CN_PLAN_LINE = re.compile(r"^(?:本章目标|节拍|开篇钩子|章末钩子|分节计划|情节拍)\s*[:：]")

# 中文思维链开场白：「好的，我先分析一下大纲：」「下面我梳理一下人物关系：」
# 历史盲区（2026-10-26 实测）：上面的判据只认英文标志词和固定提纲前缀，
# 而国产模型最常吐的恰恰是**中文**分析腔——"好的，我先分析一下…："后跟编号清单。
# 实测该形态 think_leak() 全 False，会原样落盘进成稿（判据③要求 plan>=2 也拦不住
# 单行开场白）。这里补一条"分析腔开场白 + 清单结构"的组合判据：单有开场白不算
# （可能是人物对话），必须同时有编号/英文标志行才判泄漏，避免误伤正常叙事。
_CN_ANALYSIS_OPEN = re.compile(
    r"^(?:好的[，,]?\s*)?(?:我(?:先|来|们先)?(?:分析|梳理|规划|构思|思考|拆解)(?:一下|下)?"
    r"|下面(?:我)?(?:来)?(?:分析|梳理|规划|构思)|思路如下|先(?:分析|梳理|拆解)(?:一下|下)?)"
    r"[^。！？]{0,24}[：:]\s*$"
)
_CN_NUM_ITEM = re.compile(r"^\d{1,2}[.、)）]\s*\S{1,24}$")


def _line_is_english(ln: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", ln))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", ln))
    return latin >= 6 and latin > cjk * 2


def think_leak(text: str) -> bool:
    """正文是否为思考泄漏（思维链/英文计划/提纲），全文口径。

    任一成立即判泄漏：
      ① 思考标志行 ≥2 且（英文行 ≥2 或 提纲行 ≥1）；
      ② 非空行 ≥5 且英文行占比 > 30%；
      ③ 提纲式行 ≥2（Section/Beat/Opening hook 或「本章目标：」等）；
      ④ 中文分析腔开场白（「好的，我先分析一下大纲：」）≥1 且编号清单行 ≥2。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    think = sum(1 for ln in lines if _THINK_MARK.search(ln))
    eng = sum(1 for ln in lines if _line_is_english(ln))
    plan = sum(1 for ln in lines if _PLAN_LINE.match(ln) or _CN_PLAN_LINE.match(ln))
    if think >= 2 and (eng >= 2 or plan >= 1):
        return True
    if len(lines) >= 5 and eng / len(lines) > 0.3:
        return True
    if plan >= 2:
        return True
    cn_open = any(_CN_ANALYSIS_OPEN.match(ln) for ln in lines)
    if cn_open and sum(1 for ln in lines if _CN_NUM_ITEM.match(ln)) >= 2:
        return True
    return False


def think_leak_hint() -> str:
    """思考泄漏的打回提示（写手可见，避免"分析式"输出）。"""
    return ("上一稿不是小说正文，而是模型的分析/计划（英文思维链、提纲、Beats/Hook 列表、"
            "「好的，我先分析一下大纲」这类中文分析腔）。"
            "请直接输出小说正文：全中文、第一人称、只写情节与对话；"
            "不要计划、不要提纲、不要英文、不要自我分析、不要章节说明。")


# ---------------------------------------------------------------------------
# 用户禁用道具硬检（config.kb.negative_terms）
# 与黑名单规则独立：词表来自用户配置，全链路（提梗→选题→大纲→逐章→润色）通用。
# ---------------------------------------------------------------------------

def exempt_terms_from_idea(cfg: dict, *texts: str) -> list[str]:
    """把"用户自己写进选题里的禁用道具"从本篇禁用表里摘出去。

    为什么必须摘：禁用道具表是给模型划的红线（别自己去写那些玩意），
    但用户用 `--idea` 亲口写下的核心道具，禁掉就是自相矛盾——实测大纲阶段
    会因此连挂两轮（"命中用户禁用道具 存单、存折：整篇重新设计主线冲突"），
    最后只能靠模型硬凑一个等价物，把用户给的设定改歪。

    返回被摘掉的词（供 report/日志留痕），并就地改写 cfg 里的词表。
    """
    kb = cfg.setdefault("kb", {})
    terms = [str(t).strip() for t in (kb.get("negative_terms") or []) if str(t).strip()]
    if not terms:
        return []
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return []
    hit = [t for t in terms if t in blob]
    if not hit:
        return []
    kb["negative_terms"] = [t for t in terms if t not in hit]
    cfg.setdefault("_banned_exempt", []).extend(hit)
    print(f"[props] 选题本身含禁用道具 {'、'.join(hit)}，本篇已临时放行（其余仍禁）")
    return hit


def banned_terms(cfg: dict | None) -> list[str]:
    """config.kb.negative_terms → 用户禁用道具词表。"""
    terms = ((cfg or {}).get("kb") or {}).get("negative_terms") or []
    return [str(t).strip() for t in terms if str(t).strip()]


# 成语/固定搭配白名单：这些词里含禁用道具名，但语义上不是道具。
# 实测代价：成稿里「你这如意算盘打得真响」被当成禁用道具「算盘」，
# 触发 3 轮整稿重写（约 6 万字 token）后仍然命中——因为模型改不动一个成语。
# 匹配时先把这些搭配从文本里"挖掉"再扫，避免无解的空转。
_BANNED_IDIOM_SAFE = [
    "如意算盘", "打小算盘", "小算盘", "算盘落空", "算盘打错", "算盘珠子",
    "心里有本账", "心里那本账", "一本明白账", "一笔糊涂账", "秋后算账",
    "翻旧账", "算旧账", "认账", "买账", "不买账", "报账", "对账", "结账",
]


def _mask_idioms(text: str) -> str:
    """把白名单里的成语/固定搭配替换成等长占位符，避免误判为道具。"""
    if not text:
        return text
    for w in _BANNED_IDIOM_SAFE:
        if w in text:
            text = text.replace(w, "\u3000" * len(w))
    return text


def banned_regex(cfg: dict | None) -> "re.Pattern | None":
    """禁用道具正则（词表为空时返回 None）。"""
    terms = banned_terms(cfg)
    if not terms:
        return None
    return re.compile("|".join(re.escape(t) for t in terms))


def scan_banned(text: str, cfg: dict | None) -> dict:
    """扫描正文是否命中用户禁用道具。返回 {pass, hits:[{term,count}]}。零成本确定性检查。"""
    rx = banned_regex(cfg)
    if rx is None:
        return {"pass": True, "hits": []}
    counts = Counter(rx.findall(_mask_idioms(text)))
    hits = [{"term": t, "count": c} for t, c in counts.most_common()]
    return {"pass": not hits, "hits": hits}


def format_banned(breg: dict) -> str:
    """禁用道具命中清单 → 给写手的修复指令文本。"""
    if breg.get("pass", True):
        return ""
    items = "、".join(f"「{h['term']}」×{h['count']}" for h in breg["hits"])
    return (
        f"本篇禁用道具硬检命中：{items}。\n"
        "这些道具被用户明令禁止，一个都不能出现。请用情节等价物替换"
        "（换成与主线无关的物件/冲突载体），保持场景与人物关系不变，只换掉被点名的道具本身。"
    )


# ---------------------------------------------------------------------------
# 四类语言缺陷结构硬检（2026-10-26 用户实测反馈驱动）
#
# 用户原话：成稿「还有句子不符合中文语序、细节描写详略不当、重复描写细节表述冗余、
# 可以写成一句话的内容拆成好几个只有短句的问题」。
#
# 旧 scan() 对此四类几乎无感：段内 4 字短语重复(阈值 2) + 连续短句(low，不阻断)。
# 实测反证：一份 5989 字成稿，183 段里只有 31 个唯一段（最长一段重复 24 次），
# langqa_full 仍报 pass=true / hits=0 —— 三条链路（章级门禁、润色复查、看板质量灯）
# 全靠这个结论，等于集体失明。
#
# 阈值全部在 workspace 里 103 篇历史成稿上标定过（docs/audit/2026-10-26_语言门禁标定.md）：
#   · 完全重复段：74/103 篇为 0，仅 29 篇有 → 任何重复都是缺陷，可安全阻断
#   · 欧化句式：作为…的/在…的情况下/进行…处理/如此…以至于 等 0~5% 命中率 → 高置信
#     而「的链」「是…的强调句」在正常中文里 32%/75.7% 篇命中 → 纯误报，已剔除
#   · 碎句：不能用"连续短句数"判定（网文短句节奏是风格要求，46/103 篇会命中）；
#     改用语法上不可能的句首（的/地/得/了/着/过）与"连接词起句"两个精确判据
# ---------------------------------------------------------------------------

# 欧化/翻译腔：中文口语与网文里几乎不会出现的英语语序直译。逐条在 103 篇上测过命中率。
# 名称 → (正则, 严重度)。severity=high 的会阻断重写。
_EURO_RULES: list[tuple[str, str, str]] = [
    ("欧化句式·作为…的", r"作为[^。，；！？\n]{1,12}的", "high"),
    ("欧化句式·对于…来说", r"对于[^。，；！？\n]{1,12}(来说|而言)", "high"),
    ("欧化句式·在…的情况下", r"在[^。，；！？\n]{1,12}的?情况下", "high"),
    ("欧化句式·被…所", r"被[^。，；！？\n]{1,10}所[^。，；！？\n]", "high"),
    ("欧化句式·进行/加以/予以+名词", r"(进行|加以|予以)(了)?[一这那]?(次|番|个)?[^。，；！？\n]{0,6}"
                                     r"(处理|检查|确认|了解|说明|讨论|研究|分析|安排|处置)", "high"),
    ("欧化句式·如此…以至于", r"如此[^。！？\n]{1,16}以至于", "high"),
    ("欧化句式·不但…而且", r"不但[^。！？\n]{1,16}而且", "high"),
    ("欧化句式·…之一", r"[^。，；！？\n]{1,10}之一[。，；！？]", "high"),
    # 「所谓的『恩赏』」是正常引述（后接引号），不是欧化；只判不带引号的「所谓的X」。
    ("欧化句式·所谓（的）", r"所谓的(?![「『“‘])", "mid"),
    ("欧化句式·某种程度上", r"在?某种(程度|意义)上", "high"),
    ("欧化句式·使得", r"使得", "mid"),
    ("欧化句式·被认为（是/为）", r"被认为(是|为)", "high"),
    ("欧化句式·是一个…的人", r"是一个[^。，；！？\n]{1,12}的(人|男人|女人|孩子|姑娘|小伙)", "high"),
    # 「有着」后面不能接「着落／着数」——那是正常中文词（"日子有着落"），
    # 实测 e2e6 成稿欧化只剩这 1 处误报，直接把 A 可投压成 B 有瑕疵。
    ("欧化句式·有着", r"有着(?!落|数|了)[^。，；！？\n]{1,16}", "mid"),
    ("欧化句式·表示（说）", r"(表示|声称|宣称)[：，]\s*[「“]", "mid"),
    ("欧化句式·在…方面", r"在[^。，；！？\n]{1,10}方面", "mid"),
    ("欧化句式·据…（所说/介绍）", r"据[^。，；！？\n]{1,8}(所说|介绍|报道|了解)", "mid"),
    ("欧化句式·长定语前置·一个…的X", r"一个[^。，；！？\n]{6,20}的(男人|女人|女孩|男孩|老人|孩子)", "mid"),
]

# 语法上不可能的句首是空想（实测误报 68.9%），碎句改用句式节奏分布判定，见 fragment_report。
# 对话句（含引号）与感叹/疑问句是网文的正常节奏。
_DIALOG_MARK = re.compile(r"[“”「」]")


def _sent_list(text: str) -> list[str]:
    """按句末标点切句，保留标点（碎句判定需要知道上句是。还是！？）。"""
    parts = re.split(r"(?<=[。！？!?])(?![。！？!?])", text)
    return [p.strip() for p in parts if p.strip()]


def _norm_para(p: str) -> str:
    p = re.sub(r"[\s\u3000]+", "", p)
    return re.sub(r"[“”\"「」『』]", "", p)


def dup_report(text: str, rules: dict | None = None) -> dict:
    """跨段重复检测：完全重复段 + 近似重复段（人名替换型改写）。

    返回 {exact_groups, max_rep, lost, near_pairs, samples}
      exact_groups 完全相同的段落组数
      max_rep      单段被重复写了几遍
      lost         因重复而多出来的段落数（= 总段数 − 唯一段数）
      near_pairs   相似度 ≥ 阈值的段对数（人名/称谓替换型冗余）
    """
    st = (rules or load_rules()).get("structural") or {}
    min_len = int(st.get("dup_para_min_len", 18))
    sim = float(st.get("near_sim", 0.82))
    max_scan = int(st.get("near_max_paras", 400))

    paras = [p.strip() for p in text.split("\n") if p.strip()]
    normed = [_norm_para(p) for p in paras]

    groups: dict[str, list[str]] = {}
    for raw, n in zip(paras, normed):
        if len(n) >= min_len:
            groups.setdefault(n, []).append(raw)
    exact_dups = {k: v for k, v in groups.items() if len(v) > 1}
    max_rep = max((len(v) for v in exact_dups.values()), default=0)
    lost = sum(len(v) - 1 for v in exact_dups.values())

    # 近似重复：只比对"正文段"（带标点、长度相近才算），标题行与元信息天然短且无标点。
    # 段落数超过上限时跳过（10 万字失控稿是 3000+ 段，O(n²) 会跑几十秒；
    # 而这类稿子必然有完全重复段，已被 exact 分支抓到）。
    body = [(i, n) for i, n in enumerate(normed)
            if 24 <= len(n) <= 400 and re.search(r"[。，！？；]", n)]
    near_pairs = 0
    near_samples: list[dict] = []
    if len(body) <= max_scan:
        for a in range(len(body)):
            ia, x = body[a]
            for b in range(a + 1, len(body)):
                _ib, y = body[b]
                if x == y:
                    continue
                if abs(len(x) - len(y)) > max(8, len(x) // 4):
                    continue
                # 先做廉价的 4-gram 交集预筛，命中率低的直接跳过，省掉 SequenceMatcher
                gx = {x[k:k + 4] for k in range(len(x) - 3)}
                gy = {y[k:k + 4] for k in range(len(y) - 3)}
                if not gx or not gy:
                    continue
                overlap = len(gx & gy) / min(len(gx), len(gy))
                if overlap < 0.55:
                    continue
                from difflib import SequenceMatcher
                if SequenceMatcher(None, x, y).ratio() >= sim:
                    near_pairs += 1
                    if len(near_samples) < 3:
                        near_samples.append({"a": paras[ia][:60], "b": paras[_ib][:60]})

    samples = []
    for key, vals in sorted(exact_dups.items(), key=lambda kv: -len(kv[1]))[:2]:
        samples.append({"text": vals[0][:60], "times": len(vals)})

    return {
        "exact_groups": len(exact_dups),
        "max_rep": max_rep,
        "lost": lost,
        "near_pairs": near_pairs,
        "samples": samples,
        "near_samples": near_samples,
    }


def fragment_report(text: str, rules: dict | None = None) -> dict:
    """碎句/长句检测：只抓"整篇节奏"层面的极端离群，不做逐句语法判定。

    为什么放弃逐句判定（2026-10-26 标定结论）：
      最初写的两条判据——「句首是 的/地/得/了/着/过」和「句首是 但/可/然后/就/也」
      ——在 103 篇历史成稿上误报率 68.9%。中文里「但她没说话。」「然后炸了锅。」
      「地上扔着一地鸡毛。」「过了几站，上来个女的。」全都是合法句子，
      其中「地皮/地上/地下」「过了几站」更是把"的/地/了/过"当词内字用。
      用这类判据阻断会造成大面积误伤 + 空转重写（先例：排比检测改成"首3字相同"之前，
      5 段普通叙事被判排比，整稿重写 3 轮仍不过）。

    改测**可判定的分布离群**（103 篇实测：median 中位 18，p10=13，max=142；
    p_short6 中位 0.101，max 0.42；用户实测稿 median=18/49~76 分位，属健康区间）：
      ① 碎句·整篇节奏过碎：句中位长 ≤10 字 且 ≤6 字短句占比 ≥25%
      ② 长句·整篇句子拖沓：句中位长 ≥26 字（中文叙事上限，超过即长句失控）
    """
    st = (rules or load_rules()).get("structural") or {}
    min_median = float(st.get("frag_median_max", 10))
    min_short6 = float(st.get("frag_short6_ratio", 0.25))
    max_median = float(st.get("long_median_min", 26))

    ss = _sent_list(text)
    if len(ss) < 20:
        return {"count": 0, "hits": []}
    lens = [len(re.sub(r"[。！？!?…]+$", "", s)) for s in ss]
    n = len(lens)
    med = sorted(lens)[n // 2]
    p_short6 = sum(1 for x in lens if x <= 6) / n
    hits: list[dict] = []
    if med <= min_median and p_short6 >= min_short6:
        hits.append({
            "kind": "碎句·整篇节奏过碎",
            "text": f"句中位 {med} 字，≤6字短句占 {p_short6:.0%}",
            "note": f"阈值 中位≤{min_median:.0f} 且 短句≥{min_short6:.0%}",
            "severity": "high",
        })
    elif med >= max_median:
        hits.append({
            "kind": "长句·句子拖沓",
            "text": f"句中位 {med} 字",
            "note": f"阈值 ≤{max_median:.0f} 字；长句该拆成短句",
            "severity": "mid",
        })
    return {"count": len(hits), "hits": hits, "median": med, "p_short6": round(p_short6, 3)}


def _dialog_ratio(text: str) -> float:
    """对白字占比的唯一实现：委托 `language.dialog_ratio`（惰性导入避免循环）。"""
    try:
        from .language import dialog_ratio
    except Exception:  # noqa: BLE001
        return 0.0
    return dialog_ratio(text)


def euro_report(text: str) -> dict:
    """欧化/翻译腔检测：中文语序被英语语序污染（长定语前置、从句套从句、被动滥用）。"""
    hits: list[dict] = []
    for label, pat, sev in _EURO_RULES:
        found = re.findall(pat, text)
        if found:
            sample = found[0] if isinstance(found[0], str) else str(found[0])
            hits.append({"kind": label, "text": sample[:20], "count": len(found), "severity": sev})
    return {"count": sum(h["count"] for h in hits), "hits": hits}


def detail_report(text: str, rules: dict | None = None) -> dict:
    """详略失衡检测（可确定性判定的部分）。

    真"详略"是语义判断（该详的情节写略了 / 该略的过场写细了），确定性只能抓三种硬伤：
      ① 超长无对话段（>350 字一整段全是叙述）→ 既详略不当也不可读
      ② 对话占比过低（<15%）→ 全靠概述推进，戏剧张力缺失
      ③ 环境描写连续堆叠（≥4 段无对话也无人物动作）→ 详略倒置
    """
    st = (rules or load_rules()).get("structural") or {}
    long_chars = int(st.get("long_para_chars", 350))
    min_dialog = float(st.get("dialog_ratio_min", 0.15))

    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if not paras:
        return {"count": 0, "hits": []}
    dialog = [p for p in paras if _DIALOG_MARK.search(p)]
    ratio = len(dialog) / len(paras)
    hits: list[dict] = []
    for p in paras:
        if len(p) > long_chars and not _DIALOG_MARK.search(p):
            hits.append({"kind": "详略·超长无对话段", "text": p[:24] + "…",
                         "note": f"{len(p)} 字一整段无对话", "severity": "mid"})
            break
    n_chars = len(re.sub(r"\s", "", text))
    if n_chars >= 2500 and ratio < min_dialog:
        hits.append({"kind": "详略·对话占比过低", "text": f"对话段占比 {ratio:.0%}",
                     "note": f"阈值 ≥{min_dialog:.0%}，全靠叙述推进", "severity": "mid"})
    # ③ 节长失衡：大纲要求各节字数均衡（outline.md 明禁 1500/900 这种分配），
    #    实测头部/尾部常出现 732 字 vs 1853 字的 2.5 倍失衡——这是读者最直观的"详略不当"。
    lens = _section_lengths(text)
    if len(lens) >= 3:
        real = [n for n in lens if n >= 150]
        if len(real) >= 3 and min(real) >= 150 and max(real) >= min(real) * 2.2:
            hits.append({
                "kind": "详略·节长失衡",
                "text": f"最长 {max(real)} 字 / 最短 {min(real)} 字",
                "note": f"{max(real) / min(real):.1f}× 落差，阈值 ≤2.2×；把最长那节的铺陈挪到最短那节的关键拍上",
                "severity": "mid",
            })
    return {"count": len(hits), "hits": hits, "dialog_ratio": round(ratio, 3)}


def _section_lengths(text: str) -> list[int]:
    """按小节头切分并返回各节字数（无节头时返回整体字数一段）。"""
    from .util import is_sec_head
    lens: list[int] = []
    buf: list[str] = []

    def _flush():
        n = len(re.sub(r"\s", "", "".join(buf)))
        if n:
            lens.append(n)
        buf.clear()

    started = False
    for line in text.splitlines():
        if is_sec_head(line):
            if started:
                _flush()
            started = True
            continue
        buf.append(line)
    _flush()
    return lens


def _freq_limits(rules: dict, n_chars: int) -> dict:
    """按章字数线性折算阈值（基准 2000 字）。"""
    k = max(0.5, n_chars / 2000)
    base = rules["freq"]
    out = {}
    for key, val in base.items():
        if key.startswith("_"):
            continue
        out[key] = max(1, round(val * k))
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?…]+", text)
    return [p.strip() for p in parts if p.strip()]


def _ctx(text: str, start: int, end: int, span: int = 16) -> str:
    """取命中位置所在的句子（截断到 span 字）——打回清单必须让写手看见整句，
    只给「静了两秒」四个字时模型经常改不中或换个位置原样再犯。"""
    left = max(text.rfind(p, 0, start) for p in "。！？；\n" if text.rfind(p, 0, start) >= 0) if any(
        p in text[:start] for p in "。！？；\n") else -1
    right_cands = [text.find(p, end) for p in "。！？；\n" if text.find(p, end) >= 0]
    right = min(right_cands) if right_cands else len(text)
    sent = text[left + 1:right].strip().replace("\n", " ")
    if len(sent) > span:
        core = text[start:end]
        # 以命中片段为中心截取
        pos = sent.find(core)
        if pos < 0:
            pos = 0
        lo = max(0, pos - (span - len(core)) // 2)
        sent = ("…" if lo > 0 else "") + sent[lo:lo + span] + ("…" if lo + span < len(sent) else "")
    return sent


def _rule_genres(rule: dict) -> tuple[str, ...]:
    g = rule.get("genre")
    if not g:
        return ()
    return (g,) if isinstance(g, str) else tuple(g)


def _rule_not_genres(rule: dict) -> tuple[str, ...]:
    """排除题材：`not_genre: ["guyan"]` 表示**只在现代题材生效**。

    用途是那些「现代稿里是 AI 味套词、古装稿里是正常书面语」的词。典型例子「些许」：
    都市稿「她心里些许不安」是典型 AI 腔；古言「银霜炭比上月轻了些许」是正经写法，
    拿同一条规则硬扫会把好句子判成病。
    """
    g = rule.get("not_genre")
    if not g:
        return ()
    return (g,) if isinstance(g, str) else tuple(g)


def scan(text: str, rules: dict | None = None, genres: tuple[str, ...] | list[str] = ()) -> dict:
    """扫描正文，返回 {pass, hits, stats}。hits: [{kind,label,note,count/pos}]

    `genres` 是当前题材标签（如 ("guyan",)）。`lang_rules.json` 里带 `genre` 的规则
    **只在该题材生效**——古装题材特有的 AI 味（「眼底闪过一丝」「眸色一沉」
    「现代词混入」）拿去扫都市稿是纯噪音，反之亦然。没有 `genre` 键的规则全题材通用。
    """
    rules = rules or load_rules()
    want = set(genres or ())
    hits: list[dict] = []
    n_chars = len(re.sub(r"\s", "", text))

    # 1) 黑名单
    high_mid = 0
    for rule in rules["blacklist"]:
        rg = _rule_genres(rule)
        if rg and not (want & set(rg)):
            continue
        ng = _rule_not_genres(rule)
        if ng and (want & set(ng)):
            continue
        found = list(re.finditer(rule["pattern"], text))
        if not found:
            continue
        sample = found[0].group(0)
        ctx = _ctx(text, found[0].start(), found[0].end())
        note = f"示例「{sample}」命中{len(found)}处"
        if ctx and ctx != sample:
            note += f"（所在句：{ctx}）"
        hits.append(
            {
                "kind": "黑名单",
                "label": rule["label"],
                "note": note,
                "suggest": rule["suggest"],
                "severity": rule["severity"],
            }
        )
        if rule["severity"] in ("high", "mid"):
            high_mid += len(found)

    # 2) 句式词频（阈值按字数折算）
    lim = _freq_limits(rules, n_chars)

    def freq_hit(key: str, pattern: str) -> int:
        found = list(re.finditer(pattern, text))
        n = len(found)
        if n > lim[key]:
            ctx = _ctx(text, found[0].start(), found[0].end()) if found else ""
            note = f"出现{n}次（阈值≤{lim[key]}）"
            if ctx:
                note += f"，实例：{ctx}"
            hits.append(
                {
                    "kind": "词频",
                    "label": rules["freq"].get(f"_label_{key}", key),
                    "note": note,
                    "suggest": "用多样化表达或动作描写替代",
                    "severity": "mid",
                }
            )
        return n

    freq_hit("jiule", r"[\u4e00-\u9fa5]{1,3}了很久")
    freq_hit("deshuo", r"[\u4e00-\u9fa5]{1,3}(说道|地说|地说着|地问)")
    freq_hit("yixia", r"[\u4e00-\u9fa5]{1,3}了一下")
    freq_hit("dialog_tag", r"[\u4e00-\u9fa5]{0,4}(说道|问道|答道|说[：:”]|道[：:”])")
    freq_hit("psy_tag", r"(她|他|我)(心想|暗想|知道|意识到|明白|清楚)(?!地)")
    # AI味·表演性精确（用户标准：测量式炫技细节，如"三厘米""一指宽"——细节只在
    # 有情节后果时才该量化）与比喻密度（奇怪/万能比喻同样是AI味）
    freq_hit("biyu", r"(像[^。，！？\n]{2,18}(一样|一般|似的)|宛如|恍若|犹如|仿佛)")
    freq_hit("celiang", r"(厘米|毫米|指宽|精确到|一?[指份毫厘][宽寸])")
    # AI味·弱化副词成瘾（缓缓/微微/轻轻/淡淡）与气场宣告
    freq_hit("ruohua", r"(缓缓|微微|轻轻|淡淡|悄然)")
    freq_hit("shengdao", r"散发着")

    # 3) 结构统计
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    # 排比检测：连续 ≥4 段"同构开头"，文档红线"排比/罗列"。
    #
    # 历史缺陷：旧判定是 `re.match(r"^[他她我]{1,2}[\u4e00-\u9fa5]$", head) or head == run_head`。
    # 前半段几乎命中所有中文叙事段落——「我笑着…」「他伸手…」「我没缩手…」这种
    # 正常的主语开头连排 4 段就报警。实测成稿里 5 段普通叙事被判成排比，
    # 而确定性复查又改不掉（中文叙事天然以代词开头），于是空转 3 轮整稿重写。
    # 真正的排比是**结构重复**：连续 ≥4 段首 3 字完全相同才算（如「她看着…」×4）。
    # 只认这一条判据：首 2 字相同太松（「我说好。」「我说好，谢谢。」是正常对话回声），
    # 首 3 字相同才能把"排比"和"网文短句节奏"分开。实测历史 89 份成稿仅 2 份触发，
    # 且都是真·同构堆砌的脏稿，无误伤。
    run_head, run_len, best = None, 0, 0
    for p in paras:
        head = p[:3]
        if len(head) == 3 and head == run_head:
            run_len += 1
        else:
            run_len = 1
        run_head = head
        best = max(best, run_len)
    if best >= 4:
        hits.append(
            {
                "kind": "结构",
                "label": "排比·同构段连排",
                "note": f"连续 {best} 段同构开头（红线：每段排比不超过 1 处）",
                "suggest": "打破句式节奏：换主语位置、用对话或动作插叙隔开",
                "severity": "mid",
            }
        )
    # 段内 4 字短语重复
    rep = lim["phrase_repeat_in_para"]
    for i, para in enumerate(paras):
        grams = re.findall(r"[\u4e00-\u9fa5]{4}", para)
        for g, c in Counter(grams).items():
            if c > rep and not re.match(r"^[\u4e00-\u9fa5]{0,2}(的|了|着)", g):
                hits.append(
                    {
                        "kind": "段落重复",
                        "label": f"第{i+1}段短语重复",
                        "note": f"「{g}」同段出现{c}次（阈值≤{rep}）",
                        "suggest": "用代词或改写替代",
                        "severity": "low",
                    }
                )
                break  # 每段报一处即可

    # 连续同主语句
    sents = _sentences(text)
    run = max_run = 0
    for s in sents:
        if re.match(r"^(她|他|我)", s):
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    if max_run >= lim["same_subject_run"]:
        hits.append(
            {
                "kind": "句式堆叠",
                "label": "连续同主语句",
                "note": f"连续{max_run}句以她/他/我开头（阈值<{lim['same_subject_run']}）",
                "suggest": "换主语、倒装或用动作切入",
                "severity": "low",
            }
        )

    # 连续短句（段落过碎）
    short_run = max_short = 0
    for s in sents:
        if len(s) <= 8:
            short_run += 1
            max_short = max(max_short, short_run)
        else:
            short_run = 0
    if max_short >= lim["short_sentence_run"]:
        hits.append(
            {
                "kind": "句式堆叠",
                "label": "连续短句段落碎",
                "note": f"连续{max_short}个≤8字短句（阈值<{lim['short_sentence_run']}）",
                "suggest": "合并短句，长短句结合",
                "severity": "low",
            }
        )

    # 4) 四类结构缺陷（重复/碎句/欧化/详略）——用户实测反馈驱动，见文件上方注释
    st = rules.get("structural") or {}
    _d = dup_report(text, rules)
    if _d["max_rep"] >= int(st.get("dup_para_min_rep", 2)):
        sev = "high" if (_d["max_rep"] >= 3 or _d["lost"] >= int(st.get("dup_para_lost_high", 3))) else "mid"
        s = _d["samples"][0] if _d["samples"] else {"text": "", "times": _d["max_rep"]}
        hits.append({
            "kind": "重复描写",
            "label": "段落整段重复",
            "note": f"同一段重复了 {_d['max_rep']} 遍（共 {_d['lost']} 个多余段落），示例：{s['text']}",
            "suggest": "删掉重复段落，只留一处；若确需呼应，改成不同措辞且只保留一次",
            "severity": sev,
        })
    if _d["near_pairs"] >= int(st.get("near_pairs_mid", 2)):
        pair = _d["near_samples"][0] if _d["near_samples"] else {"a": "", "b": ""}
        hits.append({
            "kind": "重复描写",
            "label": "近似段落冗余（换名改写）",
            "note": (f"{_d['near_pairs']} 对段落内容雷同，示例：「{pair['a']}」与「{pair['b']}」"),
            "suggest": "同一信息只写一次；把后出现的那段改成推进情节的新内容，不要换个主语/称谓重说一遍",
            "severity": "mid",
        })

    _f = fragment_report(text, rules)
    if _f["count"]:
        sev = "high" if any(h["severity"] == "high" for h in _f["hits"]) else "mid"
        top = _f["hits"][0]
        hits.append({
            "kind": "句式节奏",
            "label": top["kind"].split("·")[-1],
            "note": f"{top['text']}（{top['note']}）",
            "suggest": "把本可用逗号连成一句的碎片合并成完整句；反过来，超过 40 字的长句拆成两三个短句",
            "severity": sev,
        })

    _e = euro_report(text)
    euro_max = int(st.get("euro_max", 2))
    if _e["count"] > euro_max:
        kinds = "、".join(f"{h['kind'].split('·')[-1]}×{h['count']}" for h in _e["hits"][:5])
        sev = "high" if any(h["severity"] == "high" for h in _e["hits"]) else "mid"
        hits.append({
            "kind": "欧化句式",
            "label": "中文语序被英语语序污染",
            "note": f"命中 {_e['count']} 处（阈值≤{euro_max}）：{kinds}",
            "suggest": "改成中文自然语序：定语后置成短句、去掉「作为/对于/关于/进行」等翻译腔外壳、被动改主动",
            "severity": sev,
        })

    _dt = detail_report(text, rules)
    for h in _dt["hits"]:
        hits.append({
            "kind": "详略",
            "label": h["kind"].split("·")[-1],
            "note": h.get("note") or h["text"],
            "suggest": "该详的写足动作与对话，该略的一句话带过；删掉不推进情节的铺陈",
            "severity": h["severity"],
        })

    # 5) 格式结构（markdown 记号 / 小标题 / 外文元叙述泄漏）
    #   合法行：首行「01 小节标题」编号行、直接写模式开头的「## 短标题」行、
    #   成稿组装层 chrome（# 一级标题 / > 平台：/ **一句话故事** / **导语** 等加粗标签）
    for i, raw_line in enumerate(text.splitlines()):
        s = raw_line.strip()
        if not s:
            continue
        if re.match(r"^\d{1,2}\s", s):  # 「01 小节标题」合法编号行
            continue
        if i < 3 and re.match(r"^#{1,2}\s", s) and len(s) <= 40:  # 直接写模式短标题行/成稿一级标题
            continue
        if re.match(r"^>\s*平台[:：]", s):  # 组装层平台信息行
            continue
        if re.match(r"^\*\*(一句话故事|导语|作者|责编|标签)\*\*", s):  # 组装层加粗标签行
            continue
        if re.match(r"^#{1,6}\s|^\*\*|^__|^[-*+]\s|^>", s):
            hits.append(
                {
                    "kind": "格式",
                    "label": "markdown 记号/小标题",
                    "note": f"第{i + 1}段出现非正文记号：{s[:24]}",
                    "suggest": "删掉全部 markdown 记号与小标题行，正文只留段落文字",
                    "severity": "high",
                }
            )
            continue
        if re.match(r"^[一二三四五六七八九十]+、", s) and len(s) <= 30:
            hits.append(
                {
                    "kind": "格式",
                    "label": "中文序号小标题",
                    "note": f"第{i + 1}段是小标题行：{s[:24]}",
                    "suggest": "删掉小标题，直接写正文",
                    "severity": "high",
                }
            )
            continue
        latin = len(re.findall(r"[A-Za-z]", s))
        cjk = len(re.findall(r"[\u4e00-\u9fa5]", s))
        if latin >= 10 and (cjk == 0 or latin > cjk * 2):
            hits.append(
                {
                    "kind": "格式",
                    "label": "外文/元叙述泄漏",
                    "note": f"第{i + 1}段混入外文提纲或说明文字：{s[:30]}",
                    "suggest": "删掉全部非正文内容（英文提纲/场景编号/说明），只留中文叙事正文",
                    "severity": "high",
                }
            )

    stats = {
        "chars": n_chars,
        "paras": len(paras),
        # 对白占比只认一个判据（`language.dialog_ratio`，按引号内字数算）。
        # 旧实现数「含引号或冒号的段落」——冒号把「他说：」这种叙述也算成对白，
        # 一度把真实 0.22 的稿子报成 0.55，与体检/章级门三个数各不相同。
        "dialog_ratio": _dialog_ratio(text),
        "high_mid_blacklist": high_mid,
        "dup_lost": _d["lost"],
        "dup_max_rep": _d["max_rep"],
        "near_pairs": _d["near_pairs"],
        "frag": _f["count"],
        "euro": _e["count"],
    }
    blocking = [h for h in hits if h["severity"] in ("high", "mid")]
    return {"pass": len(blocking) == 0, "hits": hits, "stats": stats}


def format_issues(report: dict) -> str:
    """把扫描结果格式化成给写手重写 prompt 的意见清单。"""
    if not report["hits"]:
        return "（无）"
    lines = []
    for i, h in enumerate(report["hits"][:15], 1):
        lines.append(
            f"{i}. [{h['kind']}/{h['severity']}] {h['label']}：{h['note']} → 改法：{h['suggest']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 题材标签（决定哪些 `genre` 规则生效）
# ---------------------------------------------------------------------------
# 只认**背景设定**，不认叙事装置：「穿书」「快穿」是穿越机制，故事可能发生在现代
# 写字楼里，把它们算成古装会让古言规则（现代词混入=high）误伤正常都市稿。
_ANCIENT_MARKS = ("宫廷", "宫斗", "宅斗", "古言", "古风", "权谋", "后宫", "仙侠", "修真",
                  "武侠", "民国", "宫墙", "朝堂", "侯门", "世家", "修仙", "玄幻",
                  "冲喜", "换嫁", "冷宫")


_MODERN_MARKS_TEXT = ("微信", "支付宝", "转账", "手机", "存折", "银行卡", "信用卡", "超市",
                      "外卖", "物业", "电梯", "小区", "快递", "网贷", "社保", "医保",
                      "公积金", "工资", "发票", "加班", "面试", "公司", "老板", "房贷",
                      "车贷", "驾照", "直播", "短视频", "朋友圈", "总监", "同事", "学区房")


# 选题里出现的**人物/物事**级古装标记（比方向名更细：选题写的是「嫡姐」「皇上」，
# 不会写「宫廷权谋」）。用于把宫斗/宅斗选题拉回古代方向，别落进现代方向。
_IDEA_ANCIENT_MARKS = ("皇上", "皇帝", "皇后", "贵妃", "妃", "嫔", "宫女", "太监", "宫人",
                       "娘娘", "殿下", "臣妾", "朕", "王爷", "丞相", "将军", "世子",
                       "太医", "圣旨", "凤印", "冷宫", "嫡姐", "嫡女", "庶妹", "庶女",
                       "府邸", "丫鬟", "小厮", "银两", "铜板", "县衙", "衙门", "老爷",
                       "夫人", "姑娘", "庄子", "牙婆", "宫墙", "朝堂", "侯门", "世家")


def idea_genre(text: str) -> tuple[str, ...]:
    """从**选题文本**判断题材归属，返回 ("guyan",) / ("modern",) / ()。

    为什么需要：`pipeline.match_direction()` 用选题关键词匹配 42 个方向，宫斗类
    选题（嫡姐/进宫/皇上/盖头）一个关键词都命中不了——方向的 keywords 全是现代
    口语词——于是回落到硬编码默认值「全村人的眼光」，写成一篇「宫廷 + 全村」的
    四不像（2026-10-26 古言实测 #3 实测）。
    """
    d = str(text or "")
    if any(m in d for m in _IDEA_ANCIENT_MARKS):
        return ("guyan",)
    if any(m in d for m in _MODERN_MARKS_TEXT):
        return ("modern",)
    return ()


def scan_for(text: str, direction: str = "", rules: dict | None = None) -> dict:
    """按题材标签扫描（`scan` 的题材感知外壳）。

    调用方只知道自己手上的 direction 名（「宫廷权谋」），不该关心标签怎么映射；
    映射规则只留在 `genre_tags` 一处，避免每个调用点自带一份题材表。
    """
    return scan(text, rules=rules, genres=genre_tags(direction))


def genre_tags(direction: str) -> tuple[str, ...]:
    """把方向名映射成规则标签。目前只有 `guyan`（古装）一类需要区分。

    为什么需要：`lang_rules.json` 里「眼底闪过一丝」「眸色一沉」「现代词混入」
    这类规则只在古装题材成立；拿去扫都市稿是纯噪音，反之都市的 AI 味套词
    （「指节泛白」「烟灰缸」）扫古装稿也是噪音。标签缺省为空 = 只跑通用规则。
    """
    d = str(direction or "")
    return ("guyan",) if any(m in d for m in _ANCIENT_MARKS) else ()
