"""书名卖点门：书名不是「作品名」，是信息流里的第一条广告。

依据（2026-10 番茄现状）：
- 番茄签约被拒的第一大显性原因就是书名没卖点。4 字书名、纯名词堆叠、
  文艺腔书名在任何一个平台都过不了——番茄只是把网文本该有的门槛摆出来了。
- 榜单在用的书名结构基本是三段式：**身份/关系 + 冲突动作 + 反转钩**，
  15–30 字，接近 30 字最好；中间用「，」或「：」断开，让读者一眼看到反转。
  实测榜单样本（fanqienovel.com 2026-10-26 首页在更书）：
  「穿越后，女主求我别走」「港岛73：我爹搞社团我搞黑科技」
  「主播万人迷骗了八个豪门网恋翻车」「我在大清当老六」
  「被苗疆少主缠上了」「开局一张图，百鬼跪着喊我姐」
- 名字要能从「我/他/她」的处境里听出冲突，所以第一人称关系词（我妈、我弟、
  婆婆、嫡姐）几乎必带一个。

这个模块只做确定性判分，不调模型：`score()` 给 0–10 分，`issues()` 给可读
问题清单，`acceptable()` 是门禁口径，`hint()` 拼重试提示词。

**两种战法取高分**（榜单同时存在这两类）：
1. `score_relation()` 关系冲突式——世情/短篇主流，要 15–30 字；
2. `score_concept()` 设定卖点式——脑洞/古言/系统/末世，6–30 字靠高概念站住。

**边界（刻意不放过）**：`萤火之城`/`娇养玫瑰`/`京婚有礼`/`心肝宠`/`够野` 这类
4–6 字文艺言情名确实是番茄女频长篇在用的，但那靠的是**已有作者名望 + 长书曝光位**，
不是书名自己在信息流里打架。本项目写的是 8000–12000 字短篇，投的是短故事频道，
书名必须自带冲突信息——所以门禁对这类名字保持 NG。将来若转长篇，把
`acceptable(min_len=4, min_score=3)` 放宽即可，判据不用重写。
"""

from __future__ import annotations

import re

# 关系/身份词：读者的代入锚点。缺了它，书名读起来像题材说明。
_IDENTITY = (
    "我妈", "我爸", "我弟", "我哥", "我姐", "我妹", "我儿子", "我女儿", "老公", "老婆",
    "前夫", "前妻", "初恋", "未婚夫", "青梅", "婆婆", "公公", "小姑", "弟媳", "嫂子",
    "大伯", "舅舅", "表姐", "堂哥", "邻居", "亲戚", "同事", "老板", "保安", "保姆",
    "司机", "老师", "医生", "村长", "全村", "嫡姐", "嫡母", "太后", "皇上", "皇后",
    "娘娘", "王爷", "世子", "小姐", "丫鬟", "太监", "总裁", "财阀", "明星", "主播",
)

# 冲突动词：书名里的「动作」比形容词有效十倍。
_CONFLICT = (
    "撕", "砸", "扔", "卖", "赶", "抢", "夺", "逼", "跪", "告", "骗", "偷", "踹",
    "骂", "打", "绑", "锁", "推", "泼", "烧", "断", "拒", "退", "休", "夺走",
    "离婚", "断绝", "过户", "霸占", "吞掉", "拉黑", "退婚", "休妻", "流放",
    "抄家", "抵押", "拍卖", "抛下", "赶出", "改名", "顶替", "冒领",
)

# 反转连接词：读者的「哦？」时刻。
_REVERSAL = (
    "却", "竟", "居然", "转头", "结果", "直到", "原来", "才知", "谁知", "偏偏",
    "反而", "那天", "当晚", "第二天", "下一秒", "同一天", "回头", "后来",
)

# 高概念/设定卖点：脑洞、古言脑洞、系统流、末世流的书名不靠关系冲突，
# 靠「设定本身有钩子」。真实榜单例：「我在大清当老六」「被苗疆少主缠上了」
# 「穿越后，女主求我别走」「开局一张图，百鬼跪着喊我姐」「港岛73：我爹搞社团我搞黑科技」。
_CONCEPT = (
    "开局", "重生", "穿越", "穿成", "穿书", "系统", "签到", "无限", "满级", "无敌",
    "长生", "提现", "加点", "熟练度", "神豪", "亿万", "百万", "暴富", "破产", "囤",
    "末世", "灾难", "求生", "末日", "诡异", "神祇", "超凡", "序列", "怪谈", "规则",
    "老六", "大佬", "冤种", "团宠", "马甲", "白月光", "替身", "修罗场", "翻车",
    "掉马", "打脸", "火葬场", "追妻", "虐渣", "杀疯", "摆烂", "躺赢", "反向",
    "苗疆", "大清", "民国", "港岛", "深宫", "冷宫", "侯门", "宗门", "灵根", "剑修",
)

# 关系事件词：古言/甜宠书名的「事件」往往是关系变动而不是冲突动作
# （被…缠上、替嫁、赐婚、退婚、换子）。
_EVENT = (
    "缠上", "赖上", "宠上", "娶", "嫁", "退婚", "悔婚", "闪婚", "替嫁", "换嫁",
    "冲喜", "认亲", "抱错", "换子", "夺嫡", "废后", "赐婚", "和亲", "私奔",
    "分家", "过继", "改嫁", "再嫁", "招赘", "入赘",
)

# 设定式的「等级/数量」卖点（第73章、200万、S级、十个师父）
_CONCEPT_NUM_RE = re.compile(
    r"(?:[0-9]+\s*(?:级|万|亿|年|号|层|个|名|座|章)|"
    r"[一二三四五六七八九十百千万两]+\s*(?:个|名|座|级|层|年|万))")

_NUM_RE = re.compile(
    r"[一二三四五六七八九十百千万两0-9]+\s*(?:年|万|块|千|百|天|个月|次|个|岁|层|套|间|斤|口人)")

# 文艺/无信息词：正常书名里出现它们是减分项。
_AIRY = ("时光", "岁月", "温柔", "余生", "离殇", "旧梦", "微光", "诗意", "人间", "山河")

_BAN_WORDS = ("震惊", "惊呆", "竟然", "绝密", "刚刚", "突发", "真相竟是")


def _hit(title: str, words: tuple) -> list[str]:
    return [w for w in words if w in title]


def parts(title: str) -> dict:
    """拆出书名里实际带了几件卖点。"""
    t = (title or "").strip()
    return {
        "identity": _hit(t, _IDENTITY),
        "conflict": _hit(t, _CONFLICT),
        "reversal": _hit(t, _REVERSAL),
        "numbers": _NUM_RE.findall(t),
        "sep": bool(re.search(r"[，,：:]", t)),
        "airy": _hit(t, _AIRY),
        "len": len(t),
    }


def score_relation(title: str) -> int:
    """关系冲突式（世情/短篇主流）：身份 + 冲突动作 + 反转钩，15–30 字。"""
    p = parts(title)
    if not (title or "").strip():
        return 0
    s = 0
    if p["conflict"]:
        s += 3
    if p["identity"]:
        s += 2
    if p["reversal"]:
        s += 2
    if p["numbers"]:
        s += 1
    if p["sep"]:
        s += 1
    if 15 <= p["len"] <= 30:
        s += 1
    if p["len"] < 10:
        s -= 2
    if p["airy"]:
        s -= 2
    return max(0, min(10, s))


def score_concept(title: str) -> int:
    """设定卖点式（脑洞/古言/系统/末世）：高概念 + 数量等级 + 反差，6–30 字。"""
    t = (title or "").strip()
    if not t:
        return 0
    p = parts(t)
    concept = _hit(t, _CONCEPT)
    s = 0
    if concept:
        s += 3
        if len(concept) >= 2:      # 「大清」+「老六」这类双高概念比单个更有卖点
            s += 1
    if _CONCEPT_NUM_RE.search(t):
        s += 2
    if p["reversal"]:
        s += 2
    if _hit(t, _EVENT):
        s += 2
    if p["identity"]:
        s += 1
    if p["sep"]:
        s += 1
    if 6 <= p["len"] <= 30:
        s += 1
    if p["len"] < 6:
        s -= 2
    return max(0, min(10, s))


def family(title: str) -> str:
    """返回书名靠哪种战法站住：relation / concept / none。"""
    a, b = score_relation(title), score_concept(title)
    if max(a, b) < 5:
        return "none"
    return "relation" if a >= b else "concept"


def score(title: str, *, min_len: int = 10, max_len: int = 32) -> int:
    """0–10，取两种战法的较高分。≥5 可发；3–4 建议改；≤2 基本签不了。"""
    return max(score_relation(title), score_concept(title))


def issues(title: str, *, min_len: int = 10, max_len: int = 32) -> list[str]:
    """可读问题清单，直接拼进重试提示。"""
    t = (title or "").strip()
    p = parts(t)
    out: list[str] = []
    if not t:
        return ["书名为空"]
    if p["len"] < min_len:
        out.append(f"只有 {p['len']} 字，太短（4–9 字书名是签约被拒的高频原因；"
                   f"番茄三段式要 15–30 字）")
    elif p["len"] > max_len:
        out.append(f"{p['len']} 字偏长（信息流会截断，压到 30 字内）")
    if not p["conflict"]:
        out.append("没有任何冲突动作（撕/扔/卖/赶/过户/拉黑…），读者看不出发生了什么")
    if not p["identity"]:
        out.append("没有身份或关系锚点（我妈/我弟/婆婆/嫡姐/前夫…），读者代入不进来")
    if not p["reversal"]:
        out.append("没有反转钩（却/转头/直到/原来/那天…），三段式缺最后一折")
    if p["airy"]:
        out.append(f"含文艺空词 {p['airy']}，这类词在榜单书名里几乎不用")
    if any(w in t for w in _BAN_WORDS):
        out.append(f"含标题党词 {[w for w in _BAN_WORDS if w in t]}，会被判低质")
    if "「" in t or "“" in t or '"' in t:
        out.append("含引号，像台词不像书名")
    if t.endswith(("？", "?", "…")) or "……" in t:
        out.append("以问号/省略号结尾是公众号标题写法，书城书名不用")
    return out


def acceptable(title: str, *, min_len: int = 6, max_len: int = 32,
               min_score: int = 5) -> bool:
    """门禁口径：无硬伤 + 任一战法达标 + 长度落在 [min_len, max_len]。"""
    t = (title or "").strip()
    if not (min_len <= len(t) <= max_len):
        return False
    if any(ch in t for ch in "「」“”" + '"') or "……" in t:
        return False
    if t.endswith(("？", "?", "…")):
        return False
    if any(w in t for w in _BAN_WORDS):
        return False
    return max(score_relation(t), score_concept(t)) >= min_score


_NOTES_CACHE: list[str] | None = None


def real_titles(limit: int = 5) -> list[str]:
    """从 `data/market_notes.json` 取真实榜单书名（卖点分达标的才算正例）。

    这份文件是人工从公开页面抄录的实证语料（带来源 URL 与抓取日期），不是编的。
    重试提示必须引用真实样本，模型才知道「15–30 字带冲突的关系句」长什么样。
    """
    global _NOTES_CACHE
    if _NOTES_CACHE is None:
        _NOTES_CACHE = []
        try:
            import json

            from .config import data_path

            d = json.loads(data_path("market_notes.json").read_text(encoding="utf-8"))
            _NOTES_CACHE = [str(t.get("title") or "") for t in (d.get("titles") or [])
                            if score(str(t.get("title") or "")) >= 5]
        except Exception:  # noqa: BLE001
            _NOTES_CACHE = []
    return _NOTES_CACHE[:limit] if limit > 0 else list(_NOTES_CACHE)


def hint() -> str:
    """重试时塞进提示词的卖点公式（简短，不占预算）。"""
    samples = real_titles(5) or [
        "我妈把一百二十万拆迁款全给了我弟，转头让我出十万给她做手术",
        "主播万人迷骗了八个豪门网恋翻车",
    ]
    cited = "、".join(f"「{t}」" for t in samples)
    return (
        "书名不合格。番茄书名的三段式公式：**【身份/关系】+【冲突动作】+【反转钩】**，"
        "15–30 字，用「，」或「：」断开。\n"
        f"正例（真实榜单在更书）：{cited}。\n"
        "硬禁忌：4–9 字短名、纯名词堆叠、文艺空词（时光/岁月/余生/微光/萤火）、"
        "引号、问号或省略号结尾、「震惊/竟然」类标题党词。\n"
        "要求：新书名必须让人一眼看到**谁对谁做了什么、结果反转成什么样**。"
    )
