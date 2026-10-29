# -*- coding: utf-8 -*-
"""POV / 主角名字一致性（零 LLM 成本的确定性检查）。

第一人称短篇在 hybrid 长上下文续写时，模型容易"人称漂移"：
- 把主角的动作/心理写成第三人称（真名"陶枝"或化名"陶三"做主语），
  而第一人称小说里这些只能用"我"；
- 甚至把化名记错（"陶三"写成"陶枝"）。

本模块只做：
1. 从故事圣经提取主角真名 / 化名 / 叙述人称；
2. 剥离对话后，检测叙述中是否出现主角名字（第一人称小说不该出现）；
3. 确定性把叙述中的主角名字改为"我"（对话里别人称呼主角的名字保留）。
"""
from __future__ import annotations

import re

# 叙述里常见的身份前缀（"化名散修陶三"里的"散修"）
_ID_PREFIX = r"(?:散修|弟子|外门|内门|杂役|凡人|女修|剑修|俗家|小)"
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"))
# 节头行（"03 杂役陶三的扫地功" / "## 第3章"），标题里出现主角名合法
_SEC_HEAD = re.compile(
    r"^#{0,2}\s*(?:第\s*[0-9一二三四五六七八九十百零两]+\s*[章节]|[0-9]{1,2})\s*[:：、.．]?\s*"
)


def _strip_head(body: str) -> str:
    """去掉每节首行的节头（标题里的主角名不算漂移）。"""
    lines = body.split("\n")
    if lines and _SEC_HEAD.match(lines[0].strip()):
        return "\n".join(lines[1:])
    return body


def protagonist(bible: dict) -> dict | None:
    """从 bible 提取主角信息：{real, aliases, first_person}。

    real=真名；aliases=化名（伪装身份）；first_person=是否第一人称叙述。
    """
    chars = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    main = next((c for c in chars if "主角" in str(c.get("role", ""))), None)
    if not main:
        return None
    real = str(main.get("name", "")).strip()
    tone = bible.get("tone") or {}
    person = str(tone.get("person", "")) if isinstance(tone, dict) else str(tone)
    first_person = "第一" in person
    # 在 occupation/secret/arc 里找"化名（散修）XX"，非贪婪并在动作词前停止
    # （"化名散修陶三回宗" → 陶三，不是"陶三回宗"）
    blob = " ".join(str(main.get(k, "")) for k in ("occupation", "secret", "arc", "look", "traits"))
    _tail = r"(?=回|来|去|身|复|报|混|进|入|的|，|。|$|→|！|、)"
    aliases: list[str] = []
    for m in re.finditer(
        r"化名[^\u4e00-\u9fa5]{0,3}" + _ID_PREFIX + r"?([\u4e00-\u9fa5]{2,4}?)" + _tail,
        blob,
    ):
        a = m.group(1).strip()
        if a and a != real and a not in aliases:
            aliases.append(a)
    # "以/用 XX（身份）混/回"
    for m in re.finditer(r"(?:假身份|假名叫|身份叫|名字叫)[「『]?([\u4e00-\u9fa5]{2,4})", blob):
        a = m.group(1).strip()
        if a and a != real and a not in aliases:
            aliases.append(a)
    return {"real": real, "aliases": aliases, "first_person": first_person}


def all_names(proto: dict) -> list[str]:
    """主角的全部名字（真名 + 化名），长的在前，避免短名误匹配。"""
    names = [n for n in ([proto.get("real")] + proto.get("aliases", [])) if n]
    return sorted(set(names), key=len, reverse=True)


def _mask_quoted(text: str) -> str:
    """把对话内容（含引号、不跨行）替换为全角空格，返回纯叙述文本。"""
    out = text
    for op, cl in _QUOTE_PAIRS:
        pat = re.compile(re.escape(op) + r"[^" + re.escape(cl) + r"\n]*" + re.escape(cl))
        out = pat.sub(lambda m: "　" * len(m.group(0)), out)
    return out


def drift_names(body: str, proto: dict) -> list[str]:
    """第一人称小说：返回叙述中（非对话、非节头）出现的主角名字。"""
    if not proto.get("first_person"):
        return []
    masked = _mask_quoted(_strip_head(body))
    found = []
    for n in all_names(proto):
        if n in masked:
            found.append(n)
    return found


def drift_severity(body: str, proto: dict) -> str:
    """第一人称小说的人称漂移程度：'none' | 'light' | 'heavy'。

    - heavy：整节切换到第三人称（叙述里"我"几乎为 0，名字成片出现），
      残留的"她"需要语义判断，确定性改不干净，必须整节重写；
    - light：以"我"为主、只是偶尔句首误用名字，可确定性替换。
    """
    if not proto.get("first_person"):
        return "none"
    masked = _mask_quoted(_strip_head(body))
    n_names = sum(masked.count(n) for n in all_names(proto))
    if n_names == 0:
        return "none"
    n_wo = masked.count("我")
    return "heavy" if n_wo < n_names else "light"


def _fix_line(line: str, names: list[str]) -> str:
    """替换一行内、对话区间外的主角名字为'我'。"""
    # 先记录对话字符区间
    protect = [False] * len(line)
    for op, cl in _QUOTE_PAIRS:
        for m in re.finditer(re.escape(op) + r"[^" + re.escape(cl) + r"]*" + re.escape(cl), line):
            for i in range(m.start(), m.end()):
                protect[i] = True
    chars = list(line)
    for n in names:
        i = 0
        while i <= len(chars) - len(n):
            if "".join(chars[i:i + len(n)]) == n and not any(protect[i:i + len(n)]):
                # 所有格："陶枝的" → "我的"
                if i + len(n) < len(chars) and chars[i + len(n)] == "的":
                    repl = "我"
                else:
                    repl = "我"
                # 重新对齐保护区间长度（名字长度可能 >1，"我"是 1 字）
                chars[i:i + len(n)] = list(repl)
                # 压缩保护区间
                protect = protect[:i] + protect[i:i + len(repl)] + protect[i + len(n):]
                i += len(repl)
            else:
                i += 1
    return "".join(chars)


def fix_drift(body: str, proto: dict) -> tuple[str, int]:
    """第一人称小说：把叙述中的主角名字改为'我'。返回 (新文本, 替换处数)。

    只在确实发生漂移时调用。
    """
    if not proto.get("first_person"):
        return body, 0
    names = all_names(proto)
    if not names:
        return body, 0
    before = sum(body.count(n) for n in names)
    out = "\n".join(_fix_line(ln, names) for ln in body.split("\n"))
    # 对话里的名字不算
    after = sum(_mask_quoted(out).count(n) for n in names)
    return out, max(0, before - after)
