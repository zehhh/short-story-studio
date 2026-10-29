# -*- coding: utf-8 -*-
"""热点数据结构与适配器基类。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class HotItem:
    source: str            # 来源平台标识，如 weibo / zhihu
    rank: int              # 榜单排名（从1开始）
    title: str             # 热点标题
    hot_value: float = 0.0  # 热度值（无量纲，仅用于排序）
    url: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HotItem":
        return cls(
            source=d.get("source", ""),
            rank=int(d.get("rank", 0)),
            title=d.get("title", ""),
            hot_value=float(d.get("hot_value", 0)),
            url=d.get("url", ""),
            extra=d.get("extra", {}) or {},
        )


def parse_hot_value(text: str) -> float:
    """把 '5000万热度' / '382.7万' / '1234567' 之类的热度文本转成数值。"""
    if text is None:
        return 0.0
    s = str(text).strip().replace(",", "").replace(" ", "")
    m = None
    import re

    m = re.search(r"([\d.]+)\s*亿", s)
    if m:
        return float(m.group(1)) * 1e8
    m = re.search(r"([\d.]+)\s*万", s)
    if m:
        return float(m.group(1)) * 1e4
    m = re.search(r"([\d.]+)", s)
    if m:
        return float(m.group(1))
    return 0.0
