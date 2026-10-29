# -*- coding: utf-8 -*-
"""Laya 判别器常驻子进程服务。

为什么独立进程：实测本机（Windows + torch 2.14 cu126）在**非主线程**里加载或推理
laya/torch 模型都会段错误（主线程反复验证稳定）。因此模型必须活在某个进程的主线程。
选择独立常驻进程而非主进程主线程加载，原因：
- 看板的生成 worker 是线程——不能在主线程里按需加载后供线程用（线程推理也崩）；
- CLI / produce / 看板多个调用方共享同一个服务（固定端口），只加载一份模型；
- 子进程崩溃不影响看板，下次请求自动拉起；空闲 30 分钟自动退出省内存。

协议（127.0.0.1:8647）：
  GET  /health → {"ready": bool, "stats": {...}}
  POST /judge  → body {"candidates": [...], "hist": [...],
                       "threshold_dup": 0.60, "threshold_new": 0.80}
               → {"flags": [true/false/null, ...]}
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8647
IDLE_EXIT_SEC = 1800

_DUP_INSTRUCTIONS = (
    "判断新故事梗与历史梗是否本质重复。判重标准：题材相同且情节骨架相同"
    "（只换人名、换场景、换职业但骨架一致也算重复）；题材与情节骨架都不同才算新。"
)

agent = None
_stats = {"runs": 0, "cands": 0, "dup": 0, "new": 0, "unsure": 0, "llm_saved": 0,
          "loaded_at": "", "last_request": time.time()}


def _load_agent():
    global agent
    if agent is not None:
        return agent
    t0 = time.time()
    import laya as _laya

    agent = _laya.load("convaiinnovations/laya", subfolder="multilingual", device="cpu")
    _stats["loaded_at"] = time.strftime("%H:%M:%S")
    print(f"[laya-server] 模型就绪（{time.time() - t0:.0f}s）", flush=True)
    return agent


def _judge(candidates: list, hist: list, threshold_dup: float, threshold_new: float) -> list:
    _load_agent()
    hist = [h for h in (x.strip() for x in hist) if h][:12]
    if not hist:
        return [False] * len(candidates)
    _stats["runs"] += 1
    out: list = []
    for cand in candidates:
        cand = (cand or "").strip()
        if not cand:
            out.append(None)
            continue
        questions = {
            f"h{i}": {
                "type": "choice",
                "instructions": f"新故事梗：「{cand[:240]}」\n历史梗{i + 1}：「{h[:240]}」\n{_DUP_INSTRUCTIONS}",
                "criteria": {
                    "dup": "本质重复（题材相同且情节骨架相同）",
                    "new": "本质不同（题材或情节骨架不同）",
                },
            }
            for i, h in enumerate(hist)
        }
        res = agent.system_one(cand[:400], questions)
        answers = [a for a in (res.get("answers") or {}).values()
                   if a.get("type") == "choice"]
        if not answers:
            out.append(None)
            continue
        sure_dup = any(a.get("choice") == "dup" and
                       float(a.get("answer_confidence") or 0) >= threshold_dup
                       for a in answers)
        sure_new = all(a.get("choice") == "new" and
                       float(a.get("answer_confidence") or 0) >= threshold_new
                       for a in answers)
        out.append(True if sure_dup else (False if sure_new else None))
    _stats["cands"] += len(candidates)
    _stats["dup"] += sum(1 for f in out if f is True)
    _stats["new"] += sum(1 for f in out if f is False)
    _stats["unsure"] += sum(1 for f in out if f is None)
    _stats["llm_saved"] += sum(1 for f in out if f is not None)
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _stats["last_request"] = time.time()
        if self.path == "/health":
            self._send({"ready": agent is not None, "stats": dict(_stats)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        _stats["last_request"] = time.time()
        if self.path != "/judge":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            flags = _judge(payload.get("candidates") or [],
                           payload.get("hist") or [],
                           float(payload.get("threshold_dup", 0.60)),
                           float(payload.get("threshold_new", 0.80)))
            self._send({"ok": True, "flags": flags})
        except Exception as e:  # noqa: BLE001
            self._send({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})


def _idle_watchdog():
    while True:
        time.sleep(60)
        if time.time() - _stats["last_request"] > IDLE_EXIT_SEC:
            print("[laya-server] 空闲超时退出（下次调用自动重启）", flush=True)
            os._exit(0)


def main():
    threading.Thread(target=_idle_watchdog, daemon=True).start()

    # 先加载模型再开端口：/health ready=True 即真正可用，调用方轮询即可
    _load_agent()
    srv = HTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"[laya-server] 监听 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
