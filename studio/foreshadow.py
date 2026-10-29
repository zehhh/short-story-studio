# -*- coding: utf-8 -*-
"""伏笔状态机：把「bible.foreshadows 里的一行文字」升级成可校验的债务账本。

借鉴 openwrite B08（`OWC/tools/foreshadowing_manager.py:119-165, 245-268`）的结构，
但按短篇体量裁剪：短篇 8–14 节，伏笔通常 1–4 条，不需要 DAG 图算法，需要的是
**每章写作时知道"本章该埋谁、该收谁"**，以及**终稿时能查出超期未收**。

修复的孤立功能：`bible.json` 的 `foreshadows` 以前只被 `pipeline._chapter_ctx`
拼成一行行静态文字塞进提示词（`studio/pipeline.py:1326-1329`），没有状态、
没有超期判定、没有"本章义务"，模型每章看到的都是同一段全量列表。
写成什么样全靠运气，收没收没人查——典型「声明了但从不校验」。

状态机（与 openwrite 对齐，去掉短篇用不上的层级/权重）：
    open（待收） → paid（已收）
    open → dropped（废弃，需在 bible 里显式标 dropped 才认）
超期：current > payoff_chapter 且状态仍 open。

本模块是纯函数，不调 LLM、不读全局，便于单测。
"""
from __future__ import annotations

from typing import Any

# 状态常量
OPEN = "open"
PAID = "paid"
DROPPED = "dropped"

_STATUS_ALIASES = {
    "": OPEN,
    "open": OPEN,
    "待收": OPEN,
    "埋伏": OPEN,
    "planted": OPEN,
    "paid": PAID,
    "已收": PAID,
    "回收": PAID,
    "resolved": PAID,
    "closed": PAID,
    "dropped": DROPPED,
    "废弃": DROPPED,
    "放弃": DROPPED,
}


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize(bible: dict | None) -> list[dict]:
    """bible.foreshadows → 规范化伏笔列表（补 id/状态/埋收章号，剔非法项）。

    容错是必须的：LLM 生成的 bible 里 plant_chapter 可能写成 "第1章"、"1"、1，
    甚至整条只有 content。这里统一成 int，缺章号按"未声明"处理（None）。
    """
    raw = (bible or {}).get("foreshadows") or []
    out: list[dict] = []
    for i, f in enumerate(raw):
        if not isinstance(f, dict):
            continue
        content = str(f.get("content") or f.get("desc") or f.get("text") or "").strip()
        if not content:
            continue
        status = _STATUS_ALIASES.get(str(f.get("status") or "").strip().lower(),
                                     _STATUS_ALIASES.get(str(f.get("status") or "").strip(), OPEN))
        out.append({
            "id": str(f.get("id") or f"F{i + 1}"),
            "content": content,
            "plant_chapter": _int(f.get("plant_chapter"), 0) or None,
            "payoff_chapter": _int(f.get("payoff_chapter"), 0) or None,
            "status": status,
            "note": str(f.get("note") or ""),
        })
    return out


def chapter_brief(bible: dict | None, no: int) -> str:
    """本章伏笔义务简报：只列「本章该埋 / 本章该收 / 已逾期该补收」。

    旧实现把全量伏笔平铺进提示词，模型分不清哪条和本章有关；
    这里按章号过滤，给出可执行指令，并在没有义务时返回空串（不占 token）。
    """
    items = normalize(bible)
    if not items:
        return ""
    plant_here = [f for f in items if f["plant_chapter"] == no and f["status"] == OPEN]
    pay_here = [f for f in items if f["payoff_chapter"] == no and f["status"] != DROPPED]
    overdue = [f for f in items
               if f["status"] == OPEN and f["payoff_chapter"] and f["payoff_chapter"] < no]
    lines: list[str] = []
    for f in plant_here:
        lines.append(f"- 本章必须自然埋下 {f['id']}「{f['content']}」（计划第{f['payoff_chapter'] or '?'}节回收）")
    for f in pay_here:
        lines.append(f"- 本章必须回收 {f['id']}「{f['content']}」（原计划第{no}节）")
    for f in overdue:
        lines.append(f"- 逾期未收 {f['id']}「{f['content']}」（原定第{f['payoff_chapter']}节，本章是最后补救机会）")
    return "\n".join(lines)


def audit(bible: dict | None, written: dict[int, str] | None = None,
          total: int = 0) -> dict:
    """伏笔账本体检：超期 / 埋而未收 / 回收章无回收痕迹。

    written: {章号: 正文}，用于检查"该收的章里正文是否真的提到了伏笔关键词"。
    关键词取 content 里长度≥2 的中文片段——这是启发式，命不中只报 warning，
    不阻断发布（避免为了确定性把作者逼死）。
    """
    items = normalize(bible)
    written = written or {}
    last = max([total] + [k for k in written]) if (total or written) else 0
    overdue, unpaid, missing_trace, paid_early = [], [], [], []
    for f in items:
        if f["status"] == DROPPED:
            continue
        pay = f["payoff_chapter"]
        if f["status"] == OPEN:
            # 已写到回收章之后仍未收 = 超期；回收章还在后面 = 正常待收。
            # （旧写法两个分支都 append(overdue)，第二个 elif 是死逻辑，这里合并）
            if pay and last and pay <= last:
                overdue.append(f)
            else:
                unpaid.append(f)
        if pay and pay in written and f["status"] != DROPPED:
            body = written.get(pay) or ""
            keys = _keywords(f["content"])
            if keys and not any(k in body for k in keys):
                missing_trace.append(f)
        plant = f["plant_chapter"]
        if f["status"] == PAID and plant and pay and pay < plant:
            paid_early.append(f)
    issues: list[str] = []
    for f in overdue:
        issues.append(f"伏笔 {f['id']}「{f['content']}」超期未收（计划第{f['payoff_chapter']}节，现已写到第{last}节）")
    for f in unpaid:
        issues.append(f"伏笔 {f['id']}「{f['content']}」尚未回收（计划第{f['payoff_chapter'] or '?'}节）")
    for f in missing_trace:
        issues.append(f"伏笔 {f['id']}「{f['content']}」在第{f['payoff_chapter']}节正文里找不到回收痕迹")
    for f in paid_early:
        issues.append(f"伏笔 {f['id']} 回收章({f['payoff_chapter']})早于埋设章({f['plant_chapter']})，设定自相矛盾")
    return {
        "total": len(items),
        "open": sum(1 for f in items if f["status"] == OPEN),
        "paid": sum(1 for f in items if f["status"] == PAID),
        "dropped": sum(1 for f in items if f["status"] == DROPPED),
        "overdue": [f["id"] for f in overdue],
        "missing_trace": [f["id"] for f in missing_trace],
        "issues": issues,
        "ok": not issues,
    }


def _keywords(content: str, n: int = 4) -> list[str]:
    """从伏笔描述里抽可匹配的关键词：优先取 2–6 字的中文实词片段。

    分词不能只靠正则切块——「炕柜底压着的旧存折」切成 ['炕柜底压着的','旧存折']，
    前半段带「着的」这种虚词尾巴，正文里几乎不可能原样出现，等于白判。
    这里按虚词再切一刀，并剔除纯虚词片段。
    """
    import re

    stop_chars = "的了着和与在把被是有个这那"
    segs = re.findall(r"[\u4e00-\u9fa5]{2,6}", content)
    out: list[str] = []
    for s in segs:
        # 按虚词切分，取最长的实词片段
        parts = [p for p in re.split(r"[的了着和与在把被是有个这那]", s) if len(p) >= 2]
        for p in (parts or [s]):
            if len(p) < 2 or all(ch in stop_chars for ch in p):
                continue
            if p not in out:
                out.append(p)
    # 长片段优先（更具体、误报更少），再按出现顺序补齐
    out.sort(key=lambda x: -len(x))
    return out[:n]
