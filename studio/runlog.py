# -*- coding: utf-8 -*-
"""运行日志系统：每次运行自动生成一份全模块运行日志。

三层内容：
  1. 全量输出镜像：控制台 stdout 通过 Tee 同时写入日志文件（所有模块的 print 全保留）；
  2. 结构化事件：阶段开始/结束（含耗时与成败）、LLM 调用（角色/模型/Token）、
     热点源抓取、素材沉淀、错误——由各模块调用 event()/staged() 记录；
  3. 运行总结：结束时写入「阶段耗时表 + LLM 统计 + 错误数」到日志尾部，
     并在 workspace/logs/runs.jsonl 建立运行索引（供看板展示）。

用法（CLI 已自动接入）：
  runlog.start(args, workspace_abs)   # 入口处
  with runlog.stage("抓取热点"): ...   # 模块级阶段计时
  runlog.event("模块名", "发生了什么")  # 自由事件
  runlog.end()                        # 退出处（atexit 兜底）
"""
from __future__ import annotations

import atexit
import functools
import json
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

_state: dict = {"session": None}


class _Tee:
    """把 stdout 同时写到控制台与日志文件。"""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, s):
        n = 0
        try:
            n = self._stream.write(s)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._fh.write(s)
            self._fh.flush()
        except Exception:  # noqa: BLE001
            pass
        return n or len(s)

    def flush(self):
        for x in (self._stream, self._fh):
            try:
                x.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        try:
            return bool(self._stream.isatty())
        except Exception:  # noqa: BLE001
            return False

    def __getattr__(self, name):  # 其余属性透传原 stdout
        return getattr(self._stream, name)


def start(args, workspace_abs: str, enabled: bool = True):
    """开启一次运行会话（CLI 入口调用一次）。"""
    if not enabled or _state["session"] is not None:
        return None
    ws = Path(workspace_abs)
    log_dir = ws / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd0 = args[0] if isinstance(args, list) and args else "run"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    fh = open(log_dir / f"run_{stamp}_{cmd0}.log", "w", encoding="utf-8", errors="replace")
    fh.write(f"=== 运行日志 | 命令: {' '.join(map(str, args))} | 开始: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _state["session"] = {
        "args": list(map(str, args)),
        "log_file": str(fh.name),
        "fh": fh,
        "started": time.time(),
        "stages": [],
        "stage_stack": [],
        "llm_calls": 0,
        "tokens_out": 0,
        "errors": 0,
        "events": [],
    }
    sys.stdout = _Tee(sys.stdout, fh)
    atexit.register(end)
    return fh.name


def _session():
    return _state["session"]


def event(module: str, msg: str, level: str = "info"):
    s = _session()
    line = f"[{level}] [{module}] {msg}"
    if s:
        s["events"].append({"module": module, "msg": msg, "level": level, "at": time.time()})
        if level == "error":
            s["errors"] += 1
    try:
        print(line)
    except Exception:  # noqa: BLE001
        pass


def error(module: str, msg: str):
    event(module, msg, level="error")


def note_llm(role: str, model: str, completion_tokens: int):
    s = _session()
    if not s:
        return
    s["llm_calls"] += 1
    s["tokens_out"] += int(completion_tokens or 0)
    event("llm", f"{role}({model}) 完成 {completion_tokens} tokens")


@contextmanager
def stage(name: str):
    """模块级阶段计时：with stage('抓取热点'): ..."""
    s = _session()
    if not s:
        yield
        return
    t0 = time.time()
    rec = {"name": name, "ok": None, "sec": None}
    s["stages"].append(rec)
    (s["stage_stack"]).append(name)
    print(f"[runlog] ▶ {name}")
    try:
        yield
    except Exception as e:  # noqa: BLE001
        rec["ok"] = False
        rec["sec"] = round(time.time() - t0, 1)
        s["errors"] += 1
        print(f"[runlog] ✘ {name} 失败（{rec['sec']}s）：{e}")
        print(traceback.format_exc(limit=4))
        raise
    else:
        rec["ok"] = True
        rec["sec"] = round(time.time() - t0, 1)
        print(f"[runlog] ✔ {name}（{rec['sec']}s）")
    finally:
        if s["stage_stack"] and s["stage_stack"][-1] == name:
            s["stage_stack"].pop()


def staged(name: str):
    """装饰器形式：@staged('抓取热点') 应用到 stage 函数。

    用 functools.wraps 保留原函数的 __name__/__doc__/__wrapped__：
    否则所有 stage 函数都变成 wrap，pytest 参数化 id、traceback 栈帧、
    以及 `inspect.signature` 全部丢失原函数信息。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            with stage(name):
                return fn(*a, **k)
        return wrap
    return deco


def end():
    """结束会话：写运行总结 + 索引。幂等。"""
    s = _session()
    if not s:
        return
    try:
        duration = round(time.time() - s["started"], 1)
        lines = [
            "",
            f"=== 运行总结 ===",
            f"命令：{' '.join(s['args'])}",
            f"总耗时：{duration}s | 阶段 {len(s['stages'])} 个 | 错误 {s['errors']} 次 | "
            f"LLM 调用 {s['llm_calls']} 次 / 输出 {s['tokens_out']} tokens",
            "阶段耗时：",
        ]
        for st in s["stages"]:
            mark = "✔" if st["ok"] else ("✘" if st["ok"] is False else "▶")
            lines.append(f"  {mark} {st['name']}  {st['sec'] if st['sec'] is not None else '-'}s")
        lines.append(f"日志文件：{s['log_file']}")
        text = "\n".join(lines) + "\n"
        s["fh"].write(text)
        s["fh"].flush()
        s["fh"].close()

        idx = Path(s["log_file"]).parent / "runs.jsonl"
        record = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": s["args"],
            "duration_s": duration,
            "stages": [{"name": x["name"], "sec": x["sec"], "ok": x["ok"]} for x in s["stages"]],
            "llm_calls": s["llm_calls"],
            "tokens_out": s["tokens_out"],
            "errors": s["errors"],
            "log_file": s["log_file"],
        }
        with open(idx, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        out = sys.stdout
        try:
            sys.stdout = out.__dict__.get("_stream", out)
        except Exception:  # noqa: BLE001
            pass
        print(text)
        _state["session"] = None
    except Exception:  # noqa: BLE001
        _state["session"] = None


def list_runs(cfg: dict, limit: int = 30) -> list[dict]:
    p = Path(cfg["studio"]["workspace_abs"]) / "logs" / "runs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(out[-limit:]))


def read_run_log(cfg: dict, log_file: str, tail: int = 400) -> str:
    p = Path(log_file)
    if not p.exists() or not p.resolve().is_relative_to((Path(cfg["studio"]["workspace_abs"]) / "logs").resolve()):
        return ""
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])
