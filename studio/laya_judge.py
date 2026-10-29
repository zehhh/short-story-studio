# -*- coding: utf-8 -*-
"""Laya 判别器客户端：管线/看板/CLI 的统一入口。

实际推理在独立子进程 studio/laya_server.py（固定端口 8647）中进行——
本机 torch 在非主线程加载/推理会段错误，子进程主线程是唯一稳定形态。
本模块负责：健康探测 → 未就绪则拉起子进程（幂等，多调用方共享同一服务）
→ HTTP 调用。子进程崩溃/空闲退出后，下次调用自动重启，对上层透明。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

PORT = 8647
_BASE = f"http://127.0.0.1:{PORT}"
_SPAWN_LOCK = threading.Lock()

# kill-only 判定语义（2026-09-24 实测校准，multilingual 基座 zero-shot 校准偏弱）：
# 只在高置信 dup 时判重（杀），全部高置信 new 时才放行，其余一律 None → LLM 仲裁；
# 绝不允许低置信"new"放行疑似重复——漏放的代价远高于一次 LLM 调用
DEFAULT_DUP_THRESHOLD = 0.60
DEFAULT_NEW_THRESHOLD = 0.80
SELF_URL_DEFAULT = "http://127.0.0.1:8152"   # self 后端（自训判别器）服务地址
ROUTE_URL_DEFAULT = "http://127.0.0.1:8150"  # 方向路由判别器（cn_route，20 选 1）


def _spawn_jev(run_dir: str, port: int, health_url: str, wait_sec: int = 150) -> bool:
    """拉起一个 jev 推理服务（幂等：健康检查通过就不动）。"""
    import time as _t

    try:
        urllib.request.urlopen(health_url, timeout=2)
        return True
    except Exception:
        pass
    jev_root = Path(r"E:\AIStudio\xiaoke_jev\XiaokeAILabs\experiments\test_jev_open_source\text_jev_train")
    if not jev_root.exists():
        return False
    flags_ = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(
        [sys.executable, "scripts/serve.py", "--run-dir", run_dir,
         "--backbone", "models/Qwen3-0.6B-Base", "--port", str(port)],
        cwd=str(jev_root),
        stdout=open(jev_root.parent.parent.parent / f"serve_{port}.log", "ab"),
        stderr=subprocess.STDOUT, creationflags=flags_)
    t0 = _t.time()
    while _t.time() - t0 < wait_sec:
        try:
            urllib.request.urlopen(health_url, timeout=2)
            return True
        except Exception:
            _t.sleep(3)
    return False


def route_direction(text: str, directions: list[str],
                    base_url: str = ROUTE_URL_DEFAULT,
                    min_conf: float = 0.62) -> str | None:
    """方向路由：一句话梗 → 20 选 1。置信度不足返回 None（调用方走关键词/LLM 兜底）。"""
    if not directions or not (text or "").strip():
        return None
    try:
        urllib.request.urlopen(f"{base_url}/health", timeout=2)
    except Exception:
        if not _spawn_jev("runs/cn_route", 8150, f"{base_url}/health"):
            return None
    payload = {"requests": [{"id": "route", "state": text[:240],
                             "questions": [{"id": "route", "type": "choice",
                                            "question": "这条素材属于哪个下沉市场题材方向？",
                                            "options": directions}]}]}
    r = urllib.request.urlopen(urllib.request.Request(
        f"{base_url}/api/evaluate", json.dumps(payload).encode(),
        {"Content-Type": "application/json"}), timeout=60)
    d = json.load(r)
    ans = ((d.get("results") or [{}])[0].get("answers") or [{}])[0]
    dist = ans.get("distribution") or {}
    if not dist:
        return None
    best_i = max(dist, key=lambda k: float(dist[k]))
    best = (float(dist[best_i]), int(best_i))
    return directions[best[1]] if best[0] >= min_conf else None


def _health(timeout: float = 2.0) -> dict | None:
    try:
        r = urllib.request.urlopen(f"{_BASE}/health", timeout=timeout)
        return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def _spawn() -> None:
    """拉起常驻子进程（幂等：health 通了就不重复拉）。"""
    if _health():
        return
    with _SPAWN_LOCK:
        if _health():
            return
        import os
        from .config import ROOT

        log_f = open(ROOT / "workspace" / "logs" / "laya_server.log",
                     "ab") if (ROOT / "workspace" / "logs").exists() else subprocess.DEVNULL
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(
            [sys.executable, "-m", "studio.laya_server", str(PORT)],
            stdout=log_f, stderr=subprocess.STDOUT,
            cwd=str(ROOT), creationflags=flags)


def _wait_ready(timeout: float = 180.0) -> bool:
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        h = _health()
        if h and h.get("ready"):
            return True
        time.sleep(2)
    return False


def configure(checkpoint: str = "multilingual", device: str | None = None,
              reset: bool = False) -> bool:
    """预拉起子进程并等就绪（可选调用）。checkpoint/device 参数保留兼容，实际由服务端固定。"""
    try:
        _spawn()
        return _wait_ready()
    except Exception as e:  # noqa: BLE001
        print(f"[laya] 服务拉起失败：{type(e).__name__}: {str(e)[:90]}")
        return False


def prelaunch() -> None:
    """只拉起子进程不等加载（看板启动用，不阻塞）。模型由子进程主线程自行加载。"""
    _spawn()


def available() -> bool:
    h = _health()
    return bool(h and h.get("ready"))


def stats() -> dict:
    h = _health()
    return (h or {}).get("stats", {})


def dup_flags(candidates: list[str], hist: list[str],
              checkpoint: str = "multilingual", device: str | None = "cpu",
              threshold_dup: float = DEFAULT_DUP_THRESHOLD,
              threshold_new: float = DEFAULT_NEW_THRESHOLD,
              backend: str = "laya", self_url: str = SELF_URL_DEFAULT,
              api_url: str = "", api_key: str = "", api_model: str = "") -> list:
    """对每个候选梗判重。返回等长列表：True=判重 False=判新 None=不确定/不可用。

    backend="laya"：官方 multilingual 零样本检查点（子进程服务）；
    backend="self"：自训中文判别器（AgentJev 架构，HTTP 8152）；
    backend="api"：自行配置的外部判重 API（OpenAI 兼容端点，两两成对问模型，
                   kill-only 语义不变；请求失败按 0.5 处理→ 不会误判 dup）；
    backend="off"：直接返回 [None]（交主模型仲裁，等效未启用）。
    kill-only 语义：对任一历史梗高置信 dup → True；对全部历史梗都高置信 new →
    False；其余（含后端不可用）→ None 交主模型仲裁。
    """
    if backend == "api":
        try:
            return _api_dup_flags(candidates, hist, api_url, api_key, api_model,
                                  threshold_dup, threshold_new)
        except Exception as e:  # noqa: BLE001
            print(f"[laya] api 后端调用失败，交主模型仲裁：{type(e).__name__}: {str(e)[:90]}")
            return [None] * len(candidates)
    # YAML 1.1 会把裸 off/on/yes/no 解析成布尔（False/True），此处归一化防落入错误分支
    if backend is False or backend is None:
        backend = "off"
    if backend is True:
        backend = "laya"
    if backend == "off":
        return [None] * len(candidates)
    if backend == "self":
        try:
            return _self_dup_flags(candidates, hist, self_url,
                                   threshold_dup, threshold_new)
        except Exception as e:  # noqa: BLE001
            print(f"[laya] self 后端调用失败，交主模型仲裁：{type(e).__name__}: {str(e)[:90]}")
            return [None] * len(candidates)
    _spawn()
    h = _health(timeout=3.0)
    if not (h and h.get("ready")):
        if not _wait_ready():
            print("[laya] 判别服务未就绪，本轮交主模型仲裁")
            return [None] * len(candidates)
    payload = {"candidates": candidates, "hist": hist,
               "threshold_dup": threshold_dup, "threshold_new": threshold_new}
    r = urllib.request.urlopen(
        urllib.request.Request(f"{_BASE}/judge", json.dumps(payload).encode(),
                               {"Content-Type": "application/json"}), timeout=300)
    data = json.load(r)
    if not data.get("ok"):
        print(f"[laya] 判重失败：{data.get('error', '')[:90]}")
        return [None] * len(candidates)
    return list(data.get("flags") or [None] * len(candidates))

# ---------------------------------------------------------------------------
# self 后端：我们自己微调的中文判别器（AgentJev 架构，教程：
# Datawhale《打造属于你自己的 Jev》，训练/服务见 E:/AIStudio/xiaoke_jev/）。
# 训练完成后 config.laya.backend 设为 "self" 即整体切换，判定语义（kill-only）
# 与 laya 后端完全一致。
# ---------------------------------------------------------------------------

def _self_dup_flags(candidates: list[str], hist: list[str], base_url: str,
                    threshold_dup: float, threshold_new: float) -> list:
    """自有判别器判定。契约：POST /api/evaluate {state, questions:[{options}]}
    → answers 带 probabilities。按训练格式：state=「新故事梗+历史梗」成对文本。"""
    import time as _t
    import urllib.request

    _jev_root = Path(r"E:\AIStudio\xiaoke_jev\XiaokeAILabs\experiments\test_jev_open_source\text_jev_train")
    _health_url = f"{base_url}/health"
    alive = False
    try:
        urllib.request.urlopen(_health_url, timeout=2)
        alive = True
    except Exception:
        pass
    if not alive and _jev_root.exists():
        # 自动拉起（幂等：服务活着就不动；Windows 隐藏窗口脱离本进程）
        flags_ = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(
            [sys.executable, "scripts/serve.py", "--run-dir", "runs/cn_v4",
             "--backbone", "models/Qwen3-0.6B-Base", "--port", "8152"],
            cwd=str(_jev_root),
            stdout=open(_jev_root.parent.parent.parent / "serve.log", "ab"),
            stderr=subprocess.STDOUT, creationflags=flags_)
        t0 = _t.time()
        while _t.time() - t0 < 120:
            try:
                urllib.request.urlopen(_health_url, timeout=2)
                break
            except Exception:
                _t.sleep(3)

    # 兼容 str / {"core_trope": str} / {"text": str} 三种条目形态（跨章检测传 dict）
    def _txt(x):
        if isinstance(x, dict):
            return (x.get("core_trope") or x.get("text") or "").strip()
        return (x or "").strip()

    hist = [h for h in map(_txt, hist) if h][:12]
    if not hist:
        return [False] * len(candidates)
    reqs = []
    for cand in map(_txt, candidates):
        if not cand:
            continue
        for i, h in enumerate(hist):
            state = "新故事梗：「" + cand[:240] + "」" + "\n" + "历史梗：「" + h[:240] + "」"
            reqs.append({
                "id": f"r{len(reqs)}",
                "state": state,
                "questions": [{
                    "id": "dup",
                    "type": "choice",
                    "question": "新故事梗与历史梗是否本质重复？判重标准：题材相同且情节骨架相同"
                                "（只换人名、换场景、换职业但骨架一致也算重复）；题材与情节骨架都不同算新梗。",
                    "options": ["本质重复（同一故事骨架）", "本质新梗（题材或骨架不同）"],
                }],
            })
    # /api/evaluate 单请求内 1..32 个 state，分块
    out_pdup: list[float] = []
    for k in range(0, len(reqs), 32):
        chunk = reqs[k:k + 32]
        payload = {"requests": chunk}
        r = urllib.request.urlopen(
            urllib.request.Request(f"{base_url}/api/evaluate",
                                   json.dumps(payload).encode(),
                                   {"Content-Type": "application/json"}), timeout=60)
        data = json.loads(r.read().decode("utf-8"))
        for res in data.get("results") or []:
            for a in res.get("answers") or []:
                # engine 输出: {"distribution": {"0": p, "1": 1-p}, "value": "0", ...}
                probs = a.get("distribution") or a.get("probabilities") or {}
                if isinstance(probs, dict):
                    out_pdup.append(float(probs.get("0", 0.5)))
                elif isinstance(probs, list):
                    out_pdup.append(float(probs[0]))
                else:
                    out_pdup.append(0.5)
    out = []
    idx = 0
    for cand_raw in candidates:
        cand_txt = (cand_raw.get("core_trope") or cand_raw.get("text") or "").strip() \
            if isinstance(cand_raw, dict) else (cand_raw or "").strip()
        if not cand_txt:
            out.append(None)
            continue
        votes = out_pdup[idx:idx + len(hist)]
        idx += len(hist)
        sure_dup = any(p >= threshold_dup for p in votes)
        sure_new = all(p <= 1 - threshold_new for p in votes)
        out.append(True if sure_dup else (False if sure_new else None))
    return out


# ---------------------------------------------------------------------------
# api 后端：自行配置的外部判重 API（OpenAI 兼容 /chat/completions）。
# 用法：config.laya.backend="api" + api_url/api_key/api_model。
# 每个「候选 × 历史」成对发一个请求（并行 ≤8），模型必须返回 JSON {"dup": 0..1}；
# 单请求失败按 0.5 处理（kill-only 语义下不会误判 dup，只会交主模型仲裁）。
# ---------------------------------------------------------------------------

_API_SYSTEM = ("你是网络文学创作工作室的剧情判重器。判断『新故事梗』与『历史梗』是否本质重复。"
               "判重标准：题材相同且情节骨架相同（只换人名、换场景、换职业但骨架一致也算重复）；"
               "题材与情节骨架都不同算新梗。只输出 JSON，格式：{\"dup\": 0到1的小数}，"
               "dup 越接近 1 表示越肯定重复，越接近 0 表示越肯定是新梗，禁止输出其他内容。")


def _api_call(pair_idx: int, cand: str, h: str, api_url: str, api_key: str,
              api_model: str) -> float:
    """单对判重调用。返回 dup 概率（0=新 1=重复）；失败/解析失败 → 0.5。"""
    import time
    try:
        body = {
            "model": api_model,
            "messages": [
                {"role": "system", "content": _API_SYSTEM},
                {"role": "user", "content": f"新故事梗：「{cand[:240]}」\n历史梗：「{h[:240]}」"},
            ],
            "temperature": 0,
            "max_tokens": 32,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            api_url, json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        r = urllib.request.urlopen(req, timeout=120)
        data = json.loads(r.read().decode("utf-8"))
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = content.strip().strip("`").strip()
        if content.startswith("json"):
            content = content[len("json"):].strip()
        p = float(json.loads(content).get("dup", 0.5))
        return max(0.0, min(1.0, p))
    except Exception as e:  # noqa: BLE001
        print(f"[laya] api 判重请求失败（pair {pair_idx}）：{type(e).__name__}: {str(e)[:90]}")
        return 0.5


def _api_dup_flags(candidates: list[str], hist: list[str], api_url: str,
                   api_key: str, api_model: str, threshold_dup: float,
                   threshold_new: float) -> list:
    """外部 API 判重：两两成对，并行调用，kill-only 语义聚合。"""
    from concurrent.futures import ThreadPoolExecutor

    if not (api_url and api_key and api_model):
        print("[laya] backend=api 但未配置 api_url/api_key/api_model，本轮交主模型仲裁")
        return [None] * len(candidates)

    def _txt(x):
        if isinstance(x, dict):
            return (x.get("core_trope") or x.get("text") or "").strip()
        return (x or "").strip()

    hist = [h for h in map(_txt, hist) if h][:12]
    if not hist:
        return [False] * len(candidates)
    pairs: list[tuple[int, str, str]] = []
    for cand_raw in candidates:
        cand_txt = _txt(cand_raw)
        if not cand_txt:
            continue
        for h in hist:
            pairs.append((len(pairs), cand_txt, h))
    results: list[float] = [0.5] * len(pairs)
    if pairs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_api_call, i, c, h, api_url, api_key, api_model): i
                       for i, c, h in pairs}
            for fut in futures:
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception:  # noqa: BLE001
                    results[i] = 0.5
    out, idx = [], 0
    for cand_raw in candidates:
        cand_txt = _txt(cand_raw)
        if not cand_txt:
            out.append(None)
            continue
        votes = results[idx:idx + len(hist)]
        idx += len(hist)
        sure_dup = any(p >= threshold_dup for p in votes)
        sure_new = all(p <= 1 - threshold_new for p in votes)
        out.append(True if sure_dup else (False if sure_new else None))
    return out
