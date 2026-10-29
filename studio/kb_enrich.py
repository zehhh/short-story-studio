# -*- coding: utf-8 -*-
"""知识卡内容扩写：把 42 张题材卡从「框架」补到「能直接抄进正文」。

背景（实测）：
    卡片六个字段合计中位数只有 1808 字，`poxi`(婆媳大战) 702 字、
    `pua`(县城职场反杀) 825 字——而写手每章要吃 6—9 条，
    一章就把卡吃空，后面只能重复或空着。这不是"内容少"，是"内容不足以支撑一篇 9000 字"。

所以这里不追求"更多条"，而是**按质量门槛补可用条**：
    1. LLM 扩写 → 2. 确定性质量门（复用 `kb_lint` 判据 + 相似度去重）
    → 3. 只保留通过的条目落盘（附备份与改动记录）。

质量门拒绝的东西就是用户抱怨的四类文字病的上游：
    * 文艺腔（意象/隐喻/岁月/灵魂）→ 写手学去写"月光像刀子"
    * 未核统计（3倍/提升40%）→ 写手当事实写
    * 换词重复（同一信息说两遍）→ 写手把冗余学进正文
    * 过短/过长条目 → 要么无信息量，要么塞不进提示词预算
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from . import kb_lint
from .util import atomic_write_text

# 明显不属于"可写进正文"的文艺腔 / 空转词（比 kb_lint._AIRY 更严，仅用于新增条目）
_BAN_WORDS = ("意象", "隐喻", "象征", "哲学", "岁月", "时光", "灵魂", "远方", "星河", "诗意",
              "宿命", "命运感", "人生的", "生命的", "绽放", "沉吟", "呢喃", "氤氲", "缱绻",
              "浅笑", "薄凉", "暖阳", "陌路", "如刀", "如水", "似水", "宛如", "像刀", "仿佛")

# 每类条目的最小字数：对话天生短（「你这是要把我撵出去啊？」11 字就是好台词），
# 其余字段低于 12 字基本没有可用信息量。
_MIN_LEN = {"facts": 14, "scenes": 12, "dialogues": 9, "hooks": 12, "pitfalls": 12}
_STAT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:倍|%|％)")
_ORDER_RE = re.compile(r"^[0-9一二三四五六七八九十]+[、.．)）]")

_SYSTEM = (
    "你是下沉市场短篇网文（番茄、七猫、知乎盐选）的选题资料编辑，服务对象是写手。"
    "你的产出会被原样塞进写作提示词，写手会照着它写正文。"
    "所以只允许写「能直接变成情节、动作、台词、金额、流程」的东西；"
    "任何一句抒情、比喻、人生感慨、文学评论都算废稿。"
)

_USER_TMPL = """题材方向：{direction}
卡片要诀：{summary}

已有条目（不要重复，也不要换词说同一件事）：
{existing}

请补写下列各类可用素材，每类 {n} 条。硬性要求：

1. 每条 15—90 字，一句话一个点，不加序号、不加引号包裹。
2. facts 必须是可核查的具体事实：金额区间、办理流程、时间节点、常见做法、真实损失。
   禁止写"通常是""一般来说"这类空话；禁止写任何百分比或"N 倍"。
3. scenes 必须是一个能直接开写的镜头：地点 + 具体动作 + 一句能听见的话。
4. dialogues 必须是真人会说的口语：短句、带刺、可以带地方腔（咱/俺/啥/咋），
   不要书面语，不要成语，不要排比。
5. hooks 必须是"第一句话就能钩住人"的开局：具体场景 + 具体冲突 + 一个悬念，
   不许用问句结尾，不许用"当……时"的排比句。
6. pitfalls 必须指出这类题材最容易写坏的**具体做法**（写手能照着避），
   不要写"要真实""要有情感"这种空话。
7. 全篇禁止出现这些词：{ban}
8. 现实题材不得涉及真实地名、真实人名、群体性事件、民族宗教内容。

只输出一个 JSON 对象，键为 facts / scenes / dialogues / hooks / pitfalls，值为字符串数组。
示例（虚构题材「债」的合格条目）：
{{"facts": ["民间借贷月息常见一分到一分五，借一万一年利息一千二到一千八"],
  "scenes": ["他把手机倒扣在饭桌上，说这顿他请，手一直在桌子底下掐着"],
  "dialogues": ["「你借的时候咋不说还不上呢？」"],
  "hooks": ["我给我弟还了八年赌债，他结婚那天把我随礼的两千块退回来了。"],
  "pitfalls": ["别让主角一开始就理直气壮，他得先理亏一次，后面翻盘才爽"]}}"""


def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?「」『』\"'“”‘’]", "", str(s))


def _too_similar(new: str, existing: list[str], threshold: float = 0.72) -> bool:
    a = _norm(new)
    if not a:
        return True
    for old in existing:
        b = _norm(old)
        if not b:
            continue
        if a == b or (len(a) >= 8 and a in b) or (len(b) >= 8 and b in a):
            return True
        if SequenceMatcher(None, a, b).ratio() >= threshold:
            return True
    return False


_UNWRAP = {"「": "」", "『": "』", '"': '"', "“": "”"}


def _unwrap(text: str, keep_quotes: bool = False) -> str:
    """只脱掉「整条就是一个引号包起来的句子」的外壳。

    不能用 `strip("「」\"'“”")`：那会把 `婆婆说：「费那电干啥。」` 的收尾引号一起削掉，
    于是 has_dialog_line 判成"没有台词"，好条目被自己的质量门拒收。
    """
    t = str(text).strip()
    if keep_quotes:  # 对白条目：引号本身就是要交付的东西，不能脱
        return t
    while len(t) > 2 and t[0] in _UNWRAP and t[-1] == _UNWRAP[t[0]]:
        inner = t[1:-1]
        if any(c in inner for c in "「『“\""):
            break  # 内部还有别的引号，说明外壳是正文的一部分，不能脱
        t = inner.strip()
    return t


def accept(item: str, field: str, existing: list[str]) -> tuple[bool, str]:
    """单条质量门：返回 (是否可用, 拒绝原因)。"""
    t = _unwrap(item, keep_quotes=(field == "dialogues"))
    if not t:
        return False, "空"
    if _ORDER_RE.match(t):
        t2 = _ORDER_RE.sub("", t).strip()
        if not t2:
            return False, "只有序号"
        t = t2
    if any(m in t for m in kb_lint._PENDING):
        return False, "未核标记"
    if _STAT_RE.search(t):
        return False, "未核统计"
    if any(w in t for w in _BAN_WORDS):
        return False, "文艺腔"
    lo, hi = kb_lint._FIELD_LEN.get(field, (12, 140))
    lo = max(lo, _MIN_LEN.get(field, 12))
    if len(t) < lo:
        return False, f"过短({len(t)}字)"
    if len(t) > hi:
        return False, f"过长({len(t)}字)"
    if field == "dialogues" and kb_lint.is_vague_dialog(t):
        # 旧判据 `[？?!！。]$` 让「婆婆与儿媳关于带娃的争执。」这类概述过关，
        # 结果是 46% 的对白条目没有一句台词（写手拿到只能自己编）。
        return False, "对白没有台词"
    if _too_similar(t, existing):
        return False, "与已有条目重复"
    return True, ""


def enrich_card(llm, card: dict, per_field: int = 6, max_tries: int = 2) -> dict:
    """扩写一张卡。返回 {card, added:{field:[...]}, rejected:{field:[[item,why],...]}}。"""
    direction = str(card.get("direction") or card.get("id") or "")
    existing_map = {f: [str(x) for x in (card.get(f) or [])] for f in
                    ("facts", "scenes", "dialogues", "hooks", "pitfalls")}
    existing_txt = "\n".join(
        f"[{f}] " + " / ".join(existing_map[f][:10]) for f in existing_map if existing_map[f]
    ) or "（无）"
    user = _USER_TMPL.format(direction=direction, summary=str(card.get("summary") or "")[:200],
                             existing=existing_txt[:1800], n=per_field,
                             ban="、".join(_BAN_WORDS[:12]))
    added: dict[str, list[str]] = {f: [] for f in existing_map}
    rejected: dict[str, list[list[str]]] = {f: [] for f in existing_map}
    tries = 0
    while tries < max_tries and sum(len(v) for v in added.values()) < per_field * len(added):
        tries += 1
        try:
            data = llm.chat_json(_SYSTEM, user)
        except Exception as exc:  # noqa: BLE001
            rejected["facts"].append([f"<LLM 失败：{exc}>", "json"])
            break
        if not isinstance(data, dict):
            continue
        deficit = {f: per_field - len(added[f]) for f in added}
        for field in added:
            if deficit[field] <= 0:
                continue
            pool = data.get(field) or []
            if not isinstance(pool, list):
                continue
            for raw in pool:
                if deficit[field] <= 0:
                    break
                ok, why = accept(raw, field, existing_map[field] + added[field])
                if not ok:
                    rejected[field].append([str(raw)[:60], why])
                    continue
                added[field].append(str(raw).strip().strip("「」\"'“”"))
                deficit[field] -= 1
        if sum(len(v) for v in added.values()) >= per_field * len(added):
            break
        # 没补满：把已收的条目从提示里剔除再要一轮
        user = user.replace(existing_txt[:1800], existing_txt[:1200] + " / " +
                            " / ".join(sum((v[:3] for v in added.values()), [])))
    new_card = dict(card)
    for field, items in added.items():
        if items:
            new_card[field] = list(card.get(field) or []) + items
    return {"card": new_card, "added": added, "rejected": rejected}


def enrich_all(cfg: dict, directions: list[str] | None = None, per_field: int = 6,
               backup_dir: Path | str = "out/kb_enrich_backup", dry_run: bool = False) -> dict:
    """批量扩写。返回统计；落盘前先备份，只写有新增的卡。dry_run=True 时只统计不落盘。"""
    from .config import data_path
    from .llm import get_llm, resolve_llm

    root = data_path("knowledge")
    bak = Path(backup_dir)
    bak.mkdir(parents=True, exist_ok=True)
    base = get_llm(cfg)
    llm = resolve_llm(cfg, base, "light")
    paths = sorted(root.glob("*.json"))
    if directions:
        want = {d.strip() for d in directions if d.strip()}
        paths = [p for p in paths if p.stem in want or
                 json.loads(p.read_text(encoding="utf-8")).get("direction") in want]
    stat = {"cards": 0, "added": 0, "rejected": 0, "per_field": per_field, "detail": []}
    for p in paths:
        card = json.loads(p.read_text(encoding="utf-8"))
        res = enrich_card(llm, card, per_field=per_field)
        n_added = sum(len(v) for v in res["added"].values())
        n_rej = sum(len(v) for v in res["rejected"].values())
        stat["rejected"] += n_rej
        if not n_added:
            stat["detail"].append({"card": p.stem, "added": 0, "rejected": n_rej})
            continue
        if not (bak / p.name).exists():
            atomic_write_text(bak / p.name, json.dumps(card, ensure_ascii=False, indent=2) + "\n")
        out = res["card"]
        out["_enriched_at"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
        if not dry_run:
            atomic_write_text(p, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        stat["cards"] += 1
        stat["added"] += n_added
        stat["detail"].append({"card": p.stem, "added": n_added, "rejected": n_rej,
                               "by": {k: len(v) for k, v in res["added"].items()},
                               "sample": {k: v[:2] for k, v in res["added"].items() if v}})
    return stat


# ─────────────────────────── 改写"抽掉壳"的条目 ───────────────────────────
# 为什么需要第二轮：扩写（enrich）只是往卡里**追加**好料，卡里原有的一批条目
# 是"标题式概括"——「深夜喂奶时婆媳育儿理念冲突」「老人偏心引发的家庭财产矛盾」。
# 它们字面和查询词重合度高，选材时反而排在具体条目前面，写手照着只能自己编空话，
# 这正是成稿「细节详略不当、同一信息反复说」的一个上游成因。与其剔除（会丢覆盖），
# 不如就地改写成同等信息的可写素材。

_THIN_MEANING = {
    "facts": "真实细节/钱数行情——要金额区间、办理流程、时间节点、真实损失",
    "scenes": "可写场景——要地点 + 具体动作 + 一句能听见的话",
    "hooks": "开头钩子——要具体场景 + 具体冲突 + 一个悬念",
    "pitfalls": "读者雷点——要指出具体做法",
    "dialogues": "可抄台词——要一句带刺、带地方腔、能听见情绪的原话（必须有引号）",
}
_THIN_FIELDS = ("facts", "scenes", "hooks", "dialogues")


def _weak_items(items: list, field: str, min_score: int) -> list[int]:
    """返回需要改写的下标：具体度不足；dialogues 另外要求「必须有一句台词」。"""
    out = []
    for i, t in enumerate(items):
        txt = str(t)
        weak = kb_lint.score_item(txt, field) < min_score
        if field == "dialogues" and kb_lint.is_vague_dialog(txt):
            weak = True
        if weak:
            out.append(i)
    return out

_THIN_SYSTEM = (
    "你是下沉市场短篇网文（番茄、七猫、知乎盐选）的选题资料编辑。"
    "你的任务是把一条'概括'改写成写手能直接抄进正文的具体素材。"
    "只输出能变成情节、动作、台词、金额、流程的东西；抒情、比喻、人生感慨一律算废稿。"
)

_THIN_TMPL = """题材方向：{direction}
卡片要诀：{summary}
字段类型：{meaning}

下面这些条目写得太概括，写手拿到只能自己编空话。请逐条改写，保留原来的信息点，
但把它落成具体的东西：
{items}

硬性要求：
1. 每条 15—90 字，一句话一个点，不加序号、不加引号包裹。
2. 不要写成剧情大纲，要写成"能直接用的素材"（金额/流程/动作/台词/场景）。
3. 禁止出现这些词：{ban}
4. 不许与下列已有条目重复或换词重说：{existing}
5. 现实题材不得涉及真实地名、真实人名、群体性事件。

只输出一个 JSON 对象：{{"items": ["改写1", "改写2", ...]}}，条数与顺序必须与输入一致。
"""


def rewrite_thin(llm, card: dict, min_score: int = 2, fields: tuple[str, ...] = _THIN_FIELDS,
                 batch: int = 6, max_tries: int = 2) -> dict:
    """把具体度低于 min_score 的条目就地改写成可写素材。返回 {card, changed, rejected}。"""
    from . import kb_lint

    out = {f: list(card.get(f) or []) for f in
           ("facts", "scenes", "dialogues", "hooks", "pitfalls", "redlines")}
    direction = str(card.get("direction") or card.get("id") or "")
    summary = str(card.get("summary") or "")[:200]
    ban = "、".join(_BAN_WORDS[:12])
    changed: list[list[str]] = []
    rejected: list[list[str]] = []
    for field in fields:
        items = out.get(field) or []
        weak = _weak_items(items, field, min_score)
        for start in range(0, len(weak), batch):
            idxs = weak[start:start + batch]
            src = [str(items[i]) for i in idxs]
            user = _THIN_TMPL.format(
                direction=direction, summary=summary,
                meaning=_THIN_MEANING.get(field, field),
                items="\n".join(f"{n + 1}. {t}" for n, t in enumerate(src)),
                ban=ban,
                existing=" / ".join(str(x) for x in items if str(x) not in src)[:900] or "（无）")
            got: list[str] = []
            for _try in range(max_tries):
                try:
                    data = llm.chat_json(_THIN_SYSTEM, user)
                except Exception:  # noqa: BLE001
                    break
                pool = (data or {}).get("items") if isinstance(data, dict) else None
                if isinstance(pool, list) and len(pool) >= len(src):
                    got = [str(x) for x in pool[:len(src)]]
                    break
            for pos, i in enumerate(idxs):
                old = str(items[i])
                new = (_unwrap(got[pos], keep_quotes=(field == "dialogues"))
                       if pos < len(got) else "")
                ok, why = accept(new, field, [str(x) for x in items if str(x) != old])
                if not ok:
                    rejected.append([old[:40], why])
                    continue
                items[i] = new
                changed.append([old, new])
            out[field] = items
    new_card = dict(card)
    for f in out:
        new_card[f] = out[f]
    return {"card": new_card, "changed": changed, "rejected": rejected}


def rewrite_all(cfg: dict, directions: list[str] | None = None, min_score: int = 2,
                backup_dir: Path | str = "out/kb_rewrite_backup", dry_run: bool = False) -> dict:
    """批量改写"抽掉壳"的条目。返回统计；落盘前先备份。"""
    from .config import data_path
    from .llm import get_llm, resolve_llm

    root = data_path("knowledge")
    bak = Path(backup_dir)
    bak.mkdir(parents=True, exist_ok=True)
    llm = resolve_llm(cfg, get_llm(cfg), "light")
    paths = sorted(root.glob("*.json"))
    if directions:
        want = {d.strip() for d in directions if d.strip()}
        paths = [p for p in paths if p.stem in want or
                 json.loads(p.read_text(encoding="utf-8")).get("direction") in want]
    stat = {"cards": 0, "changed": 0, "rejected": 0, "min_score": min_score, "detail": []}
    for p in paths:
        card = json.loads(p.read_text(encoding="utf-8"))
        res = rewrite_thin(llm, card, min_score=min_score)
        if not res["changed"]:
            stat["rejected"] += len(res["rejected"])
            continue
        if not (bak / p.name).exists():
            atomic_write_text(bak / p.name, json.dumps(card, ensure_ascii=False, indent=2) + "\n")
        out = res["card"]
        out["_rewritten_at"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
        if not dry_run:
            atomic_write_text(p, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        stat["cards"] += 1
        stat["changed"] += len(res["changed"])
        stat["rejected"] += len(res["rejected"])
        stat["detail"].append({"card": p.stem, "changed": len(res["changed"]),
                               "rejected": len(res["rejected"]),
                               "sample": res["changed"][:2]})
    return stat
