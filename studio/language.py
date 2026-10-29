# -*- coding: utf-8 -*-
"""全稿语言打磨：把四类语言缺陷从"只统计"变成"真改掉"。

## 为什么需要这个模块（用户实测反馈，2026-10-26）

用户对成稿的原话：
  1. 句子不符合中文语序
  2. 细节描写详略不当
  3. 重复描写、细节表述冗余
  4. 可以写成一句话的内容拆成好几个只有短句

在此之前，这四类问题在 `langqa.scan()` 里有检测、`report["langqa_full"]` 里有计数，
但 `stage_polish` 拿到结果只 print 一行、**从不驱动任何修改**（前一轮审计记作 F6）。
结果是：报告写着"语检 8 处命中"，成稿一个字没变。

## 本模块的闭环

    scan 定位（确定性）
      → 逐节定向改写（LLM，只改表达不改情节）
      → 逐节复验（字数/缺陷分/人名不丢/禁用道具不新增，不达标回滚原文）
      → 全稿终验（仍不过则再一轮，最多 rounds 轮）
      → 整段重复的确定性删除（安全兜底，不花 token）

## 边界

- 只改"S怎么说"，不改"说什么"：情节、人物、对话含义、出场顺序、节数一律不变。
- 每节独立改写，节头保留，越界内容整节丢弃。
- 任何一节复验不过就整节回滚——**宁可这节不改善，也不许改坏**。
"""
from __future__ import annotations

import json
import re

from . import langqa
from .config import data_path
from .langqa import _EURO_RULES
from .util import SEC_HEAD, count_words_zh

# 每节缺陷分达到这个阈值才值得花一次 LLM 调用。
# 标定依据（101 篇 / 864 节历史成稿；取"无整段重复"的 69 篇干净稿统计）：
#   节得分 p50=3 / p80=8 / p90=12 / p95=16
# 阈值 8 时，中位数篇目只有 12% 的节被打回（8 节稿约 1 节），
# 而真·脏稿（p90 达 83% 的稿子）会被逐节修——正是想要的区分度。
# 阈值 6 会把中位数篇目的 22% 的节打回，越修越乱的先例就在那里。
SECTION_SCORE_MIN = 8

# 每轮最多改几节（控制 token 与耗时；一篇 9 千字 8 节，两轮基本覆盖所有脏节）
MAX_SECTIONS_PER_ROUND = 4

# 改写后字数允许区间（相对原文）
WORD_RATIO_LO = 0.88
WORD_RATIO_HI = 1.12

# —— 投稿级质量门槛（单一权威源）——
# 为什么放在这里：流水线的"要不要返修"和 tools/manuscript_audit.py 的"能不能投"
# 必须是同一套数字，否则会出现"体检 90 分、投稿被打回"或"体检不及格但流水线认为已通过"。
# 之前的教训：节头正则曾有 5 份实现、思考泄漏 3 份、硬禁词 4 份——阈值同理，只能有一份。
#
# 标定：104 篇历史成稿的每千字缺陷密度分布
#   score     p25=2.8  p50=4.83  p75=13.6
#   chain     p25=0.44 p50=1.15  p75=1.89
#   frag      p25=0     p50=0     p75=0.71
#   order     p25=0     p50=0     p75=0.86
#   redundancy p25=0    p50=0     p75=1.09
# 取"明显好于中位"的 p25 档作为投稿门槛：中位稿不合格，这才是"符合市场标准"该有的位置。
MARKET_TARGET = {
    "score_per_k": 3.0,          # 缺陷总分/千字
    "frag_per_k": 0.30,          # 碎句（连续短句没合并）
    "longsen_per_k": 0.60,       # 长句（该拆没拆）——与碎句相反方向的病，分开计量
    "chain_per_k": 0.60,         # 同主语短句链
    "order_per_k": 0.30,         # 语序硬伤
    "euro_per_k": 0.05,          # 欧化/翻译腔
    "redundancy_per_k": 0.40,    # 冗余复读
    "detail_per_k": 0.60,        # 详略失衡
    "dialog_ratio_min": 0.25,    # 对话段占比（短篇网文靠对话推冲突）
    "section_ratio_max": 2.0,    # 最长节/最短节
    "words_min": 8000,
    "words_max": 12000,
    "long_para_chars": 200,      # 超过这个字数的纯叙述段判详略失衡
}

# 单节是否值得花一次 LLM 返修：密度超门槛，且有实际缺陷分（避免 300 字小节的噪声放大）
SECTION_NEEDS_POLISH_MIN = 4

# 章级初稿门的放宽倍数。
# WHY：写手交出来的是**未打磨初稿**，最终成稿还要过 deterministic_repair（零
# token 机械合并）+ lang_polish + 整稿体检。拿投稿线（chain 0.6/千字）当章级门，
# 等于要求初稿直接达标——实测 e2e4 初稿 chain 12.22/千字，按投稿线每章必然
# maxed_langdefect，白烧写手预算还可能把情节改坏。
#
# 取值必须同时满足两条硬约束，否则章级门要么永远报警、要么形同虚设：
#   1) **高于最小可测量子**。`_local_chain` 每段文本只取最长那一条链并计
#      `(n-3+1)*4` 分；一条 3 句链在千字章里就是 4.0/千字。阈值若 <4.0（旧值
#      5 倍 = 3.0 就踩了这个坑），短章无论如何都过不了门——e2e7 实测每章白烧
#      2 轮写手才 maxed_langdefect 放行。
#   2) **确实能拦住失控**。15 倍 = 9.0/千字，等价于"每千字最多容忍一条 4 句
#      同主语短句链（8.0/千字），5 句起（12.0/千字）就回修"。
# 定名原则：章级门是**止血门**，不是达标门；投稿线由整稿返修把关。
CHAPTER_TARGET_SCALE = 15.0


def chapter_targets(scale: float | None = None) -> dict:
    """章级（初稿）用的放宽门槛：每千字密度 ×scale，比率类下限 ÷scale。"""
    s = CHAPTER_TARGET_SCALE if scale is None else float(scale)
    out = dict(MARKET_TARGET)
    for k, v in MARKET_TARGET.items():
        if k.endswith("_per_k") and isinstance(v, (int, float)):
            out[k] = round(v * s, 3)
        elif k == "dialog_ratio_min" and isinstance(v, (int, float)):
            out[k] = round(v / s, 3)
    return out


def dense(sc: dict, n_chars: int) -> dict:
    """把 defect_score 的绝对分折算成每千字密度（跨节长可比）。"""
    k = max(int(n_chars), 1) / 1000.0
    return {key: round(float(sc.get(key) or 0) / k, 2) for key in
            ("score", "frag", "longsen", "chain", "order", "euro", "redundancy", "detail", "dup")}


def language_fails(text: str, rules: dict | None = None,
                   targets: dict | None = None) -> list[str]:
    """按投稿门槛判一段文本（整稿或单节）卡在哪几项；空列表=通过。"""
    tg = dict(MARKET_TARGET)
    tg.update(targets or {})
    n_chars = len(re.sub(r"\s", "", text or ""))
    if n_chars < 200:  # 太短不判（导语、尾句）
        return []
    sc = defect_score(text, rules)
    d = dense(sc, n_chars)
    bad: list[str] = []
    if d["score"] > tg["score_per_k"]:
        bad.append(f"缺陷总分 {d['score']}/千字 > {tg['score_per_k']}")
    for key, tk in (("frag", "frag_per_k"), ("longsen", "longsen_per_k"),
                    ("chain", "chain_per_k"),
                    ("order", "order_per_k"), ("euro", "euro_per_k"),
                    ("redundancy", "redundancy_per_k"), ("detail", "detail_per_k")):
        if d[key] > tg[tk]:
            bad.append(f"{key} {d[key]}/千字 > {tg[tk]}")
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if paras:
        dialog = sum(1 for p in paras if langqa._DIALOG_MARK.search(p))
        ratio = dialog / len(paras)
        if n_chars >= 900 and ratio < tg["dialog_ratio_min"]:
            bad.append(f"对话段占比 {ratio:.0%} < {tg['dialog_ratio_min']:.0%}")
    return bad


def needs_polish(text: str, rules: dict | None = None) -> bool:
    """单节是否该返修（供 stage_polish / 章级门禁共用）。

    判据 = 「有实际缺陷分」且「至少一项密度超标」——两个条件是 AND：
    只看总分会让"某单项爆表、总分没到"的节漏掉；只看单项会让 300 字小节的
    1 处碎片被噪声放大成返修（历史上越修越乱就是这么来的）。
    """
    sc = defect_score(text, rules)
    n = len(re.sub(r"\s", "", text or ""))
    if n < 400:
        return sc["score"] >= SECTION_SCORE_MIN
    return sc["score"] >= SECTION_NEEDS_POLISH_MIN and bool(language_fails(text, rules))


def _core_len(s: str) -> int:
    return len(re.sub(r"[。！？!?…；;]+$", "", s).strip())


# 单句超过这个长度就该拆（中文叙事的自然上限）
LONG_SENT_CHARS = 46
# 短句的判定长度（≤9 字算"只有短句"里的一个碎片）
SHORT_SENT_CHARS = 9
# 连续几个（非对话）短句算"该合并却拆开了"。标定：4 太松（网文正常节奏就是 4 拍），改用 5。
SHORT_RUN_MIN = 5
# 每千字允许几个超长句（按密度折算，避免长节被绝对条数误伤）
LONG_BUDGET_PER_K = 2.0
# 长句预算的容忍倍数（见 _local_frag 注释）
LONG_TOLERANCE = 1.25


def _is_dialog_sent(s: str) -> bool:
    """判断一句话是不是对话句——对话天然短，不能算"碎句还没合并"。"""
    return bool(re.search(r"[「“\"][^」”\"]{1,80}[」”\"]", s)) or s.strip().startswith(("「", "“", '"'))


# 引号在句子边界处会互相截断：`_sent_list` 会把
#   「我说的是配合重新核保。核保通不通过，归银行管。」
# 切成 `…重新核保。` / `核保通不通过，归银行管。` / `」` 三句，中间那句看着就是
# 一段「无主语短句」，于是被误判成碎句链（e2e5 节07 chain 4 就是这么来的）。
# 任一侧为真引号的行/句，一律不参与句链判定。
_QUOTE_CHARS = "「」『』“”"

_DQ_OPEN = "「『“‘"
_DQ_CLOSE = "」』”’"


def sents_with_dialog(text: str) -> list[tuple[str, bool]]:
    """切句并标注「这句是否在对话里」——引号被断句截断的台词也要认出来。

    为什么必须有它：`_sent_list` 在 `！` 处断句，`「那是借款！` 的升引号落在
    前一句，后一句 `我有凭证！` 里一个引号都没有 → `_is_dialog_sent` 判它
    「非对话」→ 整段台词被当成 5 个连续短句扣分。实测 e2e7 成稿（8938 字）
    唯一的碎句命中就是这条假阳（"那是借款！我有凭证！你卷走我买房钱！"），
    它把一篇 chain/detail/euro/redundancy 全达标的稿子压成「C 不可投」。

    判据用**引号深度**而不是「句内有引号」：深度在句首或句末 >0 即为对话。
    """
    # 换行也是句子边界：标题 / 「导语：…」/ 正文是独立行，中间没有句号，
    # 不按行切会把「标题+导语首句+正文首句」粘成一个 57 字的假长句
    # （e2e12 实测 defect_brief 给出的长句样例就是以标题开头的拼接串）。
    parts = re.split(r"(?<=[。！？!?])(?![。！？!?])|\n", text or "")
    out: list[tuple[str, bool]] = []
    depth = 0
    for p in parts:
        if not p.strip():
            continue
        d0 = depth
        for ch in p:
            if ch in _DQ_OPEN:
                depth += 1
            elif ch in _DQ_CLOSE:
                depth = max(0, depth - 1)
        out.append((p.strip(), d0 > 0 or depth > 0))
    return out


#: 对白占比的章级下限：低于这个值说明"该吵架的场面被写成了说明文"。
#: 标定依据：最近 8 篇成稿实测 0.22–0.32（番茄/七猫短篇常见区间 0.30–0.40），
#: 差的不是"有没有对话"，而是"叙述替代了场面"——用户抱怨的「详略不当」有一半
#: 是这件事：两句话能吵明白的地方，用三段心理描写带过。
DIALOG_MIN_CHAPTER = 0.28

_DIALOG_RE = re.compile(r"[「『“‘][^」』”’]{1,400}[」』”’]")


def dialog_ratio(text: str) -> float:
    """对白字占比 = 引号内字数 ÷ 全文字数（0–1）。体检/章级门/看板同源判据。"""
    t = text or ""
    if not t:
        return 0.0
    quoted = sum(len(m) for m in _DIALOG_RE.findall(t))
    return round(quoted / max(1, len(t)), 3)


def _local_frag(text: str) -> tuple[int, list[dict], int]:
    """**节内**碎句/长句检测（与 langqa.fragment_report 的整篇中位数口径互补）。

    为什么必须有这一层：整篇 median 口径测的是"整篇节奏"，而用户抱怨的是
    "可以写成一句话的内容拆成好几个只有短句"——这是**局部**现象。
    一篇 median=18（健康）的稿子里完全可以有一节连着 6 个三字句。
    实测用户那篇 9620 字成稿：整篇 fragment_report = 0 命中，
    但逐节扫能抓到"我推开门。他站在那儿。我手在抖。心在跳。"这种节。

    标定（101 篇 / 864 节历史成稿）：
      · 短句连排只数**非对话句**——对话句天然短（「滚。」「你说什么？」），
        算进去会让所有冲突节误报；
      · 长句按**密度**折算（每千字允许 2 个），不按绝对条数——
        一节 1900 字里有 4 个长句是正常的中文叙事，不该被打回。
    """
    hits: list[dict] = []
    score = 0
    n_chars = len(re.sub(r"\s", "", text)) or 1
    pairs = sents_with_dialog(text)
    sents = [s for s, _d in pairs]

    run: list[str] = []
    best: list[str] = []
    for s, in_dlg in pairs:
        if 0 < _core_len(s) <= SHORT_SENT_CHARS and not in_dlg and not _is_dialog_sent(s):
            run.append(s.strip())
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    if len(best) >= SHORT_RUN_MIN:
        score += (len(best) - SHORT_RUN_MIN + 1) * 4
        hits.append({"kind": "碎句·连续短句", "text": "".join(best)[:60],
                     "note": f"连续 {len(best)} 个 ≤{SHORT_SENT_CHARS} 字非对话短句没合并"})

    long_sents = [s for s in sents if _core_len(s) >= LONG_SENT_CHARS]
    budget = LONG_BUDGET_PER_K * n_chars / 1000.0
    long_score = 0
    # 容忍带：中文叙事里长句略超预算不算病（22 句 vs 预算 18.5 是正常的书面节奏）。
    # 不加容忍带时「只要超一句就扣分」，而每点 3 分折算成密度必然越 0.6 的门槛，
    # 等于"长句一出现就判不可投"，与短篇网文实际（大量 40-60 字叙事句）不符。
    if len(long_sents) > budget * LONG_TOLERANCE:
        over = len(long_sents) - budget * LONG_TOLERANCE
        long_score = int(round(over * 3))
        hits.append({"kind": "长句·该拆", "text": long_sents[0].strip()[:60],
                     "note": f"{len(long_sents)} 句超过 {LONG_SENT_CHARS} 字"
                             f"（每千字允许 {LONG_BUDGET_PER_K} 句）"})
    # 长句与短句是**两种相反的病**，必须分开计数（`longsen`）。
    # 旧实现把长句罚分并进 `frag`（碎句），e2e12 实测 22 个长句（允许 18.5）
    # 被判成「frag 1.28/千字 > 0.3」→ 判定"碎句超标"，标签与事实相反，
    # 且 0.3 的阈值是按"短句连排每命中 4 分"标定的，长句残余会直接越线。
    return score, hits, long_score


# 碎句链的长度上限：核心字 ≤ 这个数才算"本该合进上句的碎片"
CHAIN_SENT_CHARS = 22
# 连续几个同主语短句算"一句话被拆成一串"
CHAIN_MIN = 3


def _chain_member(s: str, in_dialog: bool = False) -> bool:
    """一句话是否「可以被合并进上句」——**检测器与合并器共用的唯一判据**。

    为什么必须共用：`_local_chain`（判分）和 `merge_chain_sentences`（动手改）
    原来是各写一套条件，合并器更严（只碰「。」结尾、跳过时间状语开头）。
    后果是**改不掉的残余**：一条「他推开门！她愣住了。他笑了。」的链，
    合并器整条跳过，检测器照旧计 4 分，返修五轮还是那个数——
    e2e6 成稿 chain 1.3/千字（要求 ≤0.6）卡住的正是这种口径分裂。
    判据只留一处，改不动就不扣分。

    不算链成员（=不该合并）的四种情况，每一种都有语义理由：
      1. 对话句——对话天然短，合并会改变谁在说话；
      2. 句内有引号——引号在句子边界会被 `_sent_list` 截断，判出来的"短句"
         其实是台词的一半（实测 e2e5 节07 的 chain 4 全是这种假阳）；
      3. 问号/叹号/省略号结尾——那是节奏，不是碎句；换成逗号会改语气；
      4. 时间/地点状语开头——新叙事节拍，并进上句会读成
         「我推开门，走到窗边，第二天他来了」的语义变形。
    """
    st = s.strip()
    core = re.sub(r"[。！？!?…；;]+$", "", st)
    if not core or in_dialog or _is_dialog_sent(st):
        return False
    if len(core) > CHAIN_SENT_CHARS:
        return False
    if any(q in st for q in _QUOTE_CHARS):
        return False
    if not st.endswith("。"):
        return False
    if _TIME_LEAD.match(core):
        return False
    # 5. 句内已有两个以上分句（≥2 个逗号/顿号）——那已经是复句，不是"一句话拆成几截"。
    #    反例（e2e-古言 实测误判）：「凤冠砸在金砖上，珠翠崩飞，滚了一地。」核心 18 字、
    #    三个分句、节奏紧凑，却因为"≤22 字 + 同主语"被计成碎句链，整篇 chain 0.73/千字
    #    直接从 A 掉到 C。真正该抓的是「我推开门。我走到窗边。我坐下。」这种单分句连排。
    if st.count("，") + st.count("、") > 1:
        return False
    return True


def _local_chain(text: str) -> tuple[int, list[dict]]:
    """同主语短句链：一句话的内容被拆成几个只有短句。

    与 `_local_frag` 的分工（两者都得留，口径不同）：
      · `_local_frag` 抓的是**极短**碎片（≤9 字，如"我手在抖。心在跳。"）；
      · 本函数抓的是**中等长度但主语重复**的链——"我弯腰把钱捡起来。
        我走到车旁。我抬手拍回车盖。"每句 10-20 字，`_local_frag` 判它不够短，
        但读者读起来就是"一句能写完的事拆成三句"。

    判据（三条件同时成立，避免误伤正常的短句节奏）：
      1. 句子是 `_chain_member`（非对话/无引号/句号结尾/非时间状语开头/≤上限）；
      2. 核心字 ≤ CHAIN_SENT_CHARS；
      3. 相邻句开头同一主语（我/他/她/你/咱/名字前两字），至少 CHAIN_MIN 句连排。
    标定：健康网文章节里"我+短句"连排 2 句极常见，3 句才开始有机械感，
    因此 CHAIN_MIN=3，且每节只取最长的那一条计分（不叠加，避免长节被系统性误伤）。
    """
    pairs = sents_with_dialog(text)
    best: list[str] = []
    run: list[str] = []
    subs: list[str] = []
    for s, in_dlg in pairs:
        if not _chain_member(s, in_dlg):
            run, subs = [], []
            continue
        core = re.sub(r"[。！？!?…；;]+$", "", s.strip())
        subj = _lead_subject(core)
        if subj:
            if subs and subj != subs[-1]:
                run, subs = [s.strip()], [subj]
            else:
                run.append(s.strip())
                subs.append(subj)
        elif run:
            # 承前省略主语是中文常态（"我弯腰把钱捡起来。走到车旁。"），
            # 不能因为这一句没主语就把链打断——那正是"一句话被拆开"的样子。
            run.append(s.strip())
        if len(run) > len(best):
            best = list(run)
    if len(best) >= CHAIN_MIN:
        return (len(best) - CHAIN_MIN + 1) * 4, [{
            "kind": "碎句·同主语短句链",
            "text": "".join(best)[:120],
            "note": f"连续 {len(best)} 句同一主语开头且都 ≤{CHAIN_SENT_CHARS} 字，"
                    f"可合并成 1-2 句",
        }]
    return 0, []


_SUBJECTS = ("我", "他", "她", "你", "咱", "俺", "俺们", "我们", "他们", "她们")


def _lead_subject(core: str) -> str:
    """取句子主语（开头的人称/姓名），取不到返回空串。"""
    for w in _SUBJECTS:
        if core.startswith(w):
            return w
    m = re.match(r"^([一-龥]{2,3})(?=[一-龥]{1,4}(?:了|着|的|把|被|说|走|站|看|听|想|拿|抬|转|推|拉|抱|问|答|点|笑|哭))", core)
    return m.group(1) if m else ""


# AA 叠词：一笔一笔 / 一层一层 / 一下一下 —— 复古文腔，网文里显得拖沓
_AA_PAT = re.compile(r"一([一-龥])一\1")
# 数量词后置：动宾后逗号再补数量（"点了香，三根"）
# 排除「他说，三根。」这类对话转述：逗号前一字是说话动词时不算倒装。
_POST_QTY = re.compile(
    r"[\u4e00-\u9fa5]{2,}(?<![\u8bf4\u95ee\u9053\u7b54\u558a\u53eb\u9a82])[，,]"
    r"[\u4e00\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u51e0\u534a]{1,2}"
    r"(?:\u6839|\u679a|\u5f20|\u5757|\u53ea|\u6761|\u53e3|\u628a|\u4ef6|\u4e2a|\u7247|\u7c92|\u5bf8|\u6b65|\u7897|\u676f|\u9053|\u5c42|\u8d34|\u6302|\u67f1)"
    r"(?=[。！？…]|$)")


def _local_order(text: str) -> tuple[int, list[dict]]:
    """语序类硬伤：AA 叠词罗列、数量词后置倒装。

    这两类不涉及内容对错，纯语序/节奏问题，确定性可判（不需要 LLM），
    因此放在这一层而不是交给主编——主编每轮只挑 3-5 条说，容易漏。
    """
    hits: list[dict] = []
    score = 0
    n_chars = len(re.sub(r"\s", "", text)) or 1
    aa = _AA_PAT.findall(text)
    if len(aa) >= 3:
        score += (len(aa) - 2) * 2
        hits.append({"kind": "语序·AA叠词罗列", "text": "、".join(
            m.group(0) for m in list(_AA_PAT.finditer(text))[:4]),
            "note": f"本节 {len(aa)} 处叠词修饰（全篇建议 ≤2 处），删掉不影响句子完整"})
    post = [m.group(0) for m in _POST_QTY.finditer(text)]
    if post:
        score += len(post) * 3
        hits.append({"kind": "语序·数量词后置", "text": "；".join(post[:3]),
                     "note": "数量词必须前置紧跟名词（「点了三根香」），不能拎到句尾"})
    return score, hits


def _local_redundancy(text: str, rules: dict) -> tuple[int, list[dict]]:
    """**节内**冗余检测：口癖密度 + 同段 n-gram 复读 + 段末总结句。

    全部按密度折算：写 1900 字的一节出现 2 个「缓缓」是文风，出现 6 个才是病。
    """
    from collections import Counter

    hits: list[dict] = []
    score = 0
    n_chars = len(re.sub(r"\s", "", text)) or 1
    w = n_chars / 1000.0
    # ① 黑名单口癖（与整篇同一份词表，这里按"本节密度"计分）
    blk: list[tuple[str, int]] = []
    n_blk = 0
    for rule in rules["blacklist"]:
        if rule["severity"] not in ("high", "mid"):
            continue
        found = re.findall(rule["pattern"], text)
        if found:
            n_blk += len(found)
            blk.append((rule["label"], len(found)))
    budget_blk = max(1.0, 2.0 * w)
    if n_blk > budget_blk:
        score += int(round((n_blk - budget_blk) * 2))
        blk.sort(key=lambda x: -x[1])
        hits.append({"kind": "口癖·密度超标",
                     "text": "、".join(f"{l}×{c}" for l, c in blk[:4]),
                     "note": f"本节 {n_blk} 处（每千字允许 2 处）"})
    # ② 同段 6-gram 复读（同一句话里的词反复出现 = 表述冗余）
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    rep_para = 0
    for p in paras:
        grams = re.findall(r"[\u4e00-\u9fa5]{6}", p)
        if len(grams) < 3:
            continue
        top, cnt = Counter(grams).most_common(1)[0]
        if cnt >= 3:
            rep_para += 1
            if len(hits) < 8:
                hits.append({"kind": "冗余·同段复读", "text": top,
                             "note": f"同一段里「{top}」出现 {cnt} 次"})
    if rep_para:
        score += min(rep_para, 3) * 2
    # ③ 段末总结句（"终于明白/这一刻/她知道，"——给读者上价值，AI 味最重的冗余）
    tails = re.findall(r"(终于明白|才明白|这一刻[^。]{0,10}(明白|清楚))", text)
    if len(tails) >= 2:
        score += (len(tails) - 1) * 3
        hits.append({"kind": "冗余·段末总结", "text": tails[0][0] if isinstance(tails[0], tuple) else str(tails[0]),
                     "note": f"本节 {len(tails)} 处段末点题"})
    return score, hits


def _local_detail(text: str) -> tuple[int, list[dict]]:
    """**节内**详略检测：超长无对话段 / 本节对话占比过低 / 连续无对话铺陈。"""
    hits: list[dict] = []
    score = 0
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if not paras:
        return 0, hits
    for p in paras:
        if len(p) > 200 and not langqa._DIALOG_MARK.search(p):
            score += 5
            hits.append({"kind": "详略·超长无对话段", "text": p[:26] + "…",
                         "note": f"{len(p)} 字一整段全是叙述"})
            break
    n_dialog = sum(1 for p in paras if langqa._DIALOG_MARK.search(p))
    ratio = n_dialog / len(paras)
    if len(re.sub(r"\s", "", text)) >= 800 and ratio < 0.08:
        score += 5
        hits.append({"kind": "详略·对话占比过低", "text": f"对话段 {n_dialog}/{len(paras)}",
                     "note": f"本节对话占比 {ratio:.0%}（<8%），全靠概述推进"})
    # 连续 ≥3 段既无对话也无引号 = 纯铺陈
    run = best = 0
    for p in paras:
        if langqa._DIALOG_MARK.search(p) or "：" in p or ":" in p:
            run = 0
        else:
            run += 1
            best = max(best, run)
    if best >= 4:
        score += 3
        hits.append({"kind": "详略·连续铺陈", "text": f"连续 {best} 段无对话",
                     "note": "该有对话推进的地方写成了叙述"})
    return score, hits


def defect_score(text: str, rules: dict | None = None) -> dict:
    """单节（或整稿）四类语言缺陷的确定性得分。

    返回 {dup, frag, euro, detail, score, ...}。
    分两层口径：
      - 整篇口径（dup_report / fragment_report）抓"整篇级"的病（复制粘贴、全篇节奏离群）
      - 节内口径（_local_*）抓"这一节"的病（局部碎句、口癖密度、详略失衡）
    两者相加。健康节标定目标 < SECTION_SCORE_MIN(6)。
    """
    rules = rules or langqa.load_rules()
    d = langqa.dup_report(text, rules)
    e = langqa.euro_report(text)
    dup = d["lost"] * 6 + (d["max_rep"] - 1 if d["max_rep"] > 1 else 0) * 10 + d["near_pairs"] * 5
    frag_l, frag_hits, long_l = _local_frag(text)
    redun_l, redun_hits = _local_redundancy(text, rules)
    det_l, det_hits = _local_detail(text)
    chain_l, chain_hits = _local_chain(text)
    order_l, order_hits = _local_order(text)
    euro = e["count"]
    return {
        "dup": dup,
        "frag": frag_l,
        "longsen": long_l,
        "euro": euro,
        "detail": det_l,
        "redundancy": redun_l,
        "chain": chain_l,
        "order": order_l,
        "score": dup + frag_l + long_l + euro + det_l + redun_l + chain_l + order_l,
        "near_pairs": d["near_pairs"],
        "max_rep": d["max_rep"],
        "hits": frag_hits + chain_hits + order_hits + redun_hits + det_hits + e["hits"],
    }


def _quote(s: str, n: int = 46) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def defect_brief(text: str, rules: dict | None = None) -> str:
    """把本节的具体病句逐条列出来，喂给改写提示词。

    为什么必须给"具体句子"而不是只给"病名"：只写"你这节有欧化病"，
    模型会礼貌地点头然后原文照抄；给出 3-5 句真实病句并指出病因，改中率显著提高。
    """
    rules = rules or langqa.load_rules()
    lines: list[str] = []

    # 1) 欧化句式：逐句定位

    for label, pat, _sev in _EURO_RULES:
        for m in list(re.finditer(pat, text))[:3]:
            s, e = m.start(), m.end()
            lo = max(0, s - 20)
            hi = min(len(text), e + 20)
            lines.append(f"- 欧化·{label}：「{_quote(text[lo:hi])}」")
            if len(lines) >= 6:
                break
        if len(lines) >= 6:
            break

    # 2) 碎句：极短碎片 + 同主语短句链（两种口径都要报，用户抱怨的是后者）
    sents = [s for s, _d in sents_with_dialog(text)]
    run: list[str] = []
    best: list[str] = []
    for s in sents:
        core = re.sub(r"[。！？!?…]+$", "", s).strip()
        if 0 < len(core) <= 9 and not _is_dialog_sent(s):
            run.append(s.strip())
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    if len(best) >= 3:
        lines.append(f"- 碎句：连续 {len(best)} 个短句没合并 → 「{_quote(''.join(best), 60)}」")
    _chain_sc, chain_hits = _local_chain(text)
    for h in chain_hits[:1]:
        # 摘录要给到整条链（120 字），只给前 60 字时模型往往只合并看到的这几句，
        # 后面还在链上的句子原样留着 —— 实测打磨后 chain 仍超投稿线就是这么来的。
        lines.append(f"- 碎句链（一句话被拆成几句）：「{_quote(h['text'], 110)}」"
                     f"→ {h['note']}；合并后主语只出现一次")

    # 2.5) 语序类：AA 叠词、数量词后置
    _order_sc, order_hits = _local_order(text)
    for h in order_hits[:2]:
        lines.append(f"- {h['kind']}：「{_quote(h['text'], 50)}」（{h['note']}）")

    # 3) 长句
    for s in sents:
        core = re.sub(r"[。！？!?…]+$", "", s).strip()
        if len(core) >= 46:
            lines.append(f"- 长句（{len(core)}字）：「{_quote(s, 60)}」需在「谁做了什么」的接缝处拆开")
            break

    # 4) 重复段
    d = langqa.dup_report(text, rules)
    for smp in d["samples"][:2]:
        lines.append(f"- 整段重复（{smp['times']} 遍）：「{_quote(smp['text'], 40)}」")
    for smp in d["near_samples"][:2]:
        lines.append(f"- 近似冗余：「{_quote(smp['a'], 36)}」与「{_quote(smp['b'], 36)}」说的是同一件事")

    # 5) 详略
    dt = langqa.detail_report(text, rules)
    for h in dt["hits"][:3]:
        lines.append(f"- 详略·{h['kind'].split('·')[-1]}：{h['text']}（{h['note']}）")

    # 6) 黑名单口癖（与 langqa 同一词表，这里只提示不改判定）
    for rule in rules["blacklist"]:
        if rule["severity"] not in ("high", "mid"):
            continue
        m = re.search(rule["pattern"], text)
        if m:
            lo = max(0, m.start() - 16)
            hi = min(len(text), m.end() + 16)
            lines.append(f"- 口癖·{rule['label']}：「{_quote(text[lo:hi])}」")
            if len(lines) >= 12:
                break

    if not lines:
        lines.append("- （本节无确定性可定位的病句；仍请按四条病自查，特别是详略与碎句合并）")
    return "\n".join(lines[:12])


_CRAFT: dict | None = None


def _craft_bank() -> dict:
    """加载语言技法范例库 data/craft.json（懒加载并缓存）。"""
    global _CRAFT
    if _CRAFT is None:
        p = data_path("craft.json")
        try:
            _CRAFT = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _CRAFT = {}
    return _CRAFT


_CRAFT_LABEL = {
    "frag": "碎句（一句话拆成几个短句）",
    "euro": "中文语序（欧化／翻译腔）",
    "redundancy": "重复冗余",
    "detail": "详略不当",
    "chain": "碎句链（一句话拆成几句）",
    "order": "语序（叠词罗列／数量词后置）",
    "dialog": "对白（把场面写成了转述）",
}


def craft_hint(sc: dict, n: int = 2, max_kinds: int = 2) -> str:
    """按本节实际检出的病种，取对应范例（病句→改后→操作说明）注入改写提示词。

    为什么按病种取而不是全量塞：lang_polish.md 已经把规则写全了，模型看规则会
    点头照抄；范例是"实物对照"。但把 24 条例句全塞进去会挤占本节原文的注意力，
    所以只取最严重的 2 类病、每类 2 条。
    """
    bank = _craft_bank()
    if not bank:
        return "（无）"
    kinds = []
    for k in ("frag", "chain", "order", "euro", "redundancy", "detail"):
        v = int(sc.get(k) or 0)
        if v > 0 and bank.get(k):
            kinds.append((v, k))
    if not kinds:
        return "（本节四类病均未检出，按提示词里的规则自查即可）"
    kinds.sort(reverse=True)
    out: list[str] = ["【同类病的改法范例（学其中做的操作，禁止照抄这些句子）】"]
    for _v, k in kinds[:max_kinds]:
        out.append(f"▍{_CRAFT_LABEL.get(k, k)}")
        for it in bank[k][:n]:
            out.append(f"  病：{it.get('bad', '')}")
            out.append(f"  改：{it.get('good', '')}")
            why = it.get("why", "")
            if why:
                out.append(f"  操作：{why}")
    return "\n".join(out)


def dedupe_paragraphs(text: str, min_len: int = 20, short_min_len: int = 12,
                      short_times: int = 3) -> tuple[str, int]:
    """确定性删除整段重复（同段 verbatim 出现多次，只留第一次）。

    为什么不交给 LLM：这是纯复制粘贴，删掉零风险、零 token。

    阈值来自实测（历史稿真实重复段长度）：20-26 字，**不是**长篇大段。
    最初设 40 字会把它们全漏掉（回归用例当场抓到 removed=0）。所以按两档处理：
      · ≥20 字且重复 ≥2 遍：删（这种长度的逐字重复不可能是修辞）；
      · ≥12 字且重复 ≥3 遍：删（短句复调最多两遍，三遍以上就是装配 bug，
        例如「「老子养你二十五年，拿你点钱天经地义！」」被贴了两次）。
    """
    from collections import Counter

    paras = [p for p in text.splitlines() if p.strip()]
    keys = [re.sub(r"[\s　]+", "", p) for p in paras]
    cnt = Counter(k for k in keys if k)
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for p, key in zip(paras, keys):
        n = cnt.get(key, 0)
        hit = (n >= 2 and len(key) >= min_len) or (n >= short_times and len(key) >= short_min_len)
        if hit:
            if key in seen:
                removed += 1
                continue
            seen.add(key)
        out.append(p)
    if not removed:
        return text, 0
    return "\n\n".join(out), removed


def _count_sec_heads(text: str) -> int:
    """数文本里的小节头个数（越界保护用：单节输出理应只有 1 个）。"""
    return sum(1 for ln in text.splitlines() if SEC_HEAD.match(ln.strip()))


def _names_in(bible: dict | None) -> list[str]:
    if not bible:
        return []
    out = []
    for c in bible.get("characters") or []:
        n = (c.get("name") or "").strip()
        if n and len(n) >= 1:
            out.append(n)
    return out


def _accept(old: str, new: str, old_score: dict, bible: dict | None,
            cfg: dict, rules: dict, focus: tuple[str, ...] = ()) -> tuple[bool, str, dict]:
    """单节改写复验：四条全过才采纳。返回 (ok, reason, new_score)。

    `focus` 非空时按"本轮点名要修的类别"判改善：这些类别的缺陷分合计必须严格下降，
    且总分不得上升。为什么需要这条分支：整稿投稿线卡的是 chain/detail 的**密度**，
    而 polish 的默认判据要求总分严格下降——模型把碎句链合并掉的同时常引入等量的
    新长句/铺陈，总分持平，于是明明修好的改写被整条回滚（e2e5 实测 4 改 4 回滚，
    chain 0.97 原地不动）。
    """
    new = new.strip()
    if not new:
        return False, "输出为空", {}
    w_old = count_words_zh(old)
    w_new = count_words_zh(new)
    if w_old and not (WORD_RATIO_LO <= w_new / w_old <= WORD_RATIO_HI):
        return False, f"字数越界 {w_new}/{w_old}（允许 ±12%）", {}
    ns = defect_score(new, rules)
    if focus:
        f_old = sum(int(old_score.get(k) or 0) for k in focus)
        f_new = sum(int(ns.get(k) or 0) for k in focus)
        if not (f_new < f_old and ns["score"] <= old_score["score"]):
            return False, (f"定向项未改善 {dict((k, old_score.get(k)) for k in focus)}"
                           f"→{dict((k, ns.get(k)) for k in focus)}"
                           f"（总分 {old_score['score']}→{ns['score']}）"), ns
    # 缺陷分必须严格下降：并列就不算改善（防止模型换汤不换药还占一次预算）
    elif ns["score"] >= old_score["score"]:
        return False, f"缺陷分未下降 {old_score['score']}→{ns['score']}", ns
    # 人名不许丢：改写最容易把配角写没了
    lost = [n for n in _names_in(bible) if n in old and n not in new]
    if lost:
        return False, "人物被写丢：" + "、".join(lost[:3]), ns
    # 禁用道具不许新增
    try:
        from .langqa import scan_banned

        b_old = scan_banned(old, cfg).get("hits", [])
        b_new = scan_banned(new, cfg).get("hits", [])
        if len(b_new) > len(b_old):
            return False, f"新增禁用道具 {len(b_old)}→{len(b_new)}", ns
    except Exception:  # noqa: BLE001 —— 道具表缺失不该阻断语言打磨
        pass
    return True, "ok", ns



def _kb_for(kbcard: str, project, section_text: str) -> str:
    """按小节内容重选知识卡条目，拿不到就退回整卡摘要。

    为什么要按节选：旧实现整篇打磨共用一张卡的同一段摘要，8 节反复吃同一批
    细节，本身就是"重复描写"的源头之一。这里用节内文本检索卡内条目，
    让每节拿到贴合自己的事实/场景。
    """
    if not kbcard:
        return "（无）"
    try:
        from . import knowledge as _kn

        concept = project.load_json("concept.json") or {}
        card = _kn.load_card(concept.get("direction", "") or "")
        if not card:
            return kbcard
        txt, _ = _kn.digest_for(card, query=section_text, budget=900)
        return txt or kbcard
    except Exception:  # noqa: BLE001 —— 知识卡缺失不该阻断语言打磨
        return kbcard


def polish(cfg: dict, llm, project, full: str, *, rounds: int = 2,
           style_sample: str = "", genre_guide: str = "", kbcard: str = "",
           log=print) -> tuple[str, dict]:
    """全稿语言打磨主入口。返回 (新全稿, 报告 dict)。

    报告结构：
      {"rounds": [...], "sections_rewritten": n, "sections_rejected": n,
       "deduped": n, "before": {...}, "after": {...}, "pass": bool}
    """
    from .pipeline import _split_full_sections  # 延迟导入，避免与 pipeline 循环依赖
    from .prompts import render_prompt

    rules = langqa.load_rules()
    bible = None
    try:
        bible = project.load_json("bible.json")
    except Exception:  # noqa: BLE001
        pass

    before = defect_score(full, rules)
    rep = {"rounds": [], "sections_rewritten": 0, "sections_rejected": 0,
           "deduped": 0, "before": before, "after": before, "pass": False}

    # 第一步：确定性去重（零成本，先做掉最刺眼的一类）
    full, n_rm = dedupe_paragraphs(full)
    if n_rm:
        rep["deduped"] = n_rm
        log(f"[lang] 确定性删除整段重复 {n_rm} 段")

    if before["score"] == 0 and n_rm == 0:
        rep["after"] = defect_score(full, rules)
        rep["pass"] = True
        return full, rep

    writer = None

    for rnd in range(1, max(1, rounds) + 1):
        secs = _split_full_sections(full)
        if not secs:
            log("[lang] 小节头识别失败，跳过逐节语言打磨")
            break
        scored = [(s, defect_score(s["text"], rules)) for s in secs]
        scored.sort(key=lambda x: -x[1]["score"])
        # 返修判据与投稿门槛同源（needs_polish），不再只看绝对总分：
        # 绝对阈值 8 会漏掉"某单项爆表但总分 5-7"的节——那是用户最直观看得见的问题。
        targets = [(s, sc) for s, sc in scored if needs_polish(s["text"], rules)]
        # 每轮改写上限：固定 4 节时，12 节的中短篇要 3 轮才轮完一遍，
        # 而默认只给 2 轮——后面几节永远排不上。改成"至少覆盖一半小节"。
        _cap = max(MAX_SECTIONS_PER_ROUND, (len(secs) + 1) // 2)
        targets = targets[:_cap]
        if not targets:
            log(f"[lang] 第{rnd}轮：无小节达到阈值 {SECTION_SCORE_MIN}，停止")
            break
        if writer is None:
            writer = _writer_for(cfg, llm)

        log(f"[lang] 第{rnd}轮：定向打磨 "
            + "、".join(f"{s['no']:02d}({sc['score']})" for s, sc in targets))
        changed = 0
        for sec, old_sc in targets:
            no = sec["no"]
            body_lines = sec["text"].splitlines()
            body = "\n".join(body_lines[1:]).strip() if len(body_lines) > 1 else sec["text"]
            sents = [x for x in (langqa._sent_list(body) or [body]) if x.strip()]
            sec_end = _quote(sents[-1], 40) if sents else body[-40:]
            nxt = next((s for s in secs if s["no"] == no + 1), None)
            if nxt:
                nb = "\n".join(nxt["text"].splitlines()[1:]).strip() or nxt["text"]
                m = re.search(r"[^。！？]*[。！？]", nb)
                next_start = _quote(m.group(0) if m else nb, 40)
            else:
                next_start = "（本节是最后一节）"
            try:
                sys_p, user_p = render_prompt(
                    "lang_polish",
                    NO2=f"{no:02d}",
                    SECTION_TEXT=sec["text"],
                    SECTION_END=sec_end,
                    NEXT_START=next_start,
                    DEFECT_BRIEF=defect_brief(sec["text"], rules),
                    CRAFT=craft_hint(old_sc),
                    STYLE_SAMPLE=style_sample or "（暂无范文，按中文自然语序写作）",
                    GENRE_GUIDE=genre_guide or "（无）",
                    KBCARD=_kb_for(kbcard, project, sec["text"]),
                )
                new_text = writer.chat(sys_p, user_p).strip()
            except Exception as e:  # noqa: BLE001
                log(f"[lang] 小节{no:02d} 改写失败（{type(e).__name__}: {e}），保留原文")
                rep["sections_rejected"] += 1
                continue
            # 越界保护：模型若多写了别的小节头，整节丢弃
            if _count_sec_heads(new_text) > 1:
                log(f"[lang] 小节{no:02d} 输出含多个小节头（越界），保留原文")
                rep["sections_rejected"] += 1
                continue
            hl = new_text.splitlines()
            if not (hl and SEC_HEAD.match(hl[0].strip())):
                new_text = f"{no:02d}\n\n{new_text}"
            ok, why, ns = _accept(sec["text"], new_text, old_sc, bible, cfg, rules)
            if ok:
                sec["text"] = new_text
                changed += 1
                rep["sections_rewritten"] += 1
                log(f"[lang] 小节{no:02d} 打磨 {old_sc['score']}→{ns['score']}（{count_words_zh(new_text)}字）")
            else:
                rep["sections_rejected"] += 1
                log(f"[lang] 小节{no:02d} 未采纳（{why}），保留原文")
        if changed:
            full = "\n\n".join(s["text"] for s in secs)
        rep["rounds"].append({"round": rnd, "targets": [s["no"] for s, _ in targets],
                              "changed": changed})
        if not changed:
            break

    after = defect_score(full, rules)
    rep["after"] = after
    rep["pass"] = after["score"] < before["score"] or n_rm > 0
    log(f"[lang] 语言打磨完成：缺陷分 {before['score']}→{after['score']}，"
        f"改写 {rep['sections_rewritten']} 节 / 回滚 {rep['sections_rejected']} 节 / 去重 {rep['deduped']} 段")
    return full, rep


_FAIL_KIND = {
    "缺陷总分": ("frag", "chain", "order", "euro", "redundancy", "detail"),
    "frag": ("frag",),
    "chain": ("chain",),
    "order": ("order",),
    "euro": ("euro",),
    "redundancy": ("redundancy",),
    "detail": ("detail",),
    "对话段占比": ("detail",),
}

_KIND_CMDS = {
    "chain": "碎句链：把「同一主语 + 连续 3 句以上短句」合并成一句话，主语只出现一次，"
             "句号改逗号，次要动作降级成「动词+着」的伴随成分。",
    "frag": "碎句：连续多个 ≤9 字碎片合并成一个完整句，只在真正的动作切换处断句。",
    "order": "语序：数量词一律前置紧跟名词（「点了三根香」），AA 叠词全节最多留 1 处。",
    "euro": "欧化：拆掉「的的不休」与被动式长定语，改回「谁做了什么」的中文主谓宾。",
    "redundancy": "冗余：同一信息只留最有力的一次表述，删掉段末点题句与口癖。",
    "detail": "详略：超过 200 字的纯叙述段必须拆开——插入一句对白或一个动作节拍；"
              "连续 3 段以上没有对话的地方，补一句人物说的话推动情节。",
}


# 一句话合并后的字数上限：中文叙事句 35-45 字最顺，超过 46 字就触发「长句该拆」。
CHAIN_MERGE_CAP = 42
# 单段字数上限：手机一屏约 150-180 字，超过 200 字的整段在番茄/七猫上就是
# 「翻不动」。这不是审美问题，是排版硬伤，也是「详略不当」最可见的形态。
PARA_MAX_CHARS = 180


# 时间/地点状语开头的句子虽然常省略主语，但它通常是新的叙事节拍，
# 合并进上一句会读成「我推开门，走到窗边，拉开窗帘，第二天他来了」——语义变形。
_TIME_LEAD = re.compile(
    r"^(?:今天|昨天|前天|明天|第二天|第三天|当天|那天|这时|那时|此刻|后来|随后|"
    r"下午|上午|早上|中午|晚上|半夜|傍晚|入夜|天亮|隔天|周末|春节|过年|"
    r"一个月后|半年后|十年后|那年|这年|当年)")


def _merge_chain_run(run: list[str]) -> list[str]:
    """把一条同主语短句链合并成尽量少的几句（确定性，不调模型）。

    只处理以「。」结尾（或无标点）的非对话短句；问号/叹号/引号一律不碰——
    「你去哪了？我找你半天。」这种合并会把疑问语气吞掉。
    """
    parts: list[str] = []
    for s in run:
        t = s.strip()
        core = re.sub(r"[。\s]+$", "", t)
        if not core:
            continue
        subj = _lead_subject(core)
        if parts and subj:
            core = core[len(subj):].lstrip("，,、 ")
            if not core:
                continue
        parts.append(core)
    if len(parts) <= 1:
        return [run[0].strip()] if run else []
    out: list[str] = []
    buf: list[str] = []
    for p in parts:
        if buf and sum(len(x) for x in buf) + len(p) + 1 > CHAIN_MERGE_CAP:
            out.append("，".join(buf) + "。")
            buf = [p]
        else:
            buf.append(p)
    if buf:
        out.append("，".join(buf) + "。")
    return out


def merge_chain_sentences(text: str) -> tuple[str, int]:
    """全稿确定性合并「一句话被拆成几个只有短句」的同主语短句链。

    为什么先做确定性合并而不是交给模型：
      · 这是**机械**改法（句号换逗号、删掉重复主语），规则唯一，不需要创作能力；
      · 让模型改整节时实测会引入等量新病（e2e5 节02：chain 8→4，但总分 11→21；
        整稿 converge 三轮 3 改 8 拒，缺陷密度反而 1.84 → 1.95）。
    返回 (新文本, 合并处数)。
    """
    out_lines: list[str] = []
    merged = 0
    lines = text.split("\n")
    buckets: list[list[str] | None] = []
    for line in lines:
        raw = line.strip()
        if not raw:
            buckets.append([])          # 空行不打断句链（检测器也不按段落切）
        elif SEC_HEAD.match(raw):
            buckets.append(None)        # 节头才是真边界
        else:
            buckets.append(langqa._sent_list(raw) or [raw])

    # 句子链**跨行**存在时最典型的样子就是「一句一段」：
    #   我推开门。
    #   我走到窗边。
    #   我拉开窗帘。
    # 检测器（`_local_chain`）在整节句子流上看，不认段落边界，所以合并也必须跨行，
    # 否则页面上明明还是一条短句链，体检却报 0（早期按行合并实测 chain 0.95→0.92）。
    segments: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    for li, b in enumerate(buckets):
        if b is None:
            if cur:
                segments.append(cur)
                cur = []
            continue
        for si in range(len(b)):
            cur.append((li, si))
    if cur:
        segments.append(cur)

    repl: dict[tuple[int, int], list[str]] = {}
    delete: set[tuple[int, int]] = set()

    for seg in segments:
        run: list[tuple[int, int]] = []
        subs: list[str] = []

        def _flush(run=run, subs=subs):
            nonlocal merged
            if len(run) >= CHAIN_MIN:
                sents = [buckets[li][si].strip() for li, si in run]
                got = _merge_chain_run(sents)
                if len(got) < len(sents):
                    merged += 1
                    repl[run[0]] = got
                    for pos in run[1:]:
                        delete.add(pos)
            run.clear()
            subs.clear()

        for pos in seg:
            li, si = pos
            s = buckets[li][si].strip()
            if not _chain_member(s):
                _flush()
                continue
            core = re.sub(r"[。！？!?…；;]+$", "", s)
            subj = _lead_subject(core)
            if subj:
                if subs and subj != subs[-1]:
                    _flush()
                run.append(pos)
                subs.append(subj)
            elif run:
                # 承前省略主语：延续同一句链（时间状语开头的句子已被
                # `_chain_member` 挡在外面，这里不必再判一遍）
                run.append(pos)
            else:
                _flush()
        _flush()

    if not merged:
        return text, 0
    for li, b in enumerate(buckets):
        if b is None:
            out_lines.append(lines[li])
            continue
        if not b:                       # 空行原样保留，避免段落全被压平
            out_lines.append("")
            continue
        parts: list[str] = []
        for si in range(len(b)):
            pos = (li, si)
            if pos in delete:
                continue
            if pos in repl:
                parts.extend(repl[pos])
            else:
                parts.append(b[si].strip())
        if parts:      # 句子被搬空的行直接消失，等价于把那一句并回上一段
            out_lines.append("".join(parts))
    return "\n".join(out_lines), merged


def split_long_paragraphs(text: str, max_len: int = PARA_MAX_CHARS) -> tuple[str, int]:
    """把超长段落按句子边界切开（确定性）。返回 (新文本, 切开的段数)。

    只切、不加不减内容——「≥200 字一整段纯叙述」是移动端阅读硬伤，切段零风险；
    真正的「缺水少对白」交给 converge 让模型补，两步分开做：即使模型那步失败，
    排版问题也已经修掉了。
    """
    out: list[str] = []
    n = 0
    for line in text.split("\n"):
        raw = line.strip()
        if not raw or len(raw) <= max_len or SEC_HEAD.match(raw):
            out.append(line)
            continue
        sents = langqa._sent_list(raw) or [raw]
        chunks: list[str] = []
        buf = ""
        depth = 0
        for s in sents:
            # 台词会堵住切点：`……问我，「担保人是谁？」` 一旦进了引号就只能在
            # 引号闭合后才敢切，长段落于是一直憋到 300+ 字。所以在**进入引号之前**
            # 先把已经够长的前半段结掉。
            st = s.strip()
            opens = st.startswith(("「", "“", "『")) or "「" in st[:4] or "“" in st[:4]
            if (buf and depth <= 0
                    and (len(buf) + len(s) > max_len
                         or (opens and len(buf) >= int(max_len * 0.7)))):
                chunks.append(buf)
                buf = ""
            buf += s
            # 直接在句子边界切会把一句台词劈成两半（实测切出过
            # `…我问，「担保人是谁？` / `」电话那头沉默…`）——引号没闭合就不许切。
            depth += (s.count("「") + s.count("“") + s.count("『")
                      - s.count("」") - s.count("”") - s.count("』"))
        if buf:
            chunks.append(buf)
        # 尾块过短就并回上一块，避免切出「一句话成段」的新毛病
        # —— 但并回去不能超过上限，否则又把长段拼了回来（实测 185+36→221 字，
        # 详略检测照样命中）。
        if (len(chunks) >= 2 and len(chunks[-1]) < 40
                and len(chunks[-2]) + len(chunks[-1]) <= max_len):
            chunks[-2] += chunks[-1]
            chunks.pop()
        if len(chunks) <= 1:
            out.append(line)
            continue
        n += 1
        out.extend(chunks)
    return "\n".join(out), n


def dedupe_enums(text: str) -> tuple[str, int]:
    """删掉顿号罗列里的重复项（"一锅一锅、一勺一勺、一锅一锅、一锅一锅"→两项）。

    这是纯冗余：同一件事在一句里列了两遍。机械可删，不需要模型判断。
    """
    n = 0
    out_lines: list[str] = []
    for line in text.split("\n"):
        if "、" not in line:
            out_lines.append(line)
            continue
        # 只在同一标点段内去重，绝不跨句搬动内容
        chunks = re.split(r"([，。！？；：\n])", line)
        for i in range(0, len(chunks), 2):
            seg = chunks[i]
            if "、" not in seg:
                continue
            items = seg.split("、")
            seen: list[str] = []
            for it in items:
                k = it.strip()
                if k and k not in seen:
                    seen.append(k)
            if len(seen) < len(items):
                n += len(items) - len(seen)
                chunks[i] = "、".join(seen)
        out_lines.append("".join(chunks))
    return "\n".join(out_lines), n


# 长句可切点：逗号后面接这些词，说明是一个能独立成句的新分句
_SUBJ_SPLIT = re.compile(
    r"^(?:我|他|她|你|咱|俺|我们|他们|她们|你们|大家|自己|"
    r"那天|第二天|第三天|这时|那时|后来|随后|半晌|半天|很久|"
    r"晚上|下午|早上|中午|半夜|第二天一早)")


def split_long_sentences(text: str, max_len: int | None = None,
                         per_k: float | None = None) -> tuple[str, int]:
    """把超长句在「逗号 + 新主语」处断成两句（确定性，按千字预算限次）。

    `_local_frag` 里「长句·该拆」也是计分的（每千字只允许 2 句 >46 字）：
    60 字一句、顿号逗号一路铺到底，手机上就是一堵墙。断点必须落在
    「，我…」「，他…」「，第二天…」这种新分句上——那本来就是句子边界。
    """
    max_len = LONG_SENT_CHARS if max_len is None else max_len
    per_k = LONG_BUDGET_PER_K if per_k is None else per_k
    total = len(re.sub(r"\s", "", text))
    budget = max(1, int(total / 1000.0 * per_k))

    def _long_count(t: str) -> int:
        # 口径必须与 `_local_frag` 完全一致：都在**整段文本**的句子流上数，
        # 否则会出现「工具认为已经切够、检测器仍报超标」（实测 20 vs 16）。
        return sum(1 for s in langqa._sent_list(t) if _core_len(s) > max_len)

    def _pick_cut(s: str) -> int:
        depth = 0
        cands: list[int] = []
        for k, ch in enumerate(s):
            if ch in "「“『":
                depth += 1
            elif ch in "」”』":
                depth -= 1
            elif ch == "，" and depth == 0:
                rest = s[k + 1:]
                if _SUBJ_SPLIT.match(rest) and 12 <= k <= len(s) - 14:
                    cands.append(k)
        if not cands:
            return -1
        half = len(s) / 2.0
        return min(cands, key=lambda k: abs(k - half))

    n = 0
    for _ in range(budget * 3 + 40):
        if _long_count(text) <= budget:
            break
        done = False
        for s in langqa._sent_list(text):
            if _core_len(s) <= max_len:
                continue
            cut = _pick_cut(s)
            if cut < 0:
                continue
            text = text.replace(s, s[:cut] + "。" + s[cut + 1:], 1)
            n += 1
            done = True
            break
        if not done:
            break
    return text, n


def vary_aa_repeats(text: str, keep: int = 2) -> tuple[str, int]:
    """同一处「一X一X」叠词全篇出现过多时，从第 3 次起改写成「一X接一X」。

    这是写手口癖，不是作者风格：实测一篇 8 千字里「一锅一锅卤出来」出现 5 次，
    分布在 5 个段落。原样保留读起来像复读；纯删又丢信息。「一X接一X」同义、
    不新增信息、也不再触发叠词判据——是最小代价的改动。
    """
    hits = list(_AA_PAT.finditer(text))
    if len(hits) <= keep:
        return text, 0
    out: list[str] = []
    last = 0
    n = 0
    for i, m in enumerate(hits):
        if i < keep:
            continue
        x = m.group(1)
        out.append(text[last:m.start()])
        out.append(f"一{x}接一{x}")
        last = m.end()
        n += 1
    out.append(text[last:])
    return "".join(out), n


def deterministic_repair(text: str, log=print) -> tuple[str, dict]:
    """零 token 的语言返修前置：去重 → 合并碎句链 → 切超长段。

    顺序有讲究：先合并（会改变句子边界），再去重（合并后可能出现新的重复段），
    最后切段（把合并后变长的段落按 180 字重新排版）。
    """
    rep: dict = {}
    text, n_rm = dedupe_paragraphs(text)
    rep["deduped_paras"] = n_rm
    text, n_en = dedupe_enums(text)
    rep["deduped_enums"] = n_en
    text, n_aa = vary_aa_repeats(text)
    rep["varied_aa"] = n_aa
    # 合并要迭代：把「我起身。我拿包。」并成一句后，新句子可能仍然 ≤22 字，
    # 于是它和下一句又构成一条新链——单趟合并只降一半（实测 chain 0.95→0.92）。
    n_mg = 0
    for _ in range(5):
        text, k = merge_chain_sentences(text)
        n_mg += k
        if not k:
            break
    rep["merged_chains"] = n_mg
    text, n_ls = split_long_sentences(text)
    rep["split_long_sents"] = n_ls
    text, n_sp = split_long_paragraphs(text)
    rep["split_paras"] = n_sp
    if any(rep.values()):
        log(f"[repair] 确定性返修：合并碎句链 {n_mg} 处 / 切超长句 {n_ls} 句 / "
            f"切超长段 {n_sp} 段 / 删重复段 {n_rm} 段 / 去罗列重复 {n_en} 项 / "
            f"改叠词口癖 {n_aa} 处（零 token）")
    return text, rep


def _writer_for(cfg: dict, llm):
    """取写手角色 LLM；角色配置残缺时回落到传入的 llm，而不是崩掉整轮打磨。

    线上 cfg 有 llm_roles.writer，永远不会回落到这里；但离线工具/测试只给
    {"review": {}}，角色路由会 KeyError('llm') —— 那是环境差异，不该让
    「语言返修」整段功能在无配置环境下直接不可用。
    """
    try:
        from .llm import resolve_llm

        return resolve_llm(cfg, llm, "writer")
    except Exception:  # noqa: BLE001
        return llm


def _fail_keys(fails: list[str]) -> tuple[str, ...]:
    """把 language_fails 的中文/英文条目映射成要修的缺陷类别（保持稳定顺序）。"""
    keys: list[str] = []
    for f in fails:
        for label, ks in _FAIL_KIND.items():
            if f.startswith(label):
                for k in ks:
                    if k not in keys:
                        keys.append(k)
                break
    return tuple(keys)


def converge(cfg: dict, llm, project, full: str, *, max_rounds: int = 3,
             per_round: int = 4, style_sample: str = "",
             log=print) -> tuple[str, dict]:
    """投稿线收敛：按**整稿**卡住的项目反复定向返修，直到通过或预算用尽。

    与 `polish()` 的分工：
      · polish 判「这一节自身有没有病」（节内相对口径，单节看不出 0.97/千字 这种
        整稿密度超标）；
      · converge 判「整稿离投稿线还差哪几项」（MARKET_TARGET 绝对口径），
        并且只修那几项、只改密度最高的几节。
    e2e5 实测：polish 两轮 4 改 4 回滚，chain 0.97 / detail 0.97 原地不动，
    整稿永远停在「C 不可投」；缺的就是这一层按绝对门槛收敛的循环。
    """
    from .pipeline import _split_full_sections
    from .prompts import render_prompt

    rules = langqa.load_rules()
    bible = None
    try:
        bible = project.load_json("bible.json")
    except Exception:  # noqa: BLE001
        pass
    fails0 = language_fails(full, rules)
    rep = {"before": fails0, "after": fails0, "rounds": [], "rewritten": 0,
           "rejected": 0, "pass": not fails0}
    if not fails0:
        return full, rep

    writer = None
    for rnd in range(1, max(1, max_rounds) + 1):
        fails = language_fails(full, rules)
        if not fails:
            break
        keys = _fail_keys(fails)
        secs = _split_full_sections(full)
        if not secs:
            log("[converge] 小节头识别失败，停止")
            break
        cand = []
        for s in secs:
            sc = defect_score(s["text"], rules)
            n = len(re.sub(r"\s", "", s["text"])) or 1
            dens = sum(int(sc.get(k) or 0) for k in keys) / (n / 1000.0)
            if dens > 0:
                cand.append((dens, s, sc))
        cand.sort(key=lambda x: -x[0])
        targets = cand[:max(1, per_round)]
        if not targets:
            break
        if writer is None:
            writer = _writer_for(cfg, llm)
        todo = "\n".join(f"- {_KIND_CMDS[k]}" for k in keys if k in _KIND_CMDS)
        log(f"[converge] 第{rnd}轮 卡住项 {fails} → 修 "
            + "、".join(f"{s['no']:02d}({d:.1f})" for d, s, _ in targets))
        changed = 0
        snap = full
        snap_sc = defect_score(full, rules)
        for _dens, sec, old_sc in targets:
            no = sec["no"]
            body_lines = sec["text"].splitlines()
            body = "\n".join(body_lines[1:]).strip() if len(body_lines) > 1 else sec["text"]
            sents = [x for x in (langqa._sent_list(body) or [body]) if x.strip()]
            nxt = next((s for s in secs if s["no"] == no + 1), None)
            if nxt:
                nb = "\n".join(nxt["text"].splitlines()[1:]).strip() or nxt["text"]
                m = re.search(r"[^。！？]*[。！？]", nb)
                next_start = _quote(m.group(0) if m else nb, 40)
            else:
                next_start = "（本节是最后一节）"
            try:
                sys_p, user_p = render_prompt(
                    "lang_polish",
                    NO2=f"{no:02d}",
                    SECTION_TEXT=sec["text"],
                    SECTION_END=_quote(sents[-1], 40) if sents else body[-40:],
                    NEXT_START=next_start,
                    DEFECT_BRIEF=defect_brief(sec["text"], rules),
                    CRAFT=craft_hint(old_sc),
                    STYLE_SAMPLE=style_sample or "（暂无范文，按中文自然语序写作）",
                    GENRE_GUIDE="（无）",
                    KBCARD="（无）",
                )
                user_p += ("\n\n【本轮唯一任务】这一节必须把下列整稿超标项压下去，"
                           "其余一律不动（情节、人物、对白内容都不许改）：\n" + todo)
                new_text = writer.chat(sys_p, user_p).strip()
            except Exception as e:  # noqa: BLE001
                rep["rejected"] += 1
                log(f"[converge] 小节{no:02d} 改写失败（{type(e).__name__}: {e}）")
                continue
            if _count_sec_heads(new_text) > 1:
                rep["rejected"] += 1
                continue
            hl = new_text.splitlines()
            if not (hl and SEC_HEAD.match(hl[0].strip())):
                new_text = f"{no:02d}\n\n{new_text}"
            ok, why, ns = _accept(sec["text"], new_text, old_sc, bible, cfg, rules, focus=keys)
            if ok:
                sec["text"] = new_text
                changed += 1
                rep["rewritten"] += 1
                log(f"[converge] 小节{no:02d} {keys} {old_sc['score']}→{ns['score']}"
                    f"（{count_words_zh(new_text)}字）")
            else:
                rep["rejected"] += 1
                log(f"[converge] 小节{no:02d} 未采纳（{why}）")
        if changed:
            full = "\n\n".join(s["text"] for s in secs)
            # 轮级兜底：单节复验只能保证「这一节点名项降了」，降不降得住**整稿密度**
            # 是另一回事。e2e5 实测 converge 三轮采纳 3 节、拒绝 8 节，整稿缺陷密度
            # 反而 1.84→1.95——被采纳的改写把病搬去了别的类别。整轮没让全局变好
            # 就整轮回滚，宁可不动也不许越修越差。
            now_sc = defect_score(full, rules)
            if int(now_sc["score"]) >= int(snap_sc["score"]):
                full = snap
                rep["reverted_rounds"] = rep.get("reverted_rounds", 0) + 1
                log(f"[converge] 第{rnd}轮整稿缺陷分未下降"
                    f"（{snap_sc['score']}→{now_sc['score']}），整轮回滚")
                changed = 0
        rep["rounds"].append({"round": rnd, "keys": list(keys),
                              "targets": [s["no"] for _, s, _ in targets],
                              "changed": changed})
        if not changed:
            break

    rep["after"] = language_fails(full, rules)
    rep["pass"] = not rep["after"]
    log(f"[converge] 收敛完成：{rep['before']} → {rep['after'] or '全部达标'}")
    return full, rep


_BAL_SYS = (
    "你是短篇网文的返修编辑。任务只有一件事：调整**某一节的篇幅**，让全篇各节长度均衡。"
    "禁止改情节走向、禁止增删事件、禁止改人物关系、禁止新增禁用道具。"
    "只输出该节正文（第一行是原有小节编号，例如 03），不要任何解释。"
)


_DIA_SYS = (
    "你是短篇网文的返修编辑，只做一件事：把**叙述**改成**当场对峙**。\n"
    "规则：① 不许改情节走向、不许加新人物、不许换地点；② 把最能冲突的那一段改写成"
    "至少两轮来回的对白，每句话都要带刺、能听见语气；③ 保留原文所有信息点"
    "（金额、时间、物件、因果）；④ 叙述句只留必要的过场，其余全部变成人物说出口的话；"
    "⑤ 不写「他说」「她问」堆砌，用动作代替说话标签；⑥ 只输出该节正文，第一行是原有小节编号。"
)


def _long_paras(text: str, limit: int = 200) -> int:
    """本节里「一整段 >limit 字且没有任何对白」的段数（详略硬伤）。"""
    n = 0
    for para in (x.strip() for x in text.split("\n")):
        if len(para) > limit and not langqa._DIALOG_MARK.search(para):
            n += 1
    return n


def dialog_raise(cfg: dict, llm, project, full: str, *, min_ratio: float | None = None,
                 max_edits: int = 3, style_sample: str = "", log=print) -> tuple[str, dict]:
    """交付前补对白：把「对话不足」的节改成场面化对峙。

    为什么需要（2026-10-26 古言实测）：章级对白门按章放行后，终审整体重写会把对白改成
    概述（全稿 0.233 < 0.25），同时制造「>200 字无对话段」，于是同一篇稿子同时踩
    `C节奏/对话段占比`、`C节奏/超长无对话段`、`B语言/详略/千字` 三条线。三条是同一个
    动作能修的，所以合成一个门，别再各修各的。
    """
    from .pipeline import _split_full_sections

    rep = {"enabled": True, "ratio_before": None, "ratio_after": None,
           "edits": [], "rejected": 0}
    limit = float(MARKET_TARGET["dialog_ratio_min"] if min_ratio is None else min_ratio)
    secs = _split_full_sections(full)
    if len(secs) < 2:
        rep["skipped"] = "节数不足 2，不判对白"
        return full, rep
    ratio = dialog_ratio(full)
    rep["ratio_before"] = round(ratio, 3)
    if ratio >= limit:
        rep["ratio_after"] = round(ratio, 3)
        return full, rep

    floor = int((cfg.get("review") or {}).get("word_floor", 8000) or 0)
    ceil_w = int((cfg.get("review") or {}).get("word_ceiling", 12000) or 0)
    try:
        bible = project.load_json("bible.json")
    except Exception:  # noqa: BLE001
        bible = None
    done = 0

    def _edit(i: int) -> bool:
        nonlocal ratio, done
        sec = secs[i]
        no = int(sec["no"])
        old = sec["text"]
        w0 = count_words_zh(old)
        r0 = dialog_ratio(old)
        lp0 = _long_paras(old)
        user_p = (
            f"【本节正文】\n{old}\n\n"
            f"【要做的调整】\n本节对白占比 {r0:.0%}（全篇要求 ≥{limit:.0%}）。"
            f"请把节内最紧张的一次对话写成**四到六句来回**的当场对峙，"
            f"原来用叙述交代的关键信息，改由人物说出口。字数保持 {int(w0 * 0.92)}—"
            f"{int(w0 * 1.10)} 字。\n\n"
            f"【硬约束】输出以「{no:02d}」单独一行开头；不得出现小标题；"
            f"不得新增人物姓名；人物原有的姓名与称呼保持一致。"
        )
        if lp0:
            user_p += f"\n\n【附加要求】本节有 {lp0} 段超过 200 字且通篇没有对白，一并改掉。"
        if style_sample:
            user_p += f"\n\n【语言风格参考】\n{style_sample[:600]}"
        try:
            new_text = llm.chat(_DIA_SYS, user_p).strip()
        except Exception as e:  # noqa: BLE001
            rep["edits"].append({"no": no, "ok": False, "why": f"{type(e).__name__}: {e}"})
            rep["rejected"] += 1
            return False
        if not new_text:
            rep["edits"].append({"no": no, "ok": False, "why": "输出为空"})
            rep["rejected"] += 1
            return False
        if _count_sec_heads(new_text) > 1:
            rep["edits"].append({"no": no, "ok": False, "why": "输出含多个小节头"})
            rep["rejected"] += 1
            return False
        hl = new_text.splitlines()
        if not (hl and SEC_HEAD.match(hl[0].strip())):
            new_text = f"{no:02d}\n\n{new_text}"
        w1 = count_words_zh(new_text)
        if w0 and not (0.88 <= w1 / w0 <= 1.18):
            rep["edits"].append({"no": no, "ok": False, "why": f"字数越界 {w0}→{w1}"})
            rep["rejected"] += 1
            return False
        r1 = dialog_ratio(new_text)
        if r1 <= r0 + 0.02:
            rep["edits"].append({"no": no, "ok": False,
                                 "why": f"对白没加上 {r0:.2f}→{r1:.2f}"})
            rep["rejected"] += 1
            return False
        lp1 = _long_paras(new_text)
        if lp1 > lp0:
            rep["edits"].append({"no": no, "ok": False,
                                 "why": f"无对话长段变多 {lp0}→{lp1}"})
            rep["rejected"] += 1
            return False
        lost = [n for n in _names_in(bible) if n in old and n not in new_text]
        if lost:
            rep["edits"].append({"no": no, "ok": False, "why": "人物被写丢：" + "、".join(lost[:3])})
            rep["rejected"] += 1
            return False
        trial = list(secs)
        trial[i] = dict(sec, text=new_text)
        total1 = count_words_zh("\n\n".join(x.get("text", "") for x in trial))
        if ceil_w and total1 > ceil_w:
            rep["edits"].append({"no": no, "ok": False, "why": f"超出平台上限 {total1}"})
            rep["rejected"] += 1
            return False
        r_global = dialog_ratio("\n\n".join(x.get("text", "") for x in trial))
        if r_global <= ratio:
            rep["edits"].append({"no": no, "ok": False,
                                 "why": f"全篇对白占比未升 {ratio:.3f}→{r_global:.3f}"})
            rep["rejected"] += 1
            return False
        secs[:] = trial
        ratio = r_global
        done += 1
        rep["edits"].append({"no": no, "ok": True, "from": round(r0, 3), "to": round(r1, 3),
                             "words": [w0, w1]})
        log(f"[dialog] 小节{no:02d} 对白 {r0:.0%}→{r1:.0%}，全篇 {rep['ratio_before']}→{ratio:.3f}")
        return True

    for _round in range(max_edits + 1):
        if done >= max_edits or ratio >= limit:
            break
        ranked = sorted(range(len(secs)),
                        key=lambda k: (dialog_ratio(secs[k]["text"]), -_long_paras(secs[k]["text"])))
        progressed = False
        for i in ranked:
            if done >= max_edits or ratio >= limit:
                break
            if count_words_zh(secs[i]["text"]) < 300:
                continue
            if _edit(i):
                progressed = True
                break
        if not progressed:
            break
    rep["ratio_after"] = round(ratio, 3)
    new_full = "\n\n".join(s.get("text", "") for s in secs)
    total = count_words_zh(new_full)
    if floor and total < floor:
        rep["reverted"] = f"补对白后字数 {total} 跌破下限 {floor}，放弃"
        rep["ratio_after"] = rep["ratio_before"]
        return full, rep
    return new_full, rep


def section_lengths(full: str) -> list[tuple[int, int]]:
    """返回 [(节号, 字数), ...]；无法切节时返回空列表。"""
    from .pipeline import _split_full_sections

    try:
        secs = _split_full_sections(full)
    except Exception:  # noqa: BLE001
        return []
    return [(int(s["no"]), count_words_zh(s.get("text", ""))) for s in secs]


def balance(cfg: dict, llm, project, full: str, *, max_ratio: float | None = None,
            max_edits: int = 4, style_sample: str = "", log=print) -> tuple[str, dict]:
    """节长均衡：把「最长节过长 / 最短节过短」的极端节各修一次。

    为什么单独做：四类语言病检测的是句内/段内问题，而「详略不当」在读者眼里
    最直观的表现是**某节特别水、某节特别赶**——这是全篇层面的结构病，
    语言打磨的小节级返修看不见它（每节单看都没有缺陷）。
    判据与投稿体检同源：MARKET_TARGET["section_ratio_max"]。
    """
    from .pipeline import _split_full_sections

    rep = {"enabled": True, "ratio_before": None, "ratio_after": None,
           "edits": [], "rejected": 0}
    limit = float(MARKET_TARGET["section_ratio_max"] if max_ratio is None else max_ratio)
    secs = _split_full_sections(full)
    if len(secs) < 3:
        rep["skipped"] = "节数不足 3，不判均衡"
        return full, rep
    ws = [count_words_zh(s.get("text", "")) for s in secs]
    if min(ws) <= 0:
        rep["skipped"] = "存在空节"
        return full, rep
    ratio = max(ws) / min(ws)
    rep["ratio_before"] = round(ratio, 2)
    if ratio <= limit:
        rep["ratio_after"] = round(ratio, 2)
        return full, rep
    mean = sum(ws) / len(ws)
    total0 = count_words_zh(full)
    # 字数下限是硬约束：均衡只准在floor之上做，压过头等于用"结构好看了"换"字数不达标"。
    floor = int((cfg.get("review") or {}).get("word_floor", 8000) or 0)
    try:
        bible = project.load_json("bible.json")
    except Exception:  # noqa: BLE001
        bible = None
    done = 0

    def _edit(i: int, target: int, long_side: bool) -> bool:
        """改一节，成功返回 True（就地更新 secs/ws/ratio/done）。"""
        nonlocal ratio, done
        sec = secs[i]
        no = int(sec["no"])
        w = ws[i]
        target = max(int(target), 200)
        if long_side:
            todo = (f"把这一节从 {w} 字压到 {target} 字左右。优先删：与主线无关的铺陈、"
                    f"同一信息的重复表述、可以被一句带过的动作分解。保留全部事件与对白要点。")
        else:
            todo = (f"把这一节从 {w} 字补到 {target} 字左右。只准用具体的东西补：一个动作、"
                    f"一句对白、一个可验证的细节（金额/时间/地点/物件）。禁止抒情、禁止比喻、"
                    f"禁止内心独白式感慨、禁止重复已说过的信息。")
        user_p = (f"【本节正文】\n{sec['text']}\n\n【要做的调整】\n{todo}\n\n"
                  f"【硬约束】输出仍以「{no:02d}」单独一行开头；字数 {int(target * 0.85)}—"
                  f"{int(target * 1.15)} 字；人物姓名保持原样。")
        if style_sample:
            user_p += f"\n\n【语言风格参考】\n{style_sample[:600]}"
        try:
            new_text = llm.chat(_BAL_SYS, user_p).strip()
        except Exception as e:  # noqa: BLE001
            rep["edits"].append({"no": no, "ok": False, "why": f"{type(e).__name__}: {e}"})
            rep["rejected"] += 1
            return False
        if _count_sec_heads(new_text) > 1:
            rep["edits"].append({"no": no, "ok": False, "why": "输出含多个小节头"})
            rep["rejected"] += 1
            return False
        hl = new_text.splitlines()
        if not (hl and SEC_HEAD.match(hl[0].strip())):
            new_text = f"{no:02d}\n\n{new_text}"
        nw = count_words_zh(new_text)
        if long_side and nw >= w * 0.92:
            rep["edits"].append({"no": no, "ok": False, "why": f"未压下来 {w}→{nw}"})
            rep["rejected"] += 1
            return False
        if (not long_side) and nw <= w * 1.08:
            rep["edits"].append({"no": no, "ok": False, "why": f"未补上去 {w}→{nw}"})
            rep["rejected"] += 1
            return False
        if (not long_side) and nw > target * 1.4:
            # 补字数最容易变成灌水：模型经常一补就超目标 40% 以上，宁可不要
            rep["edits"].append({"no": no, "ok": False, "why": f"补过头 {w}→{nw}（目标{target}）"})
            rep["rejected"] += 1
            return False
        lost = [n for n in _names_in(bible) if n in sec["text"] and n not in new_text]
        if lost:
            rep["edits"].append({"no": no, "ok": False, "why": "人物被写丢：" + "、".join(lost[:3])})
            rep["rejected"] += 1
            return False
        trial = list(secs)
        trial[i] = dict(sec, text=new_text)
        tw = [count_words_zh(s.get("text", "")) for s in trial]
        # 判据用「离均值的偏差总和」而不是极差本身：
        # 补短节时，若最短的几节一样长，补其中一节不会改变 min，极差因此不动，
        # 但它确实在向均值靠拢（偏差∑下降）。用极差当判据会把这类正确动作全拒掉。
        dev0 = sum(abs(x - mean) for x in ws)
        dev1 = sum(abs(x - mean) for x in tw)
        if dev1 >= dev0:
            rep["edits"].append({"no": no, "ok": False, "why": f"偏离均值未改善 {dev0}→{dev1}"})
            rep["rejected"] += 1
            return False
        secs[:] = trial
        ws[:] = tw
        ratio = max(tw) / min(tw)
        done += 1
        rep["edits"].append({"no": no, "ok": True, "from": w, "to": nw})
        log(f"[balance] 小节{no:02d} {w}→{nw} 字，节长极差 {rep['ratio_before']}→{ratio:.2f}")
        return True

    # 每轮只改一节、改完重新算预算与偏差：压缩长节可能被字数下限挡住，
    # 得先补短节把总量垫起来才有压缩余量，所以不能一次性把候选排完就收工。
    # 轮数上限 = max_edits + 2，保证「改不动」时最多空转两轮就退出（不会死循环）。
    ceil_w = int((cfg.get("review") or {}).get("word_ceiling", 12000) or 0)
    for _round in range(max_edits + 2):
        if done >= max_edits or ratio <= limit:
            break
        progressed = False
        for i in sorted(range(len(ws)), key=lambda k: -abs(ws[k] - mean)):
            if done >= max_edits or ratio <= limit:
                break
            w = ws[i]
            # 候选 = 当前最长或最短的节，且偏离均值 >15%。
            # 不用固定的 [0.7,1.35]×均值带筛选：压完长节后均值会下移，
            # 原本"明显短"的节就落回带内、再也补不了，极差会卡在 2.0 边上。
            if not (w == min(ws) or w == max(ws)):
                continue
            if mean and abs(w - mean) / mean <= 0.15:
                continue
            total_now = sum(ws)
            if w > mean:
                # 压缩预算：压完不能把全篇压到下限以下，只剩一点余量就别动长节
                budget = max(0, total_now - floor - 60) if floor else (w - int(mean * 0.9))
                target = max(int(mean * 0.9), w - budget)
                if target >= w * 0.98:
                    continue      # 无压缩余量，改由"补短节"垫高总量
                if _edit(i, target, True):
                    progressed = True
                    break
            else:
                # 补短节：补到均值即可，且不能越过平台上限
                budget = max(0, ceil_w - total_now) if ceil_w else 10 ** 9
                if budget <= 0:
                    continue
                target = min(int(mean), w + budget)
                if target <= w * 1.08:
                    continue
                if _edit(i, target, False):
                    progressed = True
                    break
        if not progressed:
            break
    rep["ratio_after"] = round(ratio, 2)
    new_full = "\n\n".join(s.get("text", "") for s in secs)
    total1 = count_words_zh(new_full)
    # 字数守卫：均衡允许加字（补短节）也允许减字（压长节），但结果必须落在
    # 平台的 [floor, ceiling] 区间内——这是投稿硬线，比"漂移百分比"更有意义。
    ceil_w = int((cfg.get("review") or {}).get("word_ceiling", 12000) or 0)
    if total1 and ((floor and total1 < floor) or (ceil_w and total1 > ceil_w)):
        rep["reverted"] = (f"字数越界 {total0}→{total1}"
                           f"（要求 {floor}—{ceil_w}）")
        rep["ratio_after"] = rep["ratio_before"]
        log(f"[balance] 放弃：{rep['reverted']}")
        return full, rep
    return new_full, rep
