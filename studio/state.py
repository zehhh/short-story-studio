"""正文状态推演：时间线推进 + 道具/手段首次出现（纯计算，零 token）。

为什么要有这个模块
------------------
逐节写（`stage_write_section`）每一节都是独立一次对话，写手看不到全篇。
实测 v11 终审剩下 4 处 high 硬伤里，两处是**同一类系统缺陷**：

1. **时间线倒退**：小节 03 结尾人已走到门口、天刚亮，小节 04 开头又「天刚亮」，
   把同一个早晨重铺一遍；
2. **无因之果**：「袖中银针弹出击碎瓷壶」「运劲改脉」——前文从没交代过这个人
   有银针、会改脉。

这两类在 ConStory-Bench（arXiv:2603.05890）的一致性错误分类里分别对应
「Timeline & Plot Logic / Absolute Time Contradictions」与
「Causeless Effects（Characters suddenly possessing unexplained abilities;
Important items appearing without introduction）」，是该论文实测**最高发**的
两个维度（factual 与 temporal）。

本模块的结论（实测得出，不是设计出来的）：

- **时间倒退不能靠词法判**。试过「上一节最后一个时点词 vs 本节第一个时点词」，
  全库 35 篇真实成稿命中 47 次，逐条核对几乎全是正常叙事跳跃（本节开头在回忆
  那天早上、时点词出自台词、跨夜没写「次日」）。中文叙事不靠时点词标记日期推进，
  纯词法比较必然误判 → 按决策 35 弃用，只在 `prev_time_hint()` 里做**上下文注入**
  （把上一节收尾的时点告诉写手，从源头减少重铺），判定交给
  `pipeline._sec_boundary` 的轻模型带原文判。
- **无因之果可以确定性判**：全库 35 篇只命中 3 处，且真阳性明确（「袖中银针
  弹出击碎瓷壶」，前文从没提过银针）。`causeless()` 就做这一件事。

两类问题在 ConStory-Bench（arXiv:2603.05890）的一致性错误分类里分别对应
「Timeline & Plot Logic / Absolute Time Contradictions」与
「Causeless Effects（Characters suddenly possessing unexplained abilities;
Important items appearing without introduction）」，是该论文实测**最高发**的两个维度。

设计原则
--------
- **宁可漏判，不可误判**（决策 35）；误报会把写手带进无意义的整节重写。
- 判定与提示共用同一份词表，避免「检测说有问题、修复不知道怎么改」。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- 时点

# 一天之内的时间词 → 排序值（小时刻度）。同一时段的不同说法给同一个值，
# 因为它们在实际叙事里可以互换（「傍晚」与「黄昏」不该被判成两次不同时刻）。
_TIME_TABLE: list[tuple[int, tuple[str, ...]]] = [
    (4, ("破晓", "天蒙蒙亮", "天刚亮", "天微亮", "天色微明", "东方泛白", "天光乍亮")),
    (6, ("清晨", "清早", "早晨", "一早", "早上", "天亮")),
    (9, ("上午",)),
    (12, ("晌午", "正午", "中午", "日中", "晌午时分")),
    (15, ("午后", "下午", "日头偏西")),
    (18, ("傍晚", "黄昏", "日落", "夕阳西下", "日头落山")),
    (20, ("入夜", "掌灯", "夜里", "夜间", "夜幕", "天黑", "夜半前")),
    (23, ("深夜", "半夜", "三更", "夜深", "子夜")),
]

# 十二时辰（宫廷/古言常用）。子时跨零点，按 23 处理（同「深夜」）。
_SHICHEN: dict[str, int] = {
    "子时": 23, "丑时": 2, "寅时": 4, "卯时": 6, "辰时": 8, "巳时": 10,
    "午时": 12, "未时": 14, "申时": 16, "酉时": 18, "戌时": 20, "亥时": 22,
}

_TIME_WORDS: list[tuple[str, int]] = []
for _rank, _words in _TIME_TABLE:
    _TIME_WORDS.extend((w, _rank) for w in _words)
_TIME_WORDS.extend(_SHICHEN.items())
# 长词优先：「天蒙蒙亮」必须比「天亮」先匹配上
_TIME_WORDS.sort(key=lambda kv: -len(kv[0]))
_TIME_RE = re.compile("|".join(re.escape(w) for w, _ in _TIME_WORDS))
_TIME_MAP = dict(_TIME_WORDS)

def time_points(text: str) -> list[tuple[int, str]]:
    """按出现顺序返回正文里的时点 [(排序值, 原词), ...]。"""
    out: list[tuple[int, str]] = []
    for m in _TIME_RE.finditer(text or ""):
        out.append((_TIME_MAP.get(m.group(0), 12), m.group(0)))
    return out


def first_time(text: str) -> tuple[int, str] | None:
    pts = time_points(text)
    return pts[0] if pts else None


def last_time(text: str) -> tuple[int, str] | None:
    pts = time_points(text)
    return pts[-1] if pts else None


def prev_time_hint(prev_text: str) -> str:
    """把「上一节最后写到哪个时点」变成一句可以直接塞进提示词的约束。

    **为什么不做确定性倒退判定**：试过「上一节最后一个时点词 vs 本节第一个时点词，
    倒着走就报」，在全库 35 篇真实成稿上跑出 **47 次命中**，逐一核对几乎全是
    正常叙事跳跃——`下午 → 早上`（本节开头在回忆那天早上）、`半夜 → 夜里`
    （时点词出自台词）、`傍晚 → 清晨`（跨了一夜但没写「次日」）。中文叙事不靠
    时点词标记日期推进，纯词法比较必然误判，按决策 35（宁可漏判不可误判）弃用，
    改由 `pipeline._sec_boundary` 的轻模型带着原文判。这里只保留**上下文注入**：
    把上一节的收尾时点显式告诉写手，从源头减少「同一个早晨重铺一遍」。
    """
    p = last_time(prev_text)
    if not p:
        return ""
    _rank, word = p
    return (f"上一节最后写到的时点是「{word}」。这一节不能回到「{word}」之前的时段；"
            "除非明写「次日」「三日后」这类推进，否则开头第一句就要比它晚")


def hint_causeless(hit: dict) -> str:
    return (f"「{hit['item']}」在上一节里没有出现过，本节第一次出现就拿来用了"
            f"（「{hit['sentence']}」）——新手段/新道具第一次露面必须当场交代来源："
            "谁给的、什么时候备下的、跟谁学的，一句话带过")


def summary(prev_text: str, body: str) -> list[str]:
    """把本节的问题压成能直接塞进「上一次输出不合格」列表的字符串。"""
    return [hint_causeless(h) for h in causeless(prev_text, body)[:2]]


# ------------------------------------------------------- 道具 / 手段

# 需要「来源」的东西：有明确物理形态、能凭空出现也会被读者抓到。
# 只收**跨题材通用**的器物与技能，不收「汤药」「手炉」这类随手可得的日常物。
_ITEMS: tuple[str, ...] = (
    "银针", "暗器", "袖箭", "匕首", "短刀", "长剑", "软鞭", "弓弩", "火铳",
    "令牌", "虎符", "印信", "腰牌", "玉牌", "密信", "账册", "地契",
    "钥匙", "火折子", "信笺",
    "易容", "口技", "点穴", "内力", "轻功", "机关",
)
_ITEM_RE = re.compile("|".join(sorted((re.escape(w) for w in _ITEMS), key=len, reverse=True)))

# 「首次出现也算交代过来源」的说法：拿出/藏起/受赠/学艺/祖传/备下。
_INTRO_RE = re.compile(
    "摸出|取出|掏出|抽出|拿出|翻出|解下|摘下|拔下|从[^，。！？]{0,6}(?:袖|怀|襟|腰|包袱|匣|囊|袋|发间|靴)"
    "|藏|备下|备好|带上|带了|留着|收着|揣|递给|交给|塞进|索要|讨来|"
    "学过|练过|会使|会用|会些|懂些|祖传|家传|师父|教我|母亲留|留下的|"
    "原是他|原来是|先前|早先|那日|当年"
)


def items_in(text: str) -> list[str]:
    """正文里出现过的道具/手段（去重，保持出现顺序）。"""
    seen: list[str] = []
    for m in _ITEM_RE.finditer(text or ""):
        w = m.group(0)
        if w not in seen:
            seen.append(w)
    return seen


def known_items(secs: list[dict]) -> list[str]:
    """截至当前已在前文出现过的道具/手段（用于注入下一节提示词）。"""
    out: list[str] = []
    for s in secs or []:
        for w in items_in(str(s.get("text") or "")):
            if w not in out:
                out.append(w)
    return out


def first_mention(body: str, item: str) -> tuple[str, bool] | None:
    """某个道具在本节**首次出现**的那一句，以及这一句里有没有交代来源。"""
    pos = (body or "").find(item)
    if pos < 0:
        return None
    a = max(0, (body or "").rfind("。", 0, pos) + 1)
    b = (body or "").find("。", pos)
    sent = (body or "")[a:(b + 1) if b >= 0 else len(body)]
    return sent.strip(), bool(_INTRO_RE.search(sent))


# 「这个东西被**当工具用**了」的动作词。没有它，道具只是出现在描写里
# （「泥土上留着毒药的痕迹」），不构成「凭空拿在手上」。
_USE_RE = re.compile(
    "弹出|弹出|射出|刺入|刺向|刺穿|掷出|甩出|挥出|挥动|砍下|劈下|挑开|"
    "点住|点向|点中|催动|按动|扣动|绞住|缠住|架上|抵住|亮出|举起|"
    "取出|掏出|摸出|抽出|拿出|服下|吞下|灌下|喂下|倒进|洒出|泼出|扎进|割开"
)


# 假设句/否定句里出现「服下解药」不是真在用，不该算无因之果。
_HYPOTHETICAL_RE = re.compile("除非|如果|要是|倘若|若是|难道|或者|并不会|不是")


def causeless(prev_text: str, body: str) -> list[dict]:
    """本节首次出现、前文没提过、本句也没交代来源的道具/手段。

    只报「前文完全没有 + 本节首次出现的那句没有来源线索 + 那句确实在拿它做事」
    的，属于确定性可证的一类；语义层面（「手段不属于这个人」）交给
    `pipeline._sec_boundary` 的轻模型判。
    """
    prev_have = set(items_in(prev_text))
    out: list[dict] = []
    checked: set[str] = set()
    for item in items_in(body):
        if item in prev_have or item in checked:
            continue
        checked.add(item)
        fm = first_mention(body, item)
        if not fm:
            continue
        sent, has_intro = fm
        if has_intro or not _USE_RE.search(sent):
            continue
        if _HYPOTHETICAL_RE.search(sent):
            continue
        out.append({"item": item, "sentence": sent.strip()[:60]})
    return out


def hint_causeless(hit: dict) -> str:
    return (f"「{hit['item']}」在上一节里没有出现过，本节第一次出现就拿来用了"
            f"（「{hit['sentence']}」）——新手段/新道具第一次露面必须当场交代来源："
            "谁给的、什么时候备下的、跟谁学的，一句话带过")


def summary(prev_text: str, body: str) -> list[str]:
    """把本节的问题压成能直接塞进「上一次输出不合格」列表的字符串。"""
    return [hint_causeless(h) for h in causeless(prev_text, body)[:2]]
