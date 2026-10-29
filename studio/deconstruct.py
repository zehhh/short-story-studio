# -*- coding: utf-8 -*-
"""拆书：把一本（部）小说/故事拆成结构化知识，沉淀进知识体系。

流程（参考行业拆书方法论：每章核心目标/开篇钩子/核心冲突/爽点虐点/章末悬念 + 起承转合 + 节奏标记）：
  1. 分段：识别 01/第X章/数字编号 等分段标记；无标记则按段落体积分块；
  2. 逐段提取（LLM）：核心目标/开篇钩子/核心冲突/爽点/虐点/写作技法/可复用金句；
  3. 全书归纳（LLM）：题材判断/起承转合结构模板/节奏曲线/技法清单/标题公式/可复用教训；
产物：
  - workspace/decon/<书名>.json（结构化拆解，自动进入 RAG 索引）
  - 素材库入库：钩子→章末钩子、爽点→情绪爆点、金句→金句、技法→写作技法（打"拆书"标签）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import ROOT
from .util import count_words_zh, now_ts

SECTION_MARK = re.compile(
    r"^\s*(?:第\s*[\d一二三四五六七八九十百千零两]+\s*[章节回]|[\d０-９]{1,3}\s*[、.．:：]?\s*$|"
    r"[（(]?\d{1,3}[)）]?\s*$|Chapter\s+\d+)",
    re.I,
)


def split_sections(text: str) -> list[dict]:
    """按分段标记切块；无标记时按段落体积分块（约 2200 字/块）。"""
    text = text.replace("\r\n", "\n").replace("\uFEFF", "").strip()
    lines = text.split("\n")
    marks = [i for i, l in enumerate(lines) if SECTION_MARK.match(l.strip())]
    sections: list[dict] = []
    if len(marks) >= 2:
        bounds = marks + [len(lines)]
        for k in range(len(marks)):
            seg = lines[bounds[k] : bounds[k + 1]]
            title = seg[0].strip()
            body = "\n".join(seg[1:]).strip()
            if count_words_zh(body) >= 80:
                sections.append({"title": title[:40], "text": body})
    if not sections:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        buf, size = [], 0
        for p in paras:
            buf.append(p)
            size += len(p)
            if size >= 2200:
                sections.append({"title": "", "text": "\n\n".join(buf)})
                buf, size = [], 0
        if buf:
            sections.append({"title": "", "text": "\n\n".join(buf)})
    return sections


def decon_dir(cfg: dict) -> Path:
    d = Path(cfg["studio"]["workspace_abs"]) / "decon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]", "", title)[:40] or "untitled"


def deconstruct_file(cfg: dict, llm, path: str | Path, title: str = "",
                     max_sections: int = 24, per_call: int = 2,
                     deposit_material: bool = True) -> dict:
    """拆一本书/一篇故事。llm: BaseLLM 实例（走 planner 角色）。

    deposit_material=False 时只写拆解 JSON + 知识向量库，不向素材库淹没式写入（批量拆书用）。
    """
    from .prompts import render_prompt

    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    sections = split_sections(text)[:max_sections]
    if not sections:
        raise SystemExit(f"{path.name}：未能切分出任何段落")
    if not title:
        title = path.stem

    sec_records = []
    for i in range(0, len(sections), per_call):
        chunk = sections[i : i + per_call]
        total_secs = len(sections)
        listing = "\n\n".join(
            f"【段落 {i + k + 1}｜全书位置约 {min(100, int((i + k) / max(1, total_secs - 1) * 100))}%】"
            f"{c.get('title', '')}\n{c['text'][:2400]}" for k, c in enumerate(chunk)
        )
        sys_p, user_p = render_prompt(
            "decon_section", BOOK_TITLE=title, LISTING=listing, N=len(chunk)
        )
        try:
            data = llm.chat_json(sys_p, user_p)
            rows = data.get("sections", data if isinstance(data, list) else [])
        except Exception as e:  # noqa: BLE001
            print(f"    [decon] 第 {i + 1}-{i + len(chunk)} 段解析失败，跳过：{e}")
            continue
        for k, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            no = i + k + 1
            pos = no / max(1, len(sections))            # 位置比例（规则兜底用）
            arc = (row.get("arc") or "").strip()[:2]
            if arc not in ("起", "承", "转", "合"):
                arc = "起" if pos <= 0.20 else "承" if pos <= 0.55 else "转" if pos <= 0.85 else "合"
            sec_records.append(
                {
                    "no": no,
                    "arc": arc,
                    "arc_role": (row.get("arc_role") or "")[:40],
                    "lead_pattern": (row.get("lead_pattern") or "")[:30],
                    "title": chunk[k].get("title", "") if k < len(chunk) else "",
                    "goal": row.get("goal", ""),
                    "hook": row.get("hook", ""),
                    "hook_type": (row.get("hook_type") or "")[:8],
                    "conflict": row.get("conflict", ""),
                    "escalation": (row.get("escalation") or "")[:80],
                    "emotion": (row.get("emotion") or "")[:24],
                    "shuang": row.get("shuang", ""),
                    "shuang_type": (row.get("shuang_type") or "")[:8],
                    "nue": row.get("nue", ""),
                    "info_gap": (row.get("info_gap") or "")[:120],
                    "persona": (row.get("persona") or "")[:80],
                    "technique": row.get("technique", ""),
                    "quote": row.get("quote", ""),
                }
            )
        print(f"    [decon] 段落 {min(i + per_call, len(sections))}/{len(sections)} 完成")

    brief = "\n".join(
        f"第{s['no']}段（{s.get('title', '')}）：目标={s.get('goal', '')}｜钩子[{s.get('hook_type', '')}]={s.get('hook', '')}｜"
        f"冲突={s.get('conflict', '')}｜升级={s.get('escalation', '')}｜情绪={s.get('emotion', '')}｜"
        f"爽[{s.get('shuang_type', '')}]={s.get('shuang', '')}｜虐={s.get('nue', '')}｜"
        f"信息差={s.get('info_gap', '')}｜人设={s.get('persona', '')}"
        for s in sec_records
    )
    sys_b, user_b = render_prompt("decon_book", BOOK_TITLE=title, BRIEF=brief[:12000])
    try:
        book = llm.chat_json(sys_b, user_b)
    except Exception:  # noqa: BLE001
        book = {}
    book.setdefault("techniques", [])
    book.setdefault("golden_quotes", [])

    # 全书弧线图：从各段 arc 标注聚合（零 LLM 成本）
    arc_map = {}
    for arc in ("起", "承", "转", "合"):
        segs = [x for x in sec_records if x.get("arc") == arc]
        if segs:
            arc_map[arc] = {
                "segs": [x["no"] for x in segs],
                "roles": [x.get("arc_role", "") for x in segs if x.get("arc_role")][:4],
            }
    result = {
        "file": path.name,
        "title": book.get("title_guess") or title,
        "direction": book.get("direction", ""),
        "sections_total": len(sections),
        "sections": sec_records,
        "arc_map": arc_map,
        "book": book,
        "created_at": now_ts(),
    }
    out = decon_dir(cfg) / f"{_slug(result['title'])}.json"
    from .util import atomic_write_json

    atomic_write_json(out, result)

    # 沉淀素材库（批量模式跳过，避免数千篇淹没素材库）
    try:
        from . import material as mat

        if not deposit_material:
            raise RuntimeError("批量模式：跳过素材库写入")

        for s_ in sec_records:
            if s_.get("hook"):
                mat.add_material(cfg, "章末钩子", f"《{result['title']}》{s_['hook']}",
                                 direction=book.get("direction", ""), tags=["拆书"],
                                 source_project=title)
            if s_.get("shuang"):
                mat.add_material(cfg, "情绪爆点", f"《{result['title']}》{s_['shuang']}",
                                 direction=book.get("direction", ""), tags=["拆书"],
                                 source_project=title)
        for q in book.get("golden_quotes", []) or []:
            mat.add_material(cfg, "金句", str(q), direction=book.get("direction", ""),
                             tags=["拆书"], source_project=title)
        for t in book.get("techniques", []) or []:
            mat.add_material(cfg, "写作技法", str(t), direction=book.get("direction", ""),
                             tags=["拆书"], source_project=title)
    except Exception as e:  # noqa: BLE001
        print(f"    [decon] 素材入库失败（不影响拆解）：{e}")

    # 知识单元入向量库（kb 可用则自动，失败不影响拆解）
    try:
        from . import kb

        n_units = kb.ingest_decon(cfg, result)
        print(f"[decon] 知识单元入向量库：{n_units} 条")
    except Exception as e:  # noqa: BLE001
        print(f"    [decon] 向量库入库失败（不影响拆解）：{e}")

    print(f"[decon] 《{result['title']}》拆解完成：{len(sec_records)} 段 → {out}")
    return result


def deconstruct_dir(cfg: dict, llm, dir_path: str | Path, **kw) -> list[dict]:
    """批量拆书：目录下所有 .txt。"""
    results = []
    p = Path(dir_path)
    if not p.exists():
        raise SystemExit(f"目录不存在：{p}")
    txts = sorted(p.glob("*.txt"))
    if not txts:
        raise SystemExit(f"{p} 下没有 .txt 文件")
    print(f"[decon] 批量拆书：{len(txts)} 个文件")
    for f in txts:
        try:
            results.append(deconstruct_file(cfg, llm, f, **kw))
        except SystemExit as e:
            print(f"    [decon] 跳过 {f.name}：{e}")
    return results
