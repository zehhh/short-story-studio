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


# ------------------------------------------------- 台账：已消耗的道具 / 已退场的人
#
# 这两类跟「无因之果」是同一件事的反面：前面记「凭空出现」，这里记「已经用过/已经
# 走了，却又拿出来/又开口」。v15 成稿的 6 条 high 硬伤里，有 2 条正是这一类
# （08 蜜丸已服下毒已解，紧接着「取出另一颗」；04 结尾萧景琰已转身离去，
# 05 开头他还在场）。判官能引原文说明它抓得准，写手记不住是因为逐节写时每节
# 都是一次新对话——所以这一类必须由代码记，不能指望模型记得。

# 消耗类动词：动作完成后，这个东西在故事里就没了。
_CONSUME_RE = re.compile(
    "吞下|服下|吃下|喝下|咽下|饮尽|喝完|吃完|"
    "放入口中|送入口中|含入口中|含进嘴里|"
    "用掉|用完|耗尽|烧掉|烧了|焚了|撕碎|撕了|摔碎|砸碎|碾碎|捏碎|"
    "交给|交给|递给|塞给|送给了|给了|赏了|丢了|扔了|扔进|撒了|泼了"
)
# 取用类动词：又在手上用它了。只认「拿到手/拿在手里」这一类，避免把
# 「那颗蜜丸的药力还在」这种追述也算成复用。
_PICKUP_RE = re.compile(
    "取出|拿出|掏出|摸出|抽出|翻出|捏出|拈出|捡起|拾起|端起|托起|接过|"
    "又取|再取|又拿|再拿|又摸|又掏"
)
# 宾语抽取要吃两种语序：把字句（「把那颗蜜丸吞下」）与动宾句（「吞下那颗蜜丸」）。
# 只按「动词前 2~4 字」抽会抽到紧贴动词的状语（「我张口吞下」→ 抽成「张口」，
# 实测就是这么错的），所以把字句必须锚在 把/将 上，动宾句必须锚在量词上。
_MEASURE = "颗枚片张块包封瓶盒只条根支把碗杯壶袋卷册粒服帖"
_OBJ_BEFORE = re.compile(
    r"(?:把那|将那|把这|将这|把|将)([\u4e00-\u9fa5]{2,4})"
    r"(?:" + _CONSUME_RE.pattern + ")"
)
_OBJ_AFTER = re.compile(
    r"(?:" + _CONSUME_RE.pattern + r")(?:了)?"
    r"(?:那|这|一)?[" + _MEASURE + r"]([\u4e00-\u9fa5]{2,3})"
)


def _objs_in_sentence(sent: str) -> list[str]:
    """一句话里被消耗掉的物件（把字句 + 动宾句两种语序都认）。"""
    out: list[str] = []
    for rx in (_OBJ_BEFORE, _OBJ_AFTER):
        for m in rx.finditer(sent):
            obj = m.group(1)
            if len(obj) < 2 or obj in _GENERIC_OBJ:
                continue
            if obj not in out:
                out.append(obj)
    return out
# 代词与泛称不能当道具（「把那件事说出去」「把话交代了」）。
_GENERIC_OBJ = frozenset("""
东西 事情 问题 话 话说 消息 银子 银钱 钱 命 气息 力气 心思 念头 人 他们 我们
自己 什么 这个 那个 一个 这些 那些 众人 众人面 众人面面
""".split())


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？])", text or "") if s.strip()]


def consumed_props(text: str) -> list[dict]:
    """正文里「用掉了/交出去了/毁掉了」的东西（按出现顺序，同物取最后一次）。"""
    out: list[dict] = []
    for sent in _sentences(text):
        for obj in _objs_in_sentence(sent):
            out.append({"obj": obj, "sentence": sent.strip()[:60]})
    return out


def prop_reuse(prior_text: str, body: str) -> list[dict]:
    """前文已经用掉的东西，本节又「取出/拿出」了。

    只在三种条件同时成立时报：①前文有明确的消耗句；②消耗之后前文**再没有**
    把同一个东西取用回来（再取用就算重新获得，不报）；③本节出现了「取用动词 +
    这个东西」的句子。宁可漏判：误报会逼写手删掉正常的情节。

    `body` 里的**节内**复用也一起查：v15 第 08 节就是「我将蜜丸放入口中…吞下」
    之后紧接着「我从发髻中取出另一粒早已藏好的蜜丸吞下」，两处都在同一节里，
    跨节判据抓不到。
    """
    out: list[dict] = []
    prior_sents = _sentences(prior_text) if prior_text else []
    consumed: dict[str, int] = {}
    for i, sent in enumerate(prior_sents):
        for obj in _objs_in_sentence(sent):
            consumed[obj] = i          # 同物多次消耗以最后一次为准
    body_sents = _sentences(body) if body else []
    # ① 跨节：前文消耗过、且消耗后没再取用 → 本节若取用就算矛盾
    for obj, at in consumed.items():
        if any(obj in s and _PICKUP_RE.search(s) for s in prior_sents[at + 1:]):
            continue                   # 重新获得过，不算
        for sent in body_sents[:12]:
            if obj in sent and _PICKUP_RE.search(sent):
                out.append({"obj": obj,
                            "consumed": prior_sents[at].strip()[:60],
                            "sentence": sent.strip()[:60]})
                break
    # ② 节内：本节自己的消耗句之后又取用
    for i, sent in enumerate(body_sents):
        for obj in _objs_in_sentence(sent):
            for later in body_sents[i + 1:]:
                if obj in later and _PICKUP_RE.search(later):
                    out.append({"obj": obj,
                                "consumed": sent.strip()[:60],
                                "sentence": later.strip()[:60]})
                    break
    return out


# 明确的退场动作：写完这句，这个人就不在这个场面里了。
_EXIT_RE = re.compile(
    "转身离去|转身离开|转身走了|转身出去|拂袖而去|拂袖离开|"
    "退了出去|退了下去|退到门外|退出门外|告退|告辞|"
    "走了出去|站起身走了|起身离开|背影消失|离了殿|送出了?殿"
)


def exit_then_present(prev_text: str, body: str, names: list[str]) -> list[dict]:
    """上一节结尾已经退场的人，本节开头又在场。

    判据收得很紧：①退场句必须是上一节**最后两句**之一；②名字要出现在同一句；
    ③本节**前两句**里又出现这个名字（并且不是「想起他」「他留下的」这类追述）。
    这样「他转身离去，我盯着门」→「次日他来了」不会被误报（次日来了是重新到场），
    因为名字得落在本节开头两句且本节没有重新到场的动作也不行——所以再加一条：
    本节开头若含「又/回到/回来/再来/进门/入殿」就不算，那是正常的重新到场。
    """
    if not prev_text or not body or not names:
        return []
    tails = _sentences(prev_text)[-2:]
    if not tails:
        return []
    exits: list[tuple[str, str]] = []
    for sent in tails:
        if _EXIT_RE.search(sent):
            for n in names:
                if n and n in sent:
                    exits.append((n, sent.strip()[:60]))
    if not exits:
        return []
    heads = _sentences(body)[:2]
    head = "".join(heads)
    if re.search("又回到|回到|回来|再来|重又|进门|入殿|进来|返回", head):
        return []
    out = []
    for n, sent in exits:
        if n in head:
            out.append({"name": n, "exit": sent, "sentence": head.strip()[:60]})
    return out


def hint_prop_reuse(hit: dict) -> str:
    return (f"「{hit['obj']}」前面已经用掉了（「{hit['consumed']}」），这一节又"
            f"「{hit['sentence']}」把它取了出来——已经消耗/交出去的东西不能再原样"
            "拿出来；要它再出现，必须先写明怎么重新拿到的")


def hint_exit_present(hit: dict) -> str:
    return (f"{hit['name']}在上一节结尾已经退场（「{hit['exit']}」），这一节开头"
            f"又在场（「{hit['sentence']}」）——要么把他不在场的这段写清楚"
            "（他什么时候回来的），要么本节开头从他的不在场写起")


# ------------------------------------------------------------ 生死台账

# 「X 死了」的判定：名字与死亡词必须落在同一句、且距离不超过 24 字
# （避免「裴寂说太后死了」这种把两个人串起来的误判——它们同句但主语不同时，
#  距离会明显更大，而真正的死亡宣告都是「裴寂死在冷宫」「裴寂已死」这种贴身写法）。
# 宣告死亡必须是**名字自己的谓语**：名字后面紧跟（可带一个虚词/副词）死亡动词。
# 不能用「名字附近 8 字内有死亡词」——实测误判全是这种：「太后想让我死」（死的是我）、
# 「走向萧景琰，便是赴死之路」（赴死的是我）、「柳如烟拼死塞给我的东西」（拼死是状语）。
_CLAIM_HEAD = re.compile(
    r"^[，,、]?(?:已经|已|都|也|就|便|则|终于|后来|随即|当即|果然|当真)?"
    # 死亡动词后必须跟标点/助词/空白或断在窗口末尾——「死死盯着我」的「死」后面
    # 又是「死」，不是宣告死亡（全库实测只有这一种形态造成误判）。
    r"(?:死|亡|薨|殁|咽了?气|断了?气|丧命|毙命|不治|没了?气息)"
    r"(?=$|[了在得于，。！？；：、」』…\s])(?!前|后|时|因)"
)
_DEATH_RE = re.compile(r"死|亡|薨|殁|咽气|断气|丧命|毙命|不治|气息")
_CLAIM_WIN = 10
# 这些词出现时，句子不是在宣告死亡，而是在回忆/假设/否定
_DEATH_GUARD = re.compile(r"要是|如果|倘若|假装|谎称|诈死|假死|没死|未死|不会死|不想死|"
                          r"以为.{0,6}死|若非|差点死|险些死")
# 人物「在场且活着」的动作（不含死亡词时才算复活迹象）
_ALIVE_ACT = re.compile(
    r"走进|走入|推门|跪下|磕头|开口|笑道|说道|问道|答|抬头|低头|伸手|接过|递过|"
    r"站起|坐下|上前|俯身|看着|盯着|望向|转身|抱拳|行礼|作揖|摆手|点头|摇头|"
    r"喘着|说着|提着|端着|捧着|拔|抽|抽出|按住|抓住|扶住|扶起|走过来|走出来"
)


def death_claims(text: str, names: list[str]) -> list[dict]:
    """本节里「谁被宣告死了」——只认名字与死亡词贴身同句的写法。"""
    out: list[dict] = []
    for sent in _sentences(text or ""):
        if _DEATH_GUARD.search(sent):
            continue
        for nm in names or []:
            i = sent.find(nm)
            if i < 0 or len(nm) < 2:
                continue
            window = sent[i + len(nm): i + len(nm) + _CLAIM_WIN]
            if _CLAIM_HEAD.match(window):
                out.append({"name": nm, "sentence": sent})
                break
    return out


def death_audit(secs: list[dict], names: list[str]) -> list[dict]:
    """**跨节**生死台账：宣告死亡之后再像个活人一样出场 = 复活。

    `secs` 是 [{no, text}, ...]（按节序）。返回 [{name, dead_at, alive_at, dead_sent, alive_sent}]。
    为什么这条必须做成确定性判据：v23 成稿里「05 裴寂被刺重伤 → 06 说『三个月前裴寂
    死在冷宫』→ 07 裴寂穿着太医官服走出来跪下认罪」——判官能抓到，但只在终审阶段，
    代价是整篇重写；先在本章内累积台账，第 07 节写到一半就能拦下来重写。
    """
    dead: dict[str, dict] = {}
    hits: list[dict] = []
    for sec in secs or []:
        body = sec.get("text") or ""
        no = sec.get("no") or ""
        # 先查「已死之人又活了」，再记本节新的死亡宣告（本节内死在本节内活不算）
        for nm, rec in list(dead.items()):
            for sent in _sentences(body):
                if nm not in sent or len(nm) < 2:
                    continue
                if _DEATH_RE.search(sent) or _DEATH_GUARD.search(sent):
                    continue
                if _ALIVE_ACT.search(sent):
                    hits.append({"name": nm, "dead_at": rec["no"], "alive_at": no,
                                 "dead_sent": rec["sentence"], "alive_sent": sent})
                    break
        for c in death_claims(body, names):
            dead.setdefault(c["name"], {"no": no, "sentence": c["sentence"]})
    return hits


def resurrect_in(prior_texts: list[str], body: str, names: list[str]) -> list[dict]:
    """逐节写时用：前面的节里已宣告死亡，本节又让这个人活着出场。

    与 `death_audit` 的区别是只报「本节」这一处，供节级门禁当场打回重写；
    `death_audit` 是交付前对全篇扫一遍的版本。
    """
    n = len(prior_texts or []) + 1
    secs = [{"no": k, "text": t} for k, t in enumerate(prior_texts or [], 1)]
    secs.append({"no": n, "text": body or ""})
    return [h for h in death_audit(secs, names) if h.get("alive_at") == n]


def hint_death(hit: dict) -> str:
    return (f"{hit['name']}在第 {hit['dead_at']} 节已经被写成死了"
            f"（「{hit['dead_sent'][:26]}」），这一节又以活人的样子出现"
            f"（「{hit['alive_sent'][:26]}」）——死人不许复活。三种改法："
            "①这一节改成别人的视角提到他（回忆、遗物、传闻），不让他出场；"
            "②前面那处「死」改成「重伤/失踪/生死不明」；"
            "③若确实是假死，本节必须当场写明是谁安排的、为了骗谁")


# ---- 自称一致性台账 -------------------------------------------------------
# 中文古言里最容易出、也最刺眼的一类错：同一个说话人，对同一个人，自称在
# 「朕 / 臣妾 / 儿子 / 臣」之间乱跳。v25 终审原文：「皇帝对太后自称臣妾，
# 称谓彻底错乱」，同一段里还出现「儿子自有分寸」「臣妾打算让她真疯」。
# 判据只做一件事：同一个名字的台词里，不许同时出现「男性自称」和「女性自称」。
_SELF_MALE = ("朕", "寡人", "本王", "本侯", "本将军", "臣", "微臣", "在下", "卑职",
              "为兄", "儿子", "孙儿", "老夫")
_SELF_FEMALE = ("臣妾", "妾身", "本宫", "哀家", "奴婢", "小女", "女儿", "臣女",
                "孙女儿", "老身")
# 这些是通用第一人称，男女都用，不参与性别判定
_SELF_NEUTRAL = ("我", "咱", "本人")

_SAY_VERB = r"(?:说|道|笑道|低声道|轻声道|开口|答道|回话|喝|喊|问道|冷冷道|急道)"
_STOP = "".join(["\\u3002\\uff01\\uff1f", "\\n", "\\u300c\\u300d"])  # 。！？ 换行 「」
_NOSTOP = "[^" + "。！？\n「」" + "]"
_QUOTE_TAIL = re.compile(
    _NOSTOP + r"{0,14}?" + _SAY_VERB
    + _NOSTOP + r"{0,6}?[：:，,]?\s*[「“]([^」”]{2,300})[」”]"
)
_QUOTE_HEAD = re.compile(r"[「“]([^」”]{2,300})[」”]" + _NOSTOP + r"{0,10}$")
_TOKENS = tuple(sorted(_SELF_MALE + _SELF_FEMALE, key=len, reverse=True))


def _tokens_in(seg: str) -> list[str]:
    """段落里出现过哪些自称。长词优先并遮蔽，避免「臣妾」同时算成「臣」。"""
    work, got = seg, []
    for tok in _TOKENS:
        if tok and tok in work:
            got.append(tok)
            work = work.replace(tok, "□")
    return got


def self_refs(text: str, names: list[str]) -> dict[str, dict[str, list[str]]]:
    """抽出「谁在这篇里用过哪些自称」，返回 {名字: {自称: [例句...]}}。

    归属不靠正则猜名字边界（`萧承煜放下茶盏，淡淡道：「…」` 会被贪婪吃成
    「萧承煜放」），而是先定位名字，再看它前后各 30/320 字内有没有「动词+引号」。
    """
    out: dict[str, dict[str, list[str]]] = {}
    body = text or ""
    for nm in names or []:
        if len(nm) < 2:
            continue
        for m in re.finditer(re.escape(nm), body):
            seg = ""
            mq = _QUOTE_TAIL.match(body[m.end(): m.end() + 40])
            if mq:
                seg = mq.group(1)
            else:
                mh = _QUOTE_HEAD.search(body[max(0, m.start() - 320): m.start()])
                if mh:
                    seg = mh.group(1)
            if not seg:
                continue
            for tok in _tokens_in(seg):
                out.setdefault(nm, {}).setdefault(tok, []).append(seg[:40])
    return out


def self_ref_breaks(text: str, names: list[str]) -> list[dict]:
    """同一个人的台词里同时出现男性自称与女性自称 → 称谓错乱。"""
    out: list[dict] = []
    for who, toks in self_refs(text, names).items():
        male = [t for t in toks if t in _SELF_MALE]
        female = [t for t in toks if t in _SELF_FEMALE]
        if male and female:
            out.append({"name": who, "male": male, "female": female,
                        "sample": (toks[male[0]][0] if toks.get(male[0]) else "")})
    return out


def hint_self_ref(hit: dict) -> str:
    return (f"{hit['name']}的自称前后矛盾：一处自称「{hit['male'][0]}」，"
            f"另一处自称「{hit['female'][0]}」（「{hit['sample'][:30]}」）"
            "——一个人的自称在一章里必须固定（皇帝对臣下称「朕」、对太后称「儿」；"
            "妃嫔对皇帝称「臣妾」、对下人自称「我」），不许男女自称混用")
