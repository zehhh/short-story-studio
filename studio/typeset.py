# -*- coding: utf-8 -*-
"""自动排版引擎：把成稿整理成番茄小说草稿箱规范格式。

排版规则（番茄作家助手 web 端实测约定 + 移动端阅读体验）：
- 章节标题统一为「第XX章 标题」；无章节标记的成稿按 ≤2000 字自动切章；
- 段首不留全角空格（移动端不显示缩进），段与段之间空一行；
- 半角标点转全角（, . ! ? : ;），省略号统一为「……」，破折号统一为「——」；
- 清除 markdown 痕迹（#/…/`/[]()）与多余空行；
- 超长段落（>220 字）检测报告——不自动切，避免破坏节奏，由人决定；
- 输出 chapters=[{no,title,paragraphs}] 结构 + 纯文本草稿，供草稿箱适配器逐章粘贴。
"""
from __future__ import annotations

import re

from .util import SEC_HEAD, sec_head_no, sec_head_title

MAX_SECTION_WORDS = 2000   # 自动切章的单章上限
LONG_PARA = 220            # 超长段落告警阈值

_HALF = str.maketrans({",": "，", ".": "。", "!": "！", "?": "？", ":": "：", ";": "；"})
_SECTION_MARK = re.compile(r"^\s*(?:第\s*([0-9一二三四五六七八九十百零两]+)\s*[章节][\s:：、.．]*)?(.{0,30}?)\s*$")
_NUM_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn2int(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        return int(s)
    if re.fullmatch(r"[0-9]+", s):
        return int(s)
    # 简体中文数字（覆盖 1-999 足够章节号场景）
    total, num = 0, 0
    for ch in s:
        if ch in _NUM_CN:
            num = _NUM_CN[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
    return total + num


def clean_text(text: str) -> str:
    """标点/符号规范化 + markdown 痕迹清理。"""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)          # markdown 标题
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)          # 链接留文字
    text = text.translate(_HALF)
    text = re.sub(r"\.{3,6}|。{3,}|…+", "……", text)               # 省略号
    text = re.sub(r"-{2,}|—{1}|－－", "——", text)                 # 破折号
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return text


def _split_paragraphs(block: str) -> list[str]:
    paras = []
    for raw in re.split(r"\n\s*\n|\n", block):
        p = raw.strip()
        if p:
            paras.append(p)
    return paras


def split_sections(text: str) -> list[tuple[int, str, str]]:
    """按章节标记切分 → [(no, title, body)]；无标记时按字数自动切章。

    节头判据统一取自 `studio.util.SEC_HEAD`（全项目唯一权威源）。旧实现只认
    「第X章…」和「01、标题」两种，而管线产出是「01 标题」（空格分隔）与裸「01」，
    两者都不匹配 → 排版稿永远走"按字数自动切章"退路、标题全空（联动断点）。
    """
    text = clean_text(text)
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []   # (行号, 章节标题)
    for i, ln in enumerate(lines):
        s = ln.strip()
        if SEC_HEAD.match(s):
            marks.append((i, s))
    sections: list[tuple[int, str, str]] = []
    if len(marks) >= 2:
        bounds = [m[0] for m in marks] + [len(lines)]
        prev_no = 0
        for k, (start, title) in enumerate(marks):
            end = bounds[k + 1]
            body = "\n".join(lines[start + 1:end])
            no = sec_head_no(title)
            if no is None or no <= prev_no:
                no = prev_no + 1
            prev_no = no
            t = sec_head_title(title)
            sections.append((no, t.strip(), body))
    if not sections:
        # 自动切章：段落流按字数打包
        paras = _split_paragraphs(text)
        buf, no, cnt = [], 1, 0
        for p in paras:
            buf.append(p)
            cnt += len(p)
            if cnt >= MAX_SECTION_WORDS:
                sections.append((no, "", "\n".join(buf)))
                buf, cnt = [], no + 1
                no += 1
        if buf:
            sections.append((no, "", "\n".join(buf)))
    return sections


def typeset(text: str, book_title: str = "", logline: str = "") -> dict:
    """成稿 → 番茄草稿箱结构。返回 {book:{title,intro}, chapters:[{no,title,content,words}], issues:[]}"""
    secs = split_sections(text)
    chapters, issues = [], []
    for no, title, body in secs:
        paras = [_split_paragraphs(body)]
        flat = [p for grp in paras for p in grp]
        longs = [p for p in flat if len(p) > LONG_PARA]
        content = "\n\n".join(flat)
        words = len(re.sub(r"\s", "", content))
        if longs:
            issues.append(f"第{no}章有 {len(longs)} 个超长段（>{LONG_PARA}字），建议手动拆分")
        if words < 300:
            issues.append(f"第{no}章仅 {words} 字，短于常见单章下限")
        chapters.append({
            "no": no,
            "title": title or f"第{no:02d}章",
            "content": content,
            "words": words,
        })
    return {
        "book": {"title": book_title, "intro": logline},
        "chapters": chapters,
        "issues": issues,
        "total_words": sum(c["words"] for c in chapters),
    }


def draft_text(ts: dict, keep_titles: bool = True) -> str:
    """排版结果 → 可整篇粘贴的纯文本草稿（标题行 + 空行分段）。

    keep_titles 由目标平台决定：
      - True（番茄/七猫）：节头「NN 标题」，读者在目录里看得到标题；
      - False（知乎盐选）：节头纯数字「NN」，盐言规范明确"不加小节标题"。
    旧实现永远输出「NN 标题」，投知乎的项目排版稿节头全部违规——
    而 01_正文.txt 又走 zhihu_format 剥了标题，同一包里两种规范打架。
    """
    out = []
    for c in ts["chapters"]:
        no = f"{c['no']:02d}"
        if not keep_titles:
            head = no
        elif c["title"] and not re.match(r"^\d{1,2}\b", c["title"]):
            head = f"{no} {c['title']}"
        else:
            head = c["title"] or no
        out.append(head.strip())
        out.append("")
        out.append(c["content"])
        out.append("")
    return "\n".join(out).strip() + "\n"


def precheck(text: str) -> list[str]:
    """发布前自检：敏感表述提醒 + 排版问题（确定性规则，不调 LLM）。"""
    issues = list(typeset(text).get("issues", []))
    for pat, why in [
        (r"(自杀|自残)(?:教程|方法|步骤)", "涉自杀方法细节，平台审核高危"),
        (r"(嫖|卖淫|开房)[劫案夜]?细节", "涉色情低俗细节"),
        (r"(领导|警察|官员)(?:都|全)是(?:贪|坏|黑)", "涉政脸谱化群体影射，易触审核"),
    ]:
        if re.search(pat, text):
            issues.append(f"敏感预检：{why}")
    return issues
