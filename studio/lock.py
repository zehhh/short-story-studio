# -*- coding: utf-8 -*-
"""跨进程项目锁：一个 JSON 文件即锁，带 pid / 心跳 / 阶段 / 死进程回收（借鉴 openwrite B01）。

为什么需要：
    本项目的 run/continue/produce/scheduler 都可能同时操作**同一个项目目录**——
    两个 run 并行写 project.json 会互相覆盖阶段状态，断点续跑读到别人的半成品，
    严重时章节文件张冠李戴。调度器虽然有自己的运行锁，但那只锁整个调度器，
    不锁单项目；`produce --count 3` 与手工 `run` 撞车也没有保护。

设计（零依赖、跨平台）：
    - 锁文件：`<项目目录>/project.lock`，内容是 JSON：pid / host / phase / started_at / heartbeat；
    - 获取：文件不存在 → 原子创建；存在 → 读 pid 与心跳：
        * 持锁进程已死（Windows 用 OpenProcess 判定，POSIX 用 os.kill(pid,0)）→ 回收；
        * 心跳超过 stale_seconds 未更新（默认 900s）→ 视为僵死，回收；
        * 否则抛 `StudioError("PROJECT_BUSY")`，写明持锁 pid/阶段/起始时间。
    - 心跳：长任务每处理完一节/一章调用 `beat()`；调用方也可用 `heartbeat()` 线程。
    - 释放：只删自己持有的锁（比对 pid），避免误删别人刚抢到的锁。

用法：
    from .lock import project_lock
    with project_lock(project.root, "write"):
        ...  # 只有持锁进程能写
"""
from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import StudioError
from .util import atomic_write_json, now_ts

LOCK_NAME = "project.lock"
DEFAULT_STALE = 900  # 秒：心跳超过这个时间没更新，判定为僵死进程


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否仍存活（不依赖 psutil）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                # 拒绝访问通常意味着进程存在但权限不同 → 保守认为存活
                err = ctypes.get_last_error()
                return err == 5  # ERROR_ACCESS_DENIED
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001  判定失败时保守认为存活
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  半截/损坏视为无锁
        return None


def is_locked(root) -> dict | None:
    """返回锁信息（无锁或锁已僵死返回 None）。供看板/CLI 只读查询。"""
    p = Path(root) / LOCK_NAME
    if not p.exists():
        return None
    info = _read_lock(p)
    if not info:
        return None
    if not _pid_alive(int(info.get("pid") or 0)):
        return None
    age = time.time() - float(info.get("heartbeat_ts") or 0)
    if age > DEFAULT_STALE:
        return None
    return info


@contextmanager
def project_lock(root, phase: str = "run", stale_seconds: int = DEFAULT_STALE):
    """获取项目写锁（with 语法）；已在别处持锁时抛 PROJECT_BUSY。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    p = root / LOCK_NAME
    pid = os.getpid()
    if p.exists():
        info = _read_lock(p)
        if info:
            holder = int(info.get("pid") or 0)
            age = time.time() - float(info.get("heartbeat_ts") or 0)
            alive = _pid_alive(holder)
            if holder != pid and alive and age <= stale_seconds:
                raise StudioError(
                    "PROJECT_BUSY",
                    f"项目正被另一个进程占用：pid={holder} 阶段={info.get('phase')} "
                    f"起始={info.get('started_at')} 心跳 {int(age)}s 前",
                    where=f"{root.name}/{LOCK_NAME}",
                    found=json.dumps(info, ensure_ascii=False),
                    expect="同一项目同一时间只允许一个写入进程",
                    fix=("等持锁进程结束；若确认它已死（崩溃/强杀），"
                         f"删除 {p} 后重跑"),
                )
        # 自己的锁 / 死进程 / 心跳过期 → 回收
        try:
            p.unlink()
        except OSError:
            pass
    info = {
        "pid": pid,
        "host": socket.gethostname(),
        "phase": phase,
        "started_at": now_ts(),
        "heartbeat": now_ts(),
        "heartbeat_ts": time.time(),
        "request_id": None,
    }
    try:
        from .errors import REQUEST_ID

        info["request_id"] = REQUEST_ID
    except Exception:  # noqa: BLE001
        pass
    atomic_write_json(p, info)
    locked = {"ok": True}

    def beat(new_phase: str | None = None):
        """更新心跳（长任务每节/每章调用一次）。"""
        info["heartbeat"] = now_ts()
        info["heartbeat_ts"] = time.time()
        if new_phase:
            info["phase"] = new_phase
        try:
            atomic_write_json(p, info)
        except OSError:
            pass

    from types import SimpleNamespace

    handle = SimpleNamespace(beat=beat, path=p, info=info)
    try:
        yield handle
    finally:
        cur = _read_lock(p)
        if cur and int(cur.get("pid") or 0) == pid:
            try:
                p.unlink()
            except OSError:
                pass
