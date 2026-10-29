# -*- coding: utf-8 -*-
"""专名清洗：防止知识库沉淀/召回把历史作品的角色名、人设带进新作品，
导致角色名班子反复雷同（钱招娣→钱麦穗、白玉京→白玉仙→白青黛、谢无妄→谢无咎）。

- 沉淀侧：material.deposit_from_project 用本篇 bible 角色名表做「角色名→角色占位」替换。
- 存量侧：clean_legacy_units 用全历史项目角色名表清洗 units.jsonl。
- 角色占位映射以 role 语义为准（女主/原女主/反派/男主N/男配N），不依赖具体姓名。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 拆书语料里常见的外部人名（《迟来逢冬》《福寿村怪谈》等），照搬同样造成雷同
EXTERNAL_NAMES = [
    "陆沉舟", "苏软软", "林晚", "顾辞", "许绍", "傅司礼", "白玉京", "白青黛",
    "钱麦穗", "钱招娣", "牛翠翠", "柳如烟", "谢危", "裴野", "沈无尘", "金不换",
    "顾长渊", "燕七", "沈长渊", "霍冲", "裴寂", "谢无妄", "白玉仙", "裴铮",
    "谢无咎", "沈宴", "霍决", "裴金", "麦穗", "裘半亩", "阿苗", "柳娇娇", "李珏",
]


def _role_placeholder(role: str, idx: int, used: dict[str, int]) -> str:
    """按角色语义映射占位。同一语义多个角色 → 女主1/女主2…"""
    r = (role or "").strip()
    if re.search(r"原女主|正主|白月光", r):
        key = "原女主"
    elif re.search(r"反派|死对头|恶女|养女|继母|恶毒", r):
        key = "反派"
    elif re.search(r"女主|主角|穿成|师姐|炮灰|嫡女|庶女|重生者|宿主", r):
        key = "女主"
    elif re.search(r"男主|夫君|皇帝|太子|王爷|统领|阁主|世子|首辅|权臣|备胎|股|少将军|暗卫|死士|师尊|掌门|师傅", r):
        key = "男主"
    else:
        key = "配角"
    used[key] = used.get(key, 0) + 1
    n = used[key]
    return key if n == 1 else f"{key}{n}"


def build_name_map(bible: dict) -> dict[str, str]:
    """从故事圣经构建 角色名→占位 映射（长名优先替换）。"""
    name_map: dict[str, str] = {}
    used: dict[str, int] = {}
    for i, c in enumerate(bible.get("characters", []) or []):
        nm = (c.get("name") or "").strip()
        if not nm or nm in name_map:
            continue
        role = c.get("role", "") or ""
        ph = _role_placeholder(role, i, used)
        name_map[nm] = ph
    # 按名字长度降序替换，避免"钱麦穗"被"麦穗"先替换
    return dict(sorted(name_map.items(), key=lambda kv: -len(kv[0])))


def clean_proper_names(text: str, name_map: dict[str, str] | None = None,
                       extra_names: list[str] | None = None) -> str:
    """把文本中的角色专名替换为占位。name_map 为空时用外部常见人名表兜底。"""
    if not text:
        return text
    table: dict[str, str] = {}
    if name_map:
        table.update(name_map)
    for nm in (extra_names or EXTERNAL_NAMES):
        table.setdefault(nm, "（专名）")
    if not table:
        return text
    # 长名优先
    for nm in sorted(table, key=len, reverse=True):
        text = text.replace(nm, table[nm])
    return text


def clean_legacy_units(units_path: Path, projects_dir: Path) -> tuple[int, list[str]]:
    """一次性存量清洗：用全部历史项目 bible 角色名表 + 外部人名表，
    重写 units.jsonl（保留其余字段），返回 (清洗条数, 样例)。"""
    table: dict[str, str] = {}
    used: dict[str, int] = {}
    for p in sorted(Path(projects_dir).glob("*/bible.json")):
        try:
            bible = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for i, c in enumerate(bible.get("characters", []) or []):
            nm = (c.get("name") or "").strip()
            if not nm or nm in table:
                continue
            table[nm] = _role_placeholder(c.get("role", "") or "", i, used)
    for nm in EXTERNAL_NAMES:
        table.setdefault(nm, "（专名）")

    rows = []
    cleaned = 0
    samples: list[str] = []
    if not units_path.exists():
        return 0, []
    for line in units_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            u = json.loads(line)
        except Exception:
            continue
        t = u.get("text", "")
        new_t = t
        for nm in sorted(table, key=len, reverse=True):
            new_t = new_t.replace(nm, table[nm])
        if new_t != t:
            u["text"] = new_t
            cleaned += 1
            if len(samples) < 5:
                samples.append(f"{u.get('kind','')}: {new_t[:70]}")
        rows.append(json.dumps(u, ensure_ascii=False))
    units_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return cleaned, samples
