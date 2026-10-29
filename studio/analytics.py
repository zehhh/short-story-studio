# -*- coding: utf-8 -*-
"""阅读数据回流：抓取/录入已发布作品的阅读数据，形成时间序列并计算趋势指标。

三种数据来源：
1. custom —— 用户抓包作家后台后，在 config 配置 stats 接口模板 + 字段映射（fields）；
2. mock   —— 确定性合成数据（含"第3章掉队"等真实模式），离线测试/演示；
3. import —— 手动录入（从后台肉眼抄数据，JSON/CSV 均可），最可靠的兜底。

快照落盘 workspace/analytics/<project>/<date>.json，形成时间序列。
趋势指标：阅读增长、完读率、互动率、章节留存（若提供 per-chapter 数据）。
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import requests

from .util import now_ts, date_stamp


def analytics_root(cfg: dict) -> Path:
    p = Path(cfg["studio"]["workspace_abs"]) / "analytics"
    p.mkdir(parents=True, exist_ok=True)
    return p


def project_dir(cfg: dict, project_name: str) -> Path:
    d = analytics_root(cfg) / project_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _platform_of(cfg: dict, project_name: str) -> str:
    """推断项目目标平台：publish.json > pitch.json > config.studio.platform。

    数据回流以前完全不记平台，快照里只有数字没有来源，跨平台对比和
    「投错平台」排查都做不了。
    """
    root = Path(cfg["studio"]["workspace_abs"]) / "projects" / project_name
    for name in ("publish.json", "pitch.json"):
        f = root / name
        if f.exists():
            try:
                plat = json.loads(f.read_text(encoding="utf-8")).get("platform")
            except (json.JSONDecodeError, OSError):
                plat = None
            if plat:
                return str(plat)
    return str(cfg.get("studio", {}).get("platform", ""))


def _dig(data, path: str):
    """按 a.b.c 路径取嵌套字段；路径含 [] 时取列表首元素。"""
    cur = data
    for part in path.replace("[", ".[0].").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part.strip("[]"))]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def fetch_custom(cfg: dict, project_name: str) -> dict:
    """按 config publish.endpoints.stats + fields 映射抓取后台数据。"""
    url = (cfg["publish"].get("endpoints") or {}).get("stats", "")
    if not url:
        raise SystemExit("custom 数据源需要配置 publish.endpoints.stats（作家后台数据接口，抓包获得）")
    headers = {}
    if cfg["publish"].get("cookie"):
        headers["Cookie"] = cfg["publish"]["cookie"]
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json() if "json" in r.headers.get("content-type", "") else json.loads(r.text)
    fields = cfg["publish"].get("fields") or {}
    metrics = {}
    for key, path in fields.items():
        v = _dig(data, path)
        if v is not None:
            try:
                metrics[key] = float(v)
            except (TypeError, ValueError):
                metrics[key] = v
    if not metrics:
        raise SystemExit(f"接口返回无法按 fields 映射出指标，请检查 publish.fields 配置。原始返回前 200 字：{str(data)[:200]}")
    return {"source": "custom", "metrics": metrics}


def fetch_mock(cfg: dict, project_name: str) -> dict:
    """确定性合成数据：7 天曲线 + 章节留存（第3章明显掉队，制造可分析的真实感）。"""
    seed = sum(ord(c) for c in project_name)
    days = 7
    base = 800 + seed % 700
    daily = []
    for d in range(days):
        growth = base * (1.6 ** d) * (1 + ((seed + d) % 13) / 100)
        daily.append(round(growth))
    total_reads = sum(daily)
    chapters = []
    retention = 1.0
    for no in range(1, 7):
        drop = 0.12 + ((seed + no * 7) % 10) / 100
        if no == 3:  # 合成数据里埋一个"第3章掉队"
            drop += 0.18
        retention = max(0.3, retention - drop)
        chapters.append({"no": no, "reach_ratio": round(retention, 3)})
    return {
        "source": "mock",
        "metrics": {
            "exposure": round(total_reads * 2.4),
            "reads": total_reads,
            "finish_rate": round(retention, 3),
            "likes": round(total_reads * 0.06),
            "comments": round(total_reads * 0.012),
            "collections": round(total_reads * 0.03),
        },
        "daily_reads": daily,
        "chapter_retention": chapters,
    }


def fetch_stats(cfg: dict, project_name: str, source: str | None = None) -> dict:
    source = source or cfg.get("analytics", {}).get("source", "mock")
    if source == "mock":
        snap = fetch_mock(cfg, project_name)
    elif source == "custom":
        snap = fetch_custom(cfg, project_name)
    else:
        raise SystemExit(f"未知数据源：{source}（可选 mock/custom）")
    snap["project"] = project_name
    snap["fetched_at"] = now_ts()
    # 平台标签：同一份稿子可能投不同平台，数据回流不记平台的话，复盘时
    # 「这组完读率是知乎的还是番茄的」无从判断，跨平台对比也没法做。
    snap.setdefault("platform", _platform_of(cfg, project_name))
    d = project_dir(cfg, project_name)
    f = d / f"{date_stamp()}.json"
    from .util import atomic_write_json

    atomic_write_json(f, snap)
    print(f"[analytics] 快照已保存：{f}")
    return snap


def import_stats(cfg: dict, project_name: str, file_path: str) -> dict:
    """手动录入：JSON（metrics 字典或含 metrics 键）或 CSV（key,value 两列）。"""
    p = Path(file_path)
    raw = p.read_text(encoding="utf-8")
    metrics = {}
    daily = None
    chapters = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            metrics = data.get("metrics", data)
            daily = data.get("daily_reads")
            chapters = data.get("chapter_retention")
            metrics = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}
    except json.JSONDecodeError:
        for row in csv.DictReader(io.StringIO(raw)):
            k = (row.get("key") or row.get("指标") or "").strip()
            v = (row.get("value") or row.get("数值") or "").strip()
            if k and v:
                try:
                    metrics[k] = float(v)
                except ValueError:
                    continue
    if not metrics:
        raise SystemExit("未能解析出任何指标（JSON 需含数值字段，CSV 需 key,value 两列）")
    snap = {
        "source": "import",
        "project": project_name,
        "fetched_at": now_ts(),
        "metrics": metrics,
    }
    if daily:
        snap["daily_reads"] = daily
    if chapters:
        snap["chapter_retention"] = chapters
    d = project_dir(cfg, project_name)
    f = d / f"{date_stamp()}.json"
    from .util import atomic_write_json

    atomic_write_json(f, snap)
    print(f"[analytics] 手动录入快照已保存：{f}（{len(metrics)} 项指标）")
    return snap


# ---------------------------------------------------------------------------
# 趋势分析
# ---------------------------------------------------------------------------

def load_series(cfg: dict, project_name: str) -> list[dict]:
    d = project_dir(cfg, project_name)
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(d.glob("*.json"))]


def compute_trend(snaps: list[dict]) -> dict:
    """把时间序列压成趋势指标。"""
    if not snaps:
        return {}
    latest = snaps[-1]["metrics"]
    prev = snaps[-2]["metrics"] if len(snaps) > 1 else {}
    trend = {"snapshots": len(snaps), "latest": latest}
    for k in ("reads", "likes", "comments", "collections", "finish_rate"):
        if k in latest and k in prev and prev[k]:
            trend[f"{k}_growth"] = round((latest[k] - prev[k]) / prev[k], 3)
    ret = snaps[-1].get("chapter_retention") or []
    if ret:
        worst = min(ret, key=lambda c: c.get("reach_ratio", 1))
        trend["weakest_chapter"] = worst
        trend["retention_slope"] = round(ret[-1]["reach_ratio"] - ret[0]["reach_ratio"], 3)
    daily = snaps[-1].get("daily_reads") or []
    if len(daily) >= 2:
        trend["daily_reads_last_growth"] = round((daily[-1] - daily[-2]) / max(daily[-2], 1), 3)
    return trend


def summary_text(trend: dict) -> str:
    if not trend:
        return "（无数据）"
    lines = [f"快照数：{trend['snapshots']}"]
    latest = trend.get("latest", {})
    for k in ("reads", "exposure", "finish_rate", "likes", "comments", "collections"):
        if k in latest:
            lines.append(f"{k}：{latest[k]}")
    for k, v in trend.items():
        if k.endswith("_growth"):
            lines.append(f"{k}：{v:+.0%}")
    if trend.get("weakest_chapter"):
        w = trend["weakest_chapter"]
        lines.append(f"最弱小节：小节{int(w['no']):02d}（触达 {w['reach_ratio']:.0%}）")
    return "\n".join(lines)
