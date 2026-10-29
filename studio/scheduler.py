# -*- coding: utf-8 -*-
"""定时任务：到点自动抓热点/热梗并按设定批量撰写短篇小说。

任务定义存 workspace/schedules.json，每个任务可设置：
  - 触发：每日 HH:MM 或 每 N 分钟
  - 篇数 count、题材 directions（多篇自动轮换）、字数 words、平台 platform
  - 热点来源 sources（缺省用 config）
  - provider（mock/本地/云端，缺省跟随 config）
运行方式：
  python -m studio scheduler            # 常驻调度（每 60s 检查一轮）
  python -m studio scheduler --once     # 只跑一轮到期任务（测试/手动触发）
执行使用子进程隔离，日志写 workspace_logs/sched_<任务>_<时间>.log；
运行期间加 .scheduler_lock 防并发，心跳写 .scheduler_heartbeat 供看板判断存活。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .lock import _pid_alive
from .util import now_ts

LOCK_NAME = ".scheduler_lock"
HEARTBEAT_NAME = ".scheduler_heartbeat"


def _lock_holder(path: Path):
    """解析运行锁 → (pid, ts)；无法解析返回 None（视为陈旧锁，可接管）。"""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return int(d.get("pid") or 0), float(d.get("ts") or 0)
    except Exception:  # noqa: BLE001  旧版锁只写了一行时间戳/或半截文件
        try:
            return 0, float(path.stat().st_mtime)
        except OSError:
            return None


def schedules_path(cfg: dict) -> Path:
    p = Path(cfg["studio"]["workspace_abs"]) / "schedules.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_schedules(cfg: dict) -> list[dict]:
    p = schedules_path(cfg)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("jobs", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        return []


def save_schedules(cfg: dict, jobs: list[dict]):
    """定时任务表全量重写。原子写：写一半崩掉会丢掉用户配的全部任务。"""
    from .util import atomic_write_json

    p = schedules_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, {"jobs": jobs})


def new_job(cfg: dict, name: str, trigger: dict, count: int = 1,
            directions: list[str] | None = None, words: int | None = None,
            platform: str | None = None, sources: list[str] | None = None,
            provider: str | None = None, auto_deposit: bool = False,
            auto_publish: bool = False, auto_data: bool = False,
            auto_improve: bool = False, publish_mode: str | None = None,
            data_source: str | None = None) -> dict:
    return {
        "id": "J" + now_ts().replace("-", "").replace(" ", "").replace(":", ""),
        "name": name or "未命名任务",
        "enabled": True,
        "trigger": trigger,  # {"type":"daily","time":"20:00"} 或 {"type":"interval","minutes":180}
        "count": max(1, int(count or 1)),
        "directions": directions or [],
        "words": int(words) if words else None,   # None = 用 config 默认
        "platform": platform or "",               # 空 = 用 config 默认
        "sources": sources or [],
        "provider": provider or "",               # 空 = 跟随 config
        "auto_deposit": auto_deposit,
        "auto_publish": bool(auto_publish),
        "auto_data": bool(auto_data),
        "auto_improve": bool(auto_improve),
        "publish_mode": publish_mode or ("mock" if (auto_publish and provider == "mock") else "manual"),
        "data_source": data_source or ("mock" if provider == "mock" else "custom"),
        "last_run": "",
        "last_result": "",
        "history": [],
    }


def is_due(job: dict, now=None) -> bool:
    import datetime as dt

    if not job.get("enabled", True):
        return False
    now = now or dt.datetime.now()
    trig = job.get("trigger", {}) or {}
    last = job.get("last_run", "")
    try:
        if trig.get("type") == "daily":
            hh, mm = (trig.get("time", "20:00") + ":0").split(":")[:2]
            target = dt.datetime.strptime(f"{int(hh):02d}:{int(mm):02d}", "%H:%M").time()
            today = now.strftime("%Y-%m-%d")
            already = last.startswith(today)
            return (not already) and now.time() >= target
        if trig.get("type") == "interval":
            minutes = max(1, int(trig.get("minutes", 60)))
            if not last:
                return True
            last_dt = dt.datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
            return (now - last_dt).total_seconds() >= minutes * 60
    except Exception:  # noqa: BLE001
        return False
    return False


def next_run_text(job: dict) -> str:
    trig = job.get("trigger", {}) or {}
    if trig.get("type") == "daily":
        return f"每日 {trig.get('time', '20:00')}"
    if trig.get("type") == "interval":
        return f"每 {trig.get('minutes', 60)} 分钟"
    return str(trig)


def heartbeat(cfg: dict):
    p = Path(cfg["studio"]["workspace_abs"]) / HEARTBEAT_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(now_ts(), encoding="utf-8")


def scheduler_alive(cfg: dict, max_age_sec: int = 150) -> bool:
    p = Path(cfg["studio"]["workspace_abs"]) / HEARTBEAT_NAME
    if not p.exists():
        return False
    import datetime as dt

    try:
        last = dt.datetime.strptime(p.read_text(encoding="utf-8").strip()[:19], "%Y-%m-%d %H:%M:%S")
        return (dt.datetime.now() - last).total_seconds() < max_age_sec
    except Exception:  # noqa: BLE001
        return False


def build_run_cmd(cfg: dict, job: dict, story_index: int) -> list[str]:
    directions = job.get("directions") or []
    cmd = [sys.executable, "-m", "studio", "run", "--pick", "auto"]
    if cfg.get("_config_file"):
        cmd += ["--config", cfg["_config_file"]]
    if job.get("idea"):
        cmd += ["--idea", job["idea"]]
    if job.get("sources"):
        cmd += ["--source", ",".join(job["sources"])]
    if directions:
        cmd += ["--direction", directions[story_index % len(directions)]]
    if job.get("words"):
        cmd += ["--words", str(job["words"])]
    if job.get("platform"):
        cmd += ["--platform", job["platform"]]
    if job.get("provider"):
        cmd += ["--provider", job["provider"]]
        if job["provider"] == "mock":
            cmd += ["--no-kb"]  # mock 产出不入知识库
    # 成稿沉淀默认关；任务显式勾了才加 --deposit，否则子进程仍按默认（不沉淀）
    if job.get("auto_deposit"):
        cmd += ["--deposit"]
    return cmd


def build_produce_cmd(cfg: dict, job: dict) -> list[str]:
    """全流程制作命令：N 篇写作 → 自动发布 → 数据回流 → 复盘改进。"""
    cmd = [sys.executable, "-m", "studio", "produce",
           "--count", str(max(1, int(job.get("count", 1))))]
    if cfg.get("_config_file"):
        cmd += ["--config", cfg["_config_file"]]
    if job.get("idea"):
        cmd += ["--idea", job["idea"]]
    if job.get("directions"):
        cmd += ["--directions", ",".join(job["directions"])]
    if job.get("sources"):
        cmd += ["--source", ",".join(job["sources"])]
    if job.get("words"):
        cmd += ["--words", str(job["words"])]
    if job.get("platform"):
        cmd += ["--platform", job["platform"]]
    if job.get("provider"):
        cmd += ["--provider", job["provider"]]
        if job["provider"] == "mock":
            cmd += ["--no-kb"]
    if job.get("auto_publish"):
        cmd += ["--publish-mode", job.get("publish_mode") or
                ("mock" if job.get("provider") == "mock" else "manual")]
    if job.get("auto_data"):
        cmd += ["--data-source", job.get("data_source") or
                ("mock" if job.get("provider") == "mock" else "custom")]
    if job.get("auto_improve"):
        cmd += ["--with-improve"]
    # 成稿沉淀默认关；任务显式勾了才加 --deposit
    if job.get("auto_deposit"):
        cmd += ["--deposit"]
    return cmd


def run_job(cfg: dict, job: dict, runner=None, log_dir: Path | None = None) -> dict:
    """执行一个任务：连续写 count 篇（题材轮换）。runner 供测试注入。"""
    import datetime as dt

    jobs = load_schedules(cfg)
    for j in jobs:
        if j.get("id") == job["id"]:
            job = j
            break
    lock = Path(cfg["studio"]["workspace_abs"]) / LOCK_NAME
    if lock.exists():
        # 陈旧锁回收：旧实现只看"文件存在"，进程被 kill/断电后锁文件永远留着，
        # 之后每次调度都跳过，直到人工删文件（缺陷 P1-6）。
        # 现在锁文件内容为 JSON {pid, ts}；解析失败或 pid 已死或超过 6 小时 → 接管。
        holder = _lock_holder(lock)
        if holder is not None and _pid_alive(holder[0]) and (time.time() - holder[1]) < 6 * 3600:
            print(f"[scheduler] 已有任务在运行（pid={holder[0]}，{int(time.time() - holder[1])}s 前启动），"
                  f"跳过 {job.get('name')}")
            return {"status": "skipped"}
        print(f"[scheduler] 清理陈旧运行锁（{holder if holder else '无有效 pid'}）后继续")
        try:
            lock.unlink()
        except OSError:
            pass
    # 排他创建而非「先检查再写」：两个调度进程同时进入时，上面的 exists()
    # 可能都为假，随后双双写锁、并发跑同一任务（check-then-act 竞态）。
    # O_CREAT|O_EXCL 让内核替我们仲裁，抢不到的那个直接跳过。
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        winner = _lock_holder(lock)
        print(f"[scheduler] 另一个调度进程已抢先取得运行锁（pid={winner[0] if winner else '?'}），"
              f"跳过 {job.get('name')}")
        return {"status": "skipped"}
    with os.fdopen(fd, "w", encoding="utf-8") as _lf:
        _lf.write(json.dumps({"pid": os.getpid(), "ts": time.time(), "at": now_ts()},
                             ensure_ascii=False))
        _lf.flush()
        os.fsync(_lf.fileno())
    log_dir = log_dir or (Path(cfg["studio"]["workspace_abs"]).parent / "workspace_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        chained = job.get("auto_publish") or job.get("auto_data") or job.get("auto_improve")
        if chained:
            cmd = build_produce_cmd(cfg, job)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = log_dir / f"sched_{job.get('name', 'job')}_{stamp}.log"
            print(f"[scheduler] {job.get('name')} 全流程启动（{job.get('count', 1)} 篇×写/发/数据/复盘）→ {log_file.name}")
            if runner is not None:
                code = runner(cmd)
            else:
                with open(log_file, "w", encoding="utf-8") as fh:
                    code = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
            results.append({"index": 0, "cmd": " ".join(cmd[3:]), "code": code,
                            "log": str(log_file), "at": now_ts()})
        else:
            count = max(1, int(job.get("count", 1)))
            for i in range(count):
                cmd = build_run_cmd(cfg, job, i)
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                log_file = log_dir / f"sched_{job.get('name', 'job')}_{stamp}.log"
                print(f"[scheduler] {job.get('name')} 第 {i + 1}/{count} 篇启动 → {log_file.name}")
                if runner is not None:
                    code = runner(cmd)
                else:
                    with open(log_file, "w", encoding="utf-8") as fh:
                        code = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
                results.append({"index": i, "cmd": " ".join(cmd[3:]), "code": code,
                                "log": str(log_file), "at": now_ts()})
    finally:
        # 锁必须无条件释放：写 schedules 失败时若跳过 unlink，锁会留到 6 小时
        # 陈旧判定之后才被回收，期间每次调度都跳过（用户看到的就是"调度器不动了"）。
        try:
            job["last_run"] = now_ts()
            ok = sum(1 for r in results if r["code"] == 0)
            job["last_result"] = f"{ok}/{len(results)} 篇成功"
            all_jobs = load_schedules(cfg)
            for j in all_jobs:
                if j.get("id") == job["id"]:
                    j.update({k: job.get(k) for k in ("last_run", "last_result")})
                    j.setdefault("history", []).append({"at": now_ts(), "results": results})
                    j["history"] = j["history"][-10:]
            save_schedules(cfg, all_jobs)
        finally:
            lock.unlink(missing_ok=True)
    return {"status": "done", "results": results}


def loop(cfg: dict, interval_sec: int = 60, once: bool = False):
    print(f"[scheduler] 调度器启动（每 {interval_sec}s 检查；Ctrl+C 停止）")
    while True:
        heartbeat(cfg)
        jobs = load_schedules(cfg)
        for job in jobs:
            if is_due(job):
                print(f"[scheduler] 任务到期：{job.get('name')}")
                run_job(cfg, job)
        if once:
            print("[scheduler] --once 模式：本轮结束即退出")
            return
        time.sleep(interval_sec)
