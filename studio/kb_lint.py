# -*- coding: utf-8 -*-
"""知识卡质量校验：卡里的内容会被 `knowledge.digest_for` 直接注入写作提示词。

为什么需要这个模块（实测驱动）：
    42 张卡里有 31 张存在"不可用内容"——
    * facts 里带「（待核）」这种未核标记（共 45 条），写手会把
      「流言传播速度是陌生环境的3倍（待核）」当既定事实写进正文；
    * cases（映射到提示词的「同类爆款案例」）里塞着刘禹锡《秋词》创作背景、
      古诗词意象解析——那不是爆款案例，是阅读理解答案，注入后直接
      把成稿语言往"文艺腔"上带（正是用户抱怨的 AI 味来源之一）；
    * summary 写成"用秋日意象隐喻……的生存哲学"，写手拿到的是文学评论，
      不是"这篇该怎么写"。

    这些不是玄学，是能确定性判定的：未核标记、诗词/典故来源、空转文艺词。
    所以这里给出一份 lint + 确定性清理（不猜、不改写语义，只删掉不可用的条目），
    并让 `kb lint` / 静态检查 / 冒烟测试三处共用同一份判据。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 带精确统计的句子：这类"事实"可被读者核查，未核就只能删，不能去标记留着
_PENDING_STAT = re.compile(r"\d+(?:\.\d+)?\s*(?:倍|%|％|成|万|亿)")

# 未核标记：出现即认定"这条不是事实，是待办"
_PENDING = ("待核", "待查", "待验证", "待确认", "待补充", "TODO", "占位", "？？")

# 诗词/典故类来源：对下沉市场短篇网文没有借鉴价值，反而污染语感
_POETRY = ("诗", "词", "古文", "典故", "赋", "曲", "意象解析", "名句赏析")
_POETRY_URL_HINTS = ("shici", "gushi", "guwen", "meirishici", "shidianguwen", "poem", "tang-shi", "song-ci")

# 空转文艺词：出现在 hooks（开头钩子）里说明这条钩子不是冲突，是抒情
_AIRY = ("意象", "隐喻", "象征", "哲学", "岁月", "时光", "灵魂", "远方", "星河",
         "月光", "白鹤", "诗意", "宿命", "命运感")

# 各字段的条目长度合理区间（字数）
# --- K09 题材串味：卡片内容与 direction 不在同一个世界 -----------------------
# 真实事故（2026-10-26）：`nvzhuhougong`（女主后宫）卡里 18 条 facts 全是「赶集摊位」
# 「信用社存折」「卤味摊」——误填自乡镇一女多男方向。写手照抄，宫斗就写成了乡镇创业。
# 这种错靠人工翻 42 张卡发现不了，靠「跨域标记词」可以。
_MODERN_MARKS = re.compile(
    r"微信|支付宝|转账|手机|存折|银行卡|信用卡|超市|外卖|物业|电梯|小区|快递|网贷|"
    r"社保|医保|公积金|工资条|发票|加班|面试|公司|老板|房贷|车贷|驾照|直播|短视频|朋友圈"
)
_ANCIENT_MARKS = re.compile(
    r"皇上|皇帝|妃|嫔|宫女|太监|宫人|娘娘|殿下|臣妾|朕|府邸|丫鬟|小厮|银两|铜板|"
    r"县衙|衙门|老爷|夫人|姑娘|庄子|牙婆|王爷|丞相|将军|世子|凤印|冷宫"
)

_FIELD_LEN = {
    "facts": (8, 140),
    "scenes": (6, 120),
    "dialogues": (6, 120),
    "hooks": (8, 90),
    "pitfalls": (6, 120),
    "redlines": (4, 120),
}

REQUIRED_FIELDS = ("facts", "scenes", "dialogues", "hooks", "pitfalls", "redlines")

# 具体度打分用的词表：不是"好看的句子"，是"能直接变成情节的句子"。
# 依据：人工对比同一张卡里「深夜喂奶时婆媳育儿理念冲突」（抽掉的壳）与
# 「阳台上婆婆翻出三十年前的旧尿布往晾衣绳上搭，儿媳站在推拉门边说妈现在都用纸尿裤了」
# （能拍的戏）——差别在：有没有地点、动作、钱数、能听见的一句话。
_PLACE = ("家", "屋", "房", "厅", "桌", "床", "院", "楼", "街", "店", "厂", "校", "车",
          "站", "窗", "门", "厨", "卫", "阳台", "公司", "医院", "银行", "市场", "工地",
          "村里", "县城", "柜台", "餐", "席", "办公室", "教室", "走廊")
_ACTION = ("拍", "摔", "扔", "递", "塞", "翻", "跪", "扯", "撕", "锁", "搬", "签", "转",
           "退", "拉", "推", "抓", "端", "收", "发", "喊", "骂", "哭", "要", "抬", "走",
           "坐", "站", "拿", "放", "拨", "发", "冲", "泼", "砸")
_MONEY = ("元", "块", "万", "块钱", "彩礼", "工资", "存款", "赔偿", "医药费", "手术费",
          "首付", "房贷", "奖金", "房租")
_QUOTE_MARKS = ("「", "」", "“", "”", "‘", "’", "'", "『", "』", "﹃", "﹄")
# 台词类专用：问句/感叹句制造压迫感；方言口语词是"听得见的口气"；称呼=有对手戏。
_DIALOG_MARK = re.compile(r"[？?！!]|……|…")
_COLLOQ = ("俺", "咱", "啥", "咋", "甭", "哩", "喽", "呗", "恁", "忒", "嗐", "哟", "嘚")
_ADDRESS = ("你", "您", "恁", "咱")

# 中文数字+量词：「三十年前的旧尿布」「两万块的早教课」这类同样是可验证的硬信息，
# 只认半边角数字会把最好的那批场景条目判成空泛。
_CN_NUM = re.compile(
    r"[一二三四五六七八九十百千万两0-9]\s*(?:元|块|万|千|百|年|岁|天|个月|小时|分钟|斤|平米|次|遍|回|层|口|间|张|条"
    r"|名|级|味|份|只|支|台|瓶|箱|袋|户|亩|里|公里|桌|场|轮|趟|句|字|页|号|栋|室|楼|章|节|步|倍|成|两)")
# 量词表要覆盖「罚俸半年」「三十七味药名」「色差一级」这类真实计数写法：
# 早期只列了钱/时间/频次，把带其他量词的硬信息误判成空泛条目。


def score_item(text: str, field: str = "") -> int:
    """条目具体度打分（0-10）：越高越"能拍成戏"，越低越"抽掉了壳的概括"。

    为什么需要：`knowledge.digest_for` 原来的同分排序是「短条目优先」——这条规则在
    "卡片只有人工写的 40 条"时没问题，但扩写（kb enrich）补进来的是长而具体的条目，
    结果**每章注入的仍是最短最空的那批**（"深夜喂奶时婆媳育儿理念冲突"），
    扩写出来的好料反过来被挤在预算外。用具体度替代长度做同分裁决。

    `field` 用来区分类别：`pitfalls`（读者雷点）/`redlines`（安全红线）天生是抽象建议
    （"别让老公当传话筒"），不该按"有没有地点动作"扣分，否则整类被判成低质。
    """
    t = (text or "").strip()
    if not t:
        return 0
    advisory = field in ("pitfalls", "redlines")
    if field == "dialogues":
        # 台词类不能用"地点+动作+数字"打分：一句好台词的判断标准是
        # 「听得出是谁在用哪种口气对谁施压」，与地点动作无关。
        # 旧口径下 113 条台词被误判成"空壳"（"俺家闺女跟你睡了半年，
        # 你现在跟俺算账？" 得分 1），选材时被压到最后。
        sc = 2
        if _DIALOG_MARK.search(t):
            sc += 2                               # 问句/感叹句 = 有火药味
        if any(w in t for w in _COLLOQ):
            sc += 2                               # 方言/口语词 = 不是书面语
        if any(w in t for w in _ADDRESS):
            sc += 1                               # 有称呼 = 有对手戏
        if len(t) >= 12:
            sc += 1
        if any(w in t for w in _AIRY):
            sc -= 3
        if any(m in t for m in _PENDING):
            sc -= 3
        return max(0, min(10, sc))
    sc = 0
    if re.search(r"\d", t):
        sc += 3                                   # 有数字 = 有可验证的硬信息
    elif _CN_NUM.search(t):
        sc += 2                                   # 中文数字+量词也算（"三十年前的旧尿布"）
    if any(m in t for m in _QUOTE_MARKS):
        sc += 2                                   # 有引号 = 有能听见的一句话
    if advisory:
        # 建议类：判"有没有落到具体做法"——出现动作/物件/台词化表述才算可用
        if any(w in t for w in _ACTION):
            sc += 2
        if len(t) >= 20:
            sc += 3
        return max(0, min(10, sc))
    if any(w in t for w in _MONEY):
        sc += 1
    if any(w in t for w in _PLACE):
        sc += 1
    if any(w in t for w in _ACTION):
        sc += 1
    if len(t) >= 24:
        sc += 1                                   # 够长才装得下场景
    if any(w in t for w in _AIRY):
        sc -= 3                                   # 文艺腔 = 拍不出来
    if any(m in t for m in _PENDING):
        sc -= 3                                   # 未核内容不能当事实
    if t.endswith(("等等", "之类", "方面", "问题", "现象")):
        sc -= 1                                   # 概括收尾，没有落点
    return max(0, min(10, sc))

CODE_LABEL = {
    "K01": "未核标记",
    "K02": "钩子抒情化",
    "K03": "案例与网文无关",
    "K04": "重复条目",
    "K05": "条目长度异常",
    "K06": "字段缺失/为空",
    "K07": "要诀写成文学评论",
    "K09": "题材串味（卡片内容与 direction 不在同一个世界）",
    "K08": "对白条目没有台词",
}

# 对白条目必须能直接抄进正文：要么有一句被引号括起来的台词，要么是带说话人
# 语气的原话。标定教训：一开始用「有没有引号」当判据，把 404 条真实台词
# （`妈，这收据上连个公章都没有，你花四千块就换张纸条？`）全判成废料——
# 引号只是排版，不是台词的本质。真正的废料是没有语气的分类标签。
_DIALOG_LINE = re.compile(r"[「『\"“‘][^」』\"”’\n]{2,}[」』\"”’]")
_VAGUE_LABEL = re.compile(r"(争执|冲突|矛盾|分歧|博弈|尴尬|压力|指责|抱怨|不满|角力|拉扯|隔阂|算计|较量)[。.！!；;]?$")
_SPEECH_MARK = re.compile(r"[你您我咱俺妈爸哥姐弟妹叔婶爷奶婆媳闺女娃账钱]|[？?!！：]|说|问|喊|叫|骂")


def has_dialog_line(text: str) -> bool:
    """条目里是否含一句被引号括起来的台词。"""
    return bool(_DIALOG_LINE.search(str(text)))


def is_vague_dialog(text: str) -> bool:
    """对白条目是不是「概述」而不是「可抄的原话」。"""
    t = str(text).strip()
    if has_dialog_line(t):
        return False
    # 只认「分类标签」这一种废料。格言式台词（"药可以换，婚帖不能换"）没有
    # 你/我/？也不带引号，但它是能直接抄的原话，不能用语气词判据误伤。
    return bool(_VAGUE_LABEL.search(t))


def _txt(x) -> str:
    return json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x


def lint_card(card: dict, name: str = "") -> dict:
    """返回 {name, issues:[{code, field, index, text}], cleanable:[...]}。"""
    issues: list[dict] = []
    for field in REQUIRED_FIELDS:
        vals = card.get(field)
        if not isinstance(vals, list) or not [v for v in vals if str(v).strip()]:
            issues.append({"code": "K06", "field": field, "index": -1, "text": ""})
    for field in ("facts", "scenes", "dialogues", "hooks", "pitfalls", "redlines"):
        vals = card.get(field) or []
        if not isinstance(vals, list):
            continue
        seen: dict[str, int] = {}
        for i, v in enumerate(vals):
            t = str(v).strip()
            if not t:
                continue
            if any(mark in t for mark in _PENDING):
                issues.append({"code": "K01", "field": field, "index": i, "text": t[:40]})
            if field == "hooks" and any(w in t for w in _AIRY):
                issues.append({"code": "K02", "field": field, "index": i, "text": t[:40]})
            if field == "dialogues" and is_vague_dialog(t):
                issues.append({"code": "K08", "field": field, "index": i, "text": t[:40]})
            if t in seen:
                issues.append({"code": "K04", "field": field, "index": i, "text": t[:40]})
            else:
                seen[t] = i
            lo, hi = _FIELD_LEN.get(field, (4, 200))
            if len(t) < lo or len(t) > hi:
                issues.append({"code": "K05", "field": field, "index": i,
                               "text": f"{len(t)}字：{t[:30]}"})
    for i, c in enumerate(card.get("cases") or []):
        blob = _txt(c)
        url = str(c.get("url") or "") if isinstance(c, dict) else ""
        title = str(c.get("title") or "") if isinstance(c, dict) else blob
        if any(h in url.lower() for h in _POETRY_URL_HINTS) or (
                isinstance(c, dict) and "《" in title and any(k in title for k in _POETRY)):
            issues.append({"code": "K03", "field": "cases", "index": i, "text": title[:40]})
        elif any(k in title for k in ("意象解析", "名句赏析", "创作背景")):
            issues.append({"code": "K03", "field": "cases", "index": i, "text": title[:40]})
    summary = str(card.get("summary") or "")
    if summary and sum(1 for w in _AIRY if w in summary) >= 2:
        issues.append({"code": "K07", "field": "summary", "index": -1, "text": summary[:50]})
    # K09 题材串味（只判有明确古今归属的方向；跨题材方向不判）
    try:
        from .langqa import genre_tags

        _dir = str(card.get("direction") or name)
        # 「穿书/快穿/系统/无限流/诸天/攻略」是跨世界的叙事装置，故事可能落在任何时代，
        # 不判串味（否则穿进古书的卡会被当成古装误报，反之亦然）。
        _cross = ("穿书", "快穿", "系统", "无限流", "诸天", "攻略")
        ancient = bool(genre_tags(_dir))
        if not any(k in _dir for k in _cross):
            body = " ".join(
                str(x) for f in ("facts", "scenes", "dialogues", "hooks", "pitfalls")
                for x in (card.get(f) or [])
            )
            n_mod = len(_MODERN_MARKS.findall(body))
            n_anc = len(_ANCIENT_MARKS.findall(body))
            wrong = (ancient and n_mod > n_anc) or (
                not ancient and n_anc > max(5, n_mod * 3))
            if wrong:
                issues.append({
                    "code": "K09", "field": "direction", "index": -1,
                    "text": (f"题材串味：direction={card.get('direction')}，"
                             f"古代标记 {n_anc} 处 / 现代标记 {n_mod} 处——"
                             f"这张卡可能被别的方向的内容覆盖过"),
                })
    except Exception:  # noqa: BLE001  题材映射不可用时跳过，不阻断 lint
        pass
    return {"name": name, "issues": issues}


def lint_all(root: Path) -> list[dict]:
    out = []
    for p in sorted(Path(root).glob("*.json")):
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            out.append({"name": p.stem, "issues": [{"code": "K00", "field": "-", "index": -1,
                                                    "text": f"JSON 解析失败：{exc}"}]})
            continue
        r = lint_card(card, p.stem)
        if r["issues"]:
            out.append(r)
    return out


def _strip_pending(text: str) -> str:
    """去掉「（待核）」这类采集期标记，保留句子本体。"""
    out = text
    for mark in _PENDING:
        out = out.replace(f"（{mark}）", "").replace(f"({mark})", "")
        out = out.replace(mark, "")
    return out.strip()


def fix_card(card: dict) -> tuple[dict, list[str]]:
    """确定性清理：处理"不可用"条目。

    分两种处理，判据是"这句话有没有可被读者核查的硬指标"：
    * 带精确统计的未核句子（"流言传播速度是陌生环境的3倍（待核）"）→ **删**：
      写手会把它当既定事实写进正文，而它本来就没人核过。
    * 只是被采集器打了标记的常识句（"微信转账记录、支付宝流水是最常见的取证（待核）"）
      → **去标记保留**：删掉等于白扔卡片里最有用的部分。
    * 诗词/典故类 cases → **删**（映射到提示词里是「同类爆款案例」，注入即污染语感）。
    只删不写——补内容必须靠人/LLM 重写，猜出来的"事实"比空着危害更大。
    """
    changes: list[str] = []
    new = dict(card)
    stripped = 0
    for field in ("facts", "scenes", "dialogues", "hooks", "pitfalls", "redlines"):
        vals = card.get(field)
        if not isinstance(vals, list):
            continue
        keep: list[str] = []
        seen: set[str] = set()
        for v in vals:
            t = str(v).strip()
            if not t:
                continue
            if any(mark in t for mark in _PENDING):
                if _PENDING_STAT.search(t):
                    changes.append(f"删 {field} 未核统计：{t[:36]}")
                    continue
                t = _strip_pending(t)
                stripped += 1
            if t in seen:
                changes.append(f"删 {field} 重复条目：{t[:30]}")
                continue
            seen.add(t)
            keep.append(t)
        new[field] = keep
    cases = card.get("cases") or []
    keep_cases = []
    for c in cases:
        blob = _txt(c)
        url = str(c.get("url") or "") if isinstance(c, dict) else ""
        title = str(c.get("title") or "") if isinstance(c, dict) else blob
        if any(h in url.lower() for h in _POETRY_URL_HINTS) or (
                isinstance(c, dict) and "《" in title and any(k in title for k in _POETRY)) \
                or any(k in title for k in ("意象解析", "名句赏析", "创作背景")):
            changes.append(f"删 cases 非网文案例：{title[:30]}")
            continue
        keep_cases.append(c)
    new["cases"] = keep_cases
    if stripped:
        changes.append(f"去掉未核标记保留条目 {stripped} 条")
    return new, changes
