# -*- coding: utf-8 -*-
"""onehu 网页故事存档批量导入知识库。

每篇 HTML：提取标题+正文 → 存原文（workspace/kb/raw_text/）→
生成知识单元（开头钩子：标题+正文前 300 字，arc=起）→ 批量向量化。
断点续跑：单元指纹去重 + 已存原文跳过。deep 参数可对指定篇目做 LLM 深拆。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def extract_html(path: Path) -> tuple[str, str] | None:
    """HTML → (标题, 正文)。返回 None 表示无有效正文。"""
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else path.stem
    title = re.sub(r"\s*[-|]\s*[^-|]{2,12}$", "", title)  # 去 "- 站点名" 尾巴
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", body)
    lines = []
    seen = set()
    for l in text.splitlines():
        l = l.strip()
        if len(l) < 10 or l in seen:          # 去导航/广告/重复模板行
            continue
        if re.match(r"^(首页|上一[章页]|下一[章页]|目录|返回|登录|注册|广告|阅读|点击|收藏|推荐)", l):
            continue
        seen.add(l)
        lines.append(l)
    if len(lines) < 20:
        return None
    # 去模板噪声：标题重复行、章节号重复行、头部站内推荐短行
    t_core = re.sub(r"^\d{1,3}\.\s*", "", title)
    clean = []
    for l in lines:
        core = re.sub(r"^\d{1,3}\.\s*", "", l)
        t_cmp = re.sub(r"^\d{1,3}\.\s*", "", t_core)
        # 与标题互含（≥10 字重叠）判为标题重复行
        if len(core) >= 10 and (t_cmp[:14] in l or core[:14] in t_cmp):
            continue
        if re.match(r"^\d{1,3}\.\s*\S{0,20}$", l) and len(l) < 30:  # 章节号行
            continue
        if len(l) < 15 and len(clean) < 5:                # 头部的短推荐行
            continue
        clean.append(l)
    if len(clean) < 20:
        return None
    start = next((i for i, l in enumerate(clean) if len(l) >= 18), 0)
    return title, "\n\n".join(clean[start:])


def run(cfg: dict, src_dir: str, limit: int = 0) -> dict:
    from . import kb

    src = Path(src_dir)
    files = sorted(src.rglob("*.html"))
    if limit:
        files = files[:limit]
    raw_dir = kb.kb_dir(cfg) / "raw_text"
    raw_dir.mkdir(parents=True, exist_ok=True)

    ok = dup = short = 0
    for i, f in enumerate(files, 1):
        r = extract_html(f)
        if not r:
            short += 1
            continue
        title, body = r
        words = len(re.sub(r"\s", "", body))
        if words < 1500:                       # 太短的非完整故事
            short += 1
            continue
        # 原文落盘（供后续 LLM 深拆）
        raw_path = raw_dir / f"{f.stem}.txt"
        if not raw_path.exists():
            raw_path.write_text(f"《{title}》\n\n{body}", encoding="utf-8")
        # 知识单元：开头钩子（标题+前 300 字，arc=起）
        opening = body[:300]
        u = kb.add_unit(cfg, "lead", f"《{title}》开头：{opening}",
                        arc="起", direction="未分类",
                        tags=["onehu", "故事存档"], source=f"onehu:{f.stem[:12]}")
        if u:
            ok += 1
        else:
            dup += 1
        if i % 200 == 0:
            print(f"    [onehu] 进度 {i}/{len(files)}｜新增 {ok}｜重复 {dup}｜过短 {short}")
    print(f"[onehu] 导入完成：扫描 {len(files)}｜新增单元 {ok}｜重复 {dup}｜过短跳过 {short}")
    return {"scanned": len(files), "added": ok, "dup": dup, "short": short}


def main():
    from .config import load_config

    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".yaml") else None)
    src = "E:/小说/短篇/dataset-forge/data/raw/onehu"
    res = run(cfg, src)
    n = kb_index(cfg)
    print(f"[onehu] 向量化新增 {n} 条")


def kb_index(cfg: dict) -> int:
    from . import kb

    return kb.index_pending(cfg, batch_size=48)


if __name__ == "__main__":
    main()
