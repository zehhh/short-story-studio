# -*- coding: utf-8 -*-
"""通用工具函数。"""
from __future__ import annotations

import json
import re
import sys
import time
import datetime as _dt
from pathlib import Path


def patch_console(errors: str = "replace") -> None:
    """让 print 在 GBK 控制台下不再崩。

    历史缺陷（2026-10-26 实测）：Windows 简体中文控制台默认 cp936，
    `print("⚠ ...")` 抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0'`。
    这类字符散布在 agents/pipeline 的状态行里（⚠/✔/✘），而它们只在"出问题"时才打印——
    于是**一旦真出问题，程序就崩在报错语句上**，把真正的缺陷盖成编码错误，
    整条 run 直接丢稿。改成 errors="replace"：符号降级成 `?`，信息照打，不再崩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors=errors)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 —— 非 TextIOWrapper（被重定向/测试替身）
            pass


patch_console()


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def date_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def slugify(text: str, max_len: int = 24) -> str:
    """中文标题转安全文件名片段。"""
    text = re.sub(r"[\\/:*?\"<>|\s#%&@+={}$!'`,.。，！？；：（）()【】\[\]]+", "", text)
    return text[:max_len] or "untitled"


def count_words_zh(text: str) -> int:
    """中文字数统计：按非空白字符计，近似稿费口径。"""
    return len(re.sub(r"\s+", "", text))


_KEY_JUNK_HEAD = re.compile(r"^[\s，。、；：？！,.;:?!\"'“”‘’（）()\[\]{}·—\-_]+")
_KEY_JUNK_TAIL = re.compile(r"[\s，。、；：？！,.;:?!\"'“”‘’（）()\[\]{}·—\-_]+$")


def clean_json_keys(obj):
    """递归规整 JSON 对象键：去掉键首尾的中英文标点/空白。

    为什么需要：deepseek 系列在中文语境下会把每个键写成 `"，id"`、`"，name"`
    （把中文逗号当成分隔符带进了键名）。JSON 本身合法，`json.loads` 不报错，
    但下游 `raw.get("id")` 全部取不到值 —— 实测 e2e6 第 05 章，主编返回的 10 项
    评分卡全部因此被判「未评估」，覆盖率只剩 6%、整章变成「未评级」，
    而模型其实认真打了分、还摘了正文原句当证据。这种脏键必须在解析层统一修，
    不能靠每个消费方自己 `get("id") or get("，id")`。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = _KEY_JUNK_TAIL.sub("", _KEY_JUNK_HEAD.sub("", str(k))) or str(k)
            if nk in out and nk != k:
                continue  # 清洗后与已有键撞名：保留先到的那个
            out[nk] = clean_json_keys(v)
        return out
    if isinstance(obj, list):
        return [clean_json_keys(x) for x in obj]
    return obj


def extract_json(text: str):
    """从 LLM 输出中稳健提取 JSON（容忍 markdown 代码块、前后缀废话、尾逗号）。

    返回解析后的对象；失败抛 ValueError。
    """
    if text is None:
        raise ValueError("空输出")
    s = text.strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return clean_json_keys(json.loads(s))
    except json.JSONDecodeError:
        pass

    # 平衡扫描截取第一个完整 JSON 值
    candidate = None
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    break
        if candidate:
            break
    if candidate is None:
        raise ValueError("输出中未找到 JSON 结构:\n" + s[:500])

    # 逐步尝试：原样 / 去尾逗号 / 单引号转双引号
    attempts = [
        candidate,
        re.sub(r",\s*([\]}])", r"\1", candidate),
    ]
    if '"' not in candidate and "'" in candidate:
        attempts.append(re.sub(r",\s*([\]}])", r"\1", candidate.replace("'", '"')))
    for a in attempts:
        try:
            return clean_json_keys(json.loads(a))
        except json.JSONDecodeError:
            continue
    raise ValueError("JSON 解析失败:\n" + candidate[:500])


class RateLimiter:
    """简单限速器：保证两次调用间隔不小于 min_interval 秒。"""

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.time()


# ---------------------------------------------------------------------------
# 小节头「NN」契约（**全项目唯一权威源**）
#
# 一处定义、到处引用：pipeline（切节/装配/清理）、typeset（排版切节）、
# smoke_test（契约回归）都从这里取，避免"同一个节头三套判据"——
# 历史上 pipeline 认「01 标题」而 typeset 只认「01、标题」，导致排版稿
# 永远走"按字数自动切章"退路、标题全空。
#
# 判定必须严格：整稿里任何"以 1-2 位数字开头的行"都不能算节头，否则正文里的
# 「3 年了，她一直没回来」「4月5日夜里…」会被切成小节碎片。历史事故：
# 一份 9000 字稿被误切成 400+ 节，字数门/润色按节号批量回写，最终成稿膨胀到 9.9 万字。
# 约束（缺一不可）：
#   ① 整行 ≤26 字符（标题行不会长）；② 编号后不能紧跟数字；
#   ③ 标题不得含句末标点 。！？； ④ 标题不得含英文单词（≥2 连续字母，多为模型泄漏）；
#   ⑤ 标题非空时必须含汉字；⑥ 标题不得以逗号/顿号/冒号结尾（散文片段特征）。
# 同时接受「01」「01 标题」「## 01 标题」「01、标题」「第1章 标题」五种形态。
SEC_HEAD = re.compile(
    r"^(?=.{1,26}$)"
    r"#{0,2}[ \t]*"
    r"(?:第\s*[0-9一二三四五六七八九十百零两]+\s*[章节]|0?\d{1,2})(?!\d)"
    r"[ \t]*[:：、.．]?[ \t]*"
    # 数字后紧跟量词/时间单位 = 正文里的数字主语（「24 岁那年」「58 把残剑」「3 块五」），
    # 不是节头。历史事故：这条缺守卫时一句「24 岁那年…」把整篇切成 393 个碎片节。
    r"(?![ \t]*[岁把块元年月日号条张次天分秒米斤层间位名台辆本页段行字句人万千百十点倍成折])"
    r"(?:(?![^\n]*[。！？；])(?![^\n]*[A-Za-z]{2})(?=[^\n]*[\u4e00-\u9fff])"
    r"[^\n]*[^，,、：:])?"
    r"$"
)

# 节头里的标题部分（保留标题的平台排版稿 / 切节时取 title 用）
SEC_HEAD_TITLE = re.compile(
    r"^#{0,2}[ \t]*(?:第\s*[0-9一二三四五六七八九十百零两]+\s*[章节]|0?\d{1,2})"
    r"[ \t]*[:：、.．]?[ \t]*(?P<title>.*)$"
)

# 单篇小节数上限：超过即判定节头识别失效（正常短篇 4-15 节，极端组合题材 <30）。
# 该守卫是「失控扩写」事故的最后一道闸：宁可退回整稿模式，也不能按错误节号批量回写。
MAX_SECTIONS = 60


def is_sec_head(line: str) -> bool:
    """整行是否为小节头（SEC_HEAD 的可读别名，供新代码使用）。"""
    return bool(SEC_HEAD.match((line or "").strip()))


_HEAD_NO = re.compile(
    r"^#{0,2}[ \t]*(?:第\s*([0-9一二三四五六七八九十百零两]+)\s*[章节]|0?(\d{1,2}))"
)
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def _cn_int(s: str):
    """中文数字转 int（只处理短篇节号会用到的 1–99：一 … 九十九）。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "百" in s:
        return None
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGIT:
            return None
        if tail and tail not in _CN_DIGIT:
            return None
        return tens * 10 + ones
    return _CN_DIGIT.get(s)


def sec_head_no(line: str):
    """从节头行解析小节号；解析不到返回 None。

    判据**必须**先过 SEC_HEAD 才解析编号——此前这里自带一份更宽松的正则
    （`^\\d{1,2}` 即可），于是「3月18号，周一，晚上九点。……」这种日期开头的
    正文行被当成节头，全稿被切出一个幻影小节（e2e5 实测：7 节变 8 节、
    节长极差从 1.39× 虚报成 2.99×、平台分档判 FAIL）。
    """
    s = (line or "").strip()
    if not s or not SEC_HEAD.match(s):
        return None
    m = _HEAD_NO.match(s)
    if not m:
        return None
    if m.group(2):
        return int(m.group(2))
    return _cn_int(m.group(1))


def sec_head_title(line: str) -> str:
    """从节头行取标题（无标题返回空串）。"""
    m = SEC_HEAD_TITLE.match((line or "").strip())
    return (m.group("title").strip() if m else "")


# ---------------------------------------------------------------------------
# 原子写（借鉴 openwrite 的 B02：临时文件 + fsync + replace）
#
# 背景：长任务被强杀（Ctrl-C / 进程回收 / 断电）时，直接 write_text 会留下
# 半截 JSON 或半截章节文件，断点续跑读到坏 JSON 直接崩，整轮产出作废。
# 原子写保证"要么是旧内容，要么是完整新内容"。
# ---------------------------------------------------------------------------

def atomic_write_text(path, text: str) -> Path:
    """原子写文本：同目录临时文件 → fsync → os.replace。

    同目录保证 replace 在同一文件系统内是原子操作；失败时清理临时文件，
    不影响原文件。
    """
    import os
    import tempfile

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def atomic_write_json(path, obj, indent: int = 2) -> Path:
    """原子写 JSON（ensure_ascii=False，便于人工核对中文产物）。"""
    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))
