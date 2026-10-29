# -*- coding: utf-8 -*-
"""LLM 客户端。

- OpenAICompatLLM: 任意 OpenAI 兼容接口（DeepSeek/GLM/Kimi/Qwen/Ollama/LM Studio...）
- MockLLM:        离线假模型，用于零成本跑通全流程 / 自测管线
"""
from __future__ import annotations

import json
import random
import re
import time

import requests

from .util import RateLimiter, extract_json


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 思考字段形态判定：reasoning 里到底是"被误放的正文"还是"思维链"
# ---------------------------------------------------------------------------
# 思维链常见标志（英文计划式措辞 / 提纲式段落头 / 自我指令）
_THINK_MARK_LLM = re.compile(
    r"(?i)(let me analyze|let me think|let's |i need to|i'll |^\s*first,\s|wait, |hmm,|"
    r"actually,? |requirements:|section \d|beat \d|beats to hit|options:|the task|we need |"
    r"word count|target words|opening hook|ending hook)",
    re.M,
)


def _reasoning_is_cot(raw: str) -> bool:
    """判断 reasoning/reasoning_content 字段内容是否为思维链（不能当正文用）。

    历史事故：glm-5.3 把 16384 输出预算全烧在英文思维链上（content 为空），
    旧代码把 reasoning 当正文回退，8 章落盘 19 万字（目标 9000）。

    判定（任一成立即思维链）：
      ① 开头 1200 字里计划式英文标志 ≥ 2 处；
      ② 英文字母数 > 汉字数（中文正文不会这样）；
      ③ 前 20 个非空行里"提纲式行"占比 > 40%（以 1./2.、(1)、- 或 Section/Beat 开头）。
    纯中文、无明显计划标记的内容才允许当正文回退使用。
    """
    t = (raw or "").strip()
    if not t:
        return True
    head = t[:1200]
    if len(_THINK_MARK_LLM.findall(head)) >= 2:
        return True
    cjk = len(re.findall(r"[\u4e00-\u9fff]", head))
    latin = len(re.findall(r"[A-Za-z]", head))
    if latin > cjk:
        return True
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()][:20]
    if lines:
        outline = sum(
            1 for ln in lines
            if re.match(r"^(?:\(?\d{1,2}[.)、]|[-*•]|#|Section|Beat|Step)\b", ln, re.I)
        )
        if outline / len(lines) > 0.4:
            return True
    return False


# ---------------------------------------------------------------------------
# 全局用量记录：每次调用追加一条，结束时按角色/模型汇总
# ---------------------------------------------------------------------------
USAGE_LOG: list[dict] = []


def record_usage(role: str, model: str, prompt_tokens: int, completion_tokens: int, est: bool = False):
    USAGE_LOG.append(
        {
            "role": role,
            "model": model,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "est": est,  # True = 服务端未报 usage，按字符估算
        }
    )


def usage_summary() -> dict:
    """按 角色+模型 汇总本次运行用量。"""
    out: dict[str, dict] = {}
    for u in USAGE_LOG:
        k = f"{u['role']}({u['model']})"
        agg = out.setdefault(
            k, {"role": u["role"], "model": u["model"], "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est": False}
        )
        agg["calls"] += 1
        agg["prompt_tokens"] += u["prompt_tokens"]
        agg["completion_tokens"] += u["completion_tokens"]
        agg["est"] = agg["est"] or u["est"]
    return out


def task_of(prompt: str) -> str | None:
    """prompt 模板首行约定 <!--TASK:XXX-->，用于日志与 Mock 分流；未标注返回 None。"""
    m = re.search(r"<!--TASK:([A-Z_]+)-->", (prompt or "")[:400])
    return m.group(1) if m else None


class BaseLLM:
    role = "anon"  # 角色标识，用于用量统计

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        raise NotImplementedError

    def chat_messages(self, messages: list[dict], json_mode: bool = False) -> str:
        """多轮消息接口（hybrid 单 writer 续写用）：直接传完整 messages 数组。"""
        raise NotImplementedError

    def chat_json(self, system: str, user: str):
        """带 JSON 修复重试：解析失败时带纠错提示重试一次（小模型常见问题）。

        模型偶发输出 JSON 数组：若数组首元素是对象则取之，否则按失败重试——
        本项目的阶段契约都是 JSON 对象（dict）。
        """
        raw = self.chat(system, user, json_mode=True)
        try:
            data = extract_json(raw)
            if isinstance(data, list):
                data = next((x for x in data if isinstance(x, dict)), None)
                if data is None:
                    raise ValueError("JSON 数组中不含对象")
            return data
        except ValueError:
            pass
        repair = (
            user
            + "\n\n【格式纠错】你上一次的输出无法解析为合法 JSON 对象。"
            "请重新回答，严格只输出一个合法 JSON 对象（{...}，可被 json.loads 解析），"
            "不要输出数组、解释、注释或 markdown 围栏。"
        )
        raw2 = self.chat(system, repair, json_mode=True)
        data = extract_json(raw2)
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), None)
            if data is None:
                raise ValueError("JSON 数组中不含对象")
        return data


class OpenAICompatLLM(BaseLLM):
    def __init__(self, cfg: dict):
        # 兼容用户直接填完整端点（base_url 以 /chat/completions 结尾时剥掉，
        # 后续统一按 f"{base_url}/chat/completions" 拼接，避免双重路径 404）
        base = cfg["base_url"].rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        self.base_url = base
        self.api_key = cfg.get("api_key", "")
        self.model = cfg["model"]
        self.temperature = float(cfg.get("temperature", 0.85))
        self.max_tokens = int(cfg.get("max_tokens", 8192))
        self.timeout = int(cfg.get("timeout", 300))
        self.retries = int(cfg.get("retries", 5))
        # 思考型模型（Qwen3.5/3.8、GLM、DeepSeek-R1 等）：本地引擎默认请求关闭思考，
        # 防止正文为空（思考进 reasoning/reasoning_content 字段）。云端默认不注入，
        # 需要时事态各异：用 config llm.extra_body 透传厂商参数（如智谱 thinking.disabled）。
        self.disable_thinking = bool(cfg.get("disable_thinking", False))
        self.extra_body = dict(cfg.get("extra_body") or {})
        self.limiter = RateLimiter(float(cfg.get("qps", 0) or 0))
        # 流式：bool 直接生效；"auto" 时本地引擎开启（长文生成可见进度）
        stream_cfg = cfg.get("stream", "auto")
        if isinstance(stream_cfg, bool):
            self.stream = stream_cfg
        else:
            self.stream = "local" == cfg.get("provider", "") or "127.0.0.1" in self.base_url or "localhost" in self.base_url
        self.session = requests.Session()
        # 配置连接池：避免坏连接复用导致 ConnectionResetError(10054)
        # max_retries=0：重试由本类自己控制（区分 429 / 连接错误）
        import requests.adapters as _ad
        _adapter = _ad.HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=0, pool_block=False
        )
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)
        _hdrs = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            # Cloudflare 拦截默认 python-requests UA；模拟 openai-python 客户端
            "User-Agent": "openai-python/1.0",
            # 不复用坏连接：每次请求后关闭，避免 keep-alive 挂死
            "Connection": "close",
        }
        # OpenCode Zen / Go 网关要求每个请求带 x-opencode-session（UUID），否则 400 MissingSessionID
        self._is_opencode = "opencode.ai" in self.base_url
        self.session.headers.update(_hdrs)

    # -- 用量统计 ------------------------------------------------------------
    def _record(self, prompt_chars: int, completion_chars: int, usage: dict | None):
        try:
            from . import runlog

            runlog.note_llm(self.role, self.model, int((usage or {}).get("completion_tokens") or completion_chars / 1.6))
        except Exception:  # noqa: BLE001
            pass
        pt = ct = None
        if usage:
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
        if pt is None:
            pt = int(prompt_chars / 1.6)  # 中文粗估：1 token ≈ 1.6 字符
        if ct is None:
            ct = int(completion_chars / 1.6)
        record_usage(self.role, self.model, pt, ct, est=usage is None)

    # -- 请求 ----------------------------------------------------------------
    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        return self.chat_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode,
        )

    def chat_messages(self, messages: list[dict], json_mode: bool = False) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.disable_thinking:
            # 不同厂商关思考参数不同，发错会触发 400（实测 opencode go 上
            # reasoning_effort=none 对流式请求返回 400）：
            # - 智谱 GLM 系列：thinking.type=disabled
            # - 其他 OpenAI 兼容端：reasoning_effort=none（不支持时在 4xx 分支剔除）
            if (self.model or "").startswith("glm-"):
                payload["thinking"] = {"type": "disabled"}
            else:
                payload["reasoning_effort"] = "none"
        if self.extra_body:
            payload.update(self.extra_body)
        if json_mode:
            # 部分兼容服务不支持 response_format，失败自动降级
            payload["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        attempt = 0
        while True:
            attempt += 1
            self.limiter.wait()
            try:
                if self.stream:
                    try:
                        text, usage = self._chat_stream(url, payload)
                        self._record(sum(len(m.get("content", "")) for m in messages), len(text), usage)
                        return text
                    except (requests.RequestException, LLMError) as e:
                        if attempt >= self.retries + 2:
                            raise
                        print(f"    [llm] 流式失败（{e}），降级为非流式重试")
                        self.stream = False
                        continue
                # OpenCode 网关每次请求都要新 session UUID（复用会触发网关断连）
                extra_headers = {}
                if self._is_opencode:
                    import uuid as _uuid
                    extra_headers["x-opencode-session"] = str(_uuid.uuid4())
                resp = self.session.post(url, json=payload, timeout=self.timeout, headers=extra_headers)
                if resp.status_code in (400, 404, 422):
                    # response_format / reasoning_effort / thinking 不被支持：剔除后重试
                    if "response_format" in payload:
                        payload.pop("response_format", None)
                        json_mode = False
                        continue
                    if "reasoning_effort" in payload:
                        payload.pop("reasoning_effort", None)
                        continue
                    if "thinking" in payload:
                        payload.pop("thinking", None)
                        continue
                resp.raise_for_status()
                data = resp.json()
                msg = data["choices"][0]["message"]
                text = msg.get("content")
                if not text and (msg.get("reasoning") or msg.get("reasoning_content")):
                    # 思考型模型把内容放进了 reasoning 字段。历史事故：glm-5.3 把
                    # 16384 输出预算全烧在英文思维链上，content 为空，这里"回退使用"
                    # 把思维链当正文落盘，8 章合计 19 万字（目标 9000）。
                    # 因此回退前必须判形态：思维链不许当正文；只有"确属正文被误放"才用。
                    raw_r = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
                    if _reasoning_is_cot(raw_r):
                        raise LLMError(
                            "模型把输出全放进了思考字段（疑似思维链，非正文）；"
                            "请在 llm_roles.<role>.extra_body 配置厂商关思考参数，或换非思考模型"
                        )
                    text = raw_r
                    if text and json_mode:
                        try:
                            from .util import extract_json as _ej

                            obj = _ej(raw_r)
                            if isinstance(obj, (dict, list)):
                                text = json.dumps(obj, ensure_ascii=False)
                        except Exception:  # noqa: BLE001
                            pass
                    if text:
                        print("    [llm] 内容在思考字段（reasoning），已回退使用；"
                              "可在 config 的 llm_roles.<role>.extra_body 配置厂商关思考参数")
                    else:
                        raise LLMError("模型返回空内容（思考字段也为空）")
                if not text:
                    raise LLMError("模型返回空内容")
                self._record(sum(len(m.get("content", "")) for m in messages), len(text), data.get("usage"))
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                rate_limited = status == 429
                # 429 限流多给两次机会、等更久（云厂商免费档并发/RPM 限制常见）；
                # 其他错误维持原有 retries 次指数退避
                max_attempts = self.retries + (2 if rate_limited else 0)
                if attempt >= max_attempts:
                    break
                if rate_limited:
                    ra = ""
                    try:
                        ra = (e.response.headers or {}).get("Retry-After") or ""
                    except Exception:  # noqa: BLE001
                        ra = ""
                    wait = (min(120, int(float(ra)))
                            if ra.replace(".", "").isdigit() else min(120, 5 * 3 ** attempt))
                else:
                    # 连接错误（ConnectionReset/SSL/ReadTimeout）：网关主动断连，需更长退避
                    # 524/500/502/503 服务端错误：Cloudflare 源站超时/网关过载，同样长退避
                    ename = type(e).__name__
                    if ename in ("ConnectionError", "ProtocolError", "SSLError",
                                 "ReadTimeout", "ConnectTimeout"):
                        wait = min(180, 10 * attempt * attempt)  # 10s/40s/90s/160s
                    elif status in (500, 502, 503, 524):
                        # Cloudflare 524 = 源站100秒未响应；5xx = 网关过载
                        # 等更久让服务端恢复，退避 15s/60s/135s/240s/375s
                        wait = min(300, 15 * attempt * attempt)
                    else:
                        wait = min(60, 2**attempt * 2)
                print(f"    [llm] 第{attempt}次调用失败：{e}，{wait}s 后重试…")
                time.sleep(wait)
        raise LLMError(f"LLM 调用失败（已重试{attempt - 1}次）：{last_err}")

    def _chat_stream(self, url: str, payload: dict) -> tuple[str, dict | None]:
        """SSE 流式读取，返回 (全文, usage|None)。带实时进度显示。"""
        sp = dict(payload)
        sp["stream"] = True
        # OpenCode 网关每次请求都要新 session UUID（复用会触发网关断连）
        extra_headers = {}
        if self._is_opencode:
            import uuid as _uuid
            extra_headers["x-opencode-session"] = str(_uuid.uuid4())
        resp = self.session.post(url, json=sp, timeout=self.timeout, stream=True, headers=extra_headers)
        resp.raise_for_status()
        resp.encoding = "utf-8"  # SSE 无 charset 时 requests 会误判为 ISO-8859-1，中文变乱码
        chunks: list[str] = []
        usage = None
        last_show = 0.0
        t0 = time.time()
        finish_reason = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
            delta = (choices[0] or {}).get("delta") or {}
            piece = delta.get("content")
            if piece:
                chunks.append(piece)
                now = time.time()
                if now - last_show > 1.5:  # 进度心跳
                    last_show = now
                    speed = sum(len(c) for c in chunks) / max(now - t0, 0.1)
                    print(f"\r    [gen] {sum(len(c) for c in chunks)} 字（{speed:.0f} 字/秒）…", end="", flush=True)
        if chunks:
            print("\r" + " " * 60 + "\r", end="", flush=True)  # 清除进度行
        text = "".join(chunks)
        if not text:
            raise LLMError("流式返回为空")
        # max_tokens 截断（finish_reason=length）：长文任务必须抛错重试，
        # 静默截断会让成稿残缺（部分网关只发 [DONE] 不带 finish_reason，None 视为正常）。
        if finish_reason == "length":
            raise LLMError(
                f"流式输出被 max_tokens 截断（finish_reason=length，{len(text)} 字），"
                "降级重试以获取完整内容"
            )
        return text, usage


# ---------------------------------------------------------------------------
# Mock：按任务阶段返回结构化假数据，保证全链路可离线跑通
# ---------------------------------------------------------------------------

_MOCK_NAMES = ["王秀兰", "李建国", "陈美凤", "张桂芳", "刘大强", "赵春梅", "周铁柱", "吴丽萍"]

_MOCK_OPENINGS = [
    "腊月二十八，我攥着三千八的退彩礼判决书，站在婆家门口，手冻得跟胡萝卜一样。",
    "我爸住院的第三天，我弟把救命钱转走了一半，说要付县城新房的首付。",
    "结婚十二年，我第一次把碗摔在我婆婆头上，瓷片崩到墙上，全家都静了。",
    "拆迁办的人走后，我爷把我爸和二叔叫到堂屋，桌上摆着房契和一把秤。",
]


def _mock_paragraph(seed: int, n_chars: int) -> str:
    rnd = random.Random(seed)
    parts = []
    who = rnd.sample(_MOCK_NAMES, 4)
    lines = [
        f"{who[0]}把筷子往桌上一拍：“这钱必须留给你弟，不然全村人怎么看咱们家？”",
        "我没说话，起身把炕柜打开，那件旧棉袄还压在底下，边角都磨白了。",
        f"{who[1]}在门口探头探脑，声音又尖又细：“哟，一家人这是唱哪出呢？”",
        "屋里静得能听见灶膛里柴火哔啵响。我妈的咳嗽声从里屋传来，一声比一声闷。",
        f"“{who[2]}，你说句公道话。”我爸把烟头摁灭在鞋底上，“这个家到底谁说了算。”",
        "我数过，从镇上到县医院，班车要颠四十七分钟。这段路，我这礼拜跑了三趟。",
        f"{who[3]}拉着我的手，手心全是汗：“丫头，忍忍吧，忍过这阵就好了。”",
        "忍？我忍了二十九年。今天这个头，我不磕了。",
    ]
    total = 0
    i = 0
    while total < n_chars:
        line = lines[i % len(lines)]
        parts.append(line)
        total += len(line)
        i += 1
    return "\n\n".join(parts)


def _mock_quote(user: str) -> str:
    """从提示词里的【章节全文】块摘一句真原文，供 mock 评审卡当证据。

    为什么要真摘而不是写死一句：评审卡的核心规则是"证据必须能在正文里找到"
    （studio/review.py 会逐条核对）。如果 mock 用固定假句子，离线测试就永远
    测不到"证据核对"这条主链路——恰好把最容易出错的地方跳过了。
    """
    m = re.search(r"【章节全文】\s*<<CHAPTER>>(.*?)<<END>>", user, re.S)
    body = (m.group(1) if m else user)
    for line in body.splitlines():
        s = line.strip()
        # 跳过标题行（"01 某某"）与摘要行，取第一句像样的正文
        if len(s) >= 12 and not re.match(r"^\d{1,2}\s", s) and not s.startswith("["):
            return s[:40]
    return (body.strip()[:40] or "无正文")


class OpenCodeServeLLM(BaseLLM):
    """通过本地 `opencode serve`（headless server）调用 OpenCode 免费层模型。

    OpenCode 免费模型（`*-free`）禁止裸 API 访问（403: free tier can only be used
    from within OpenCode），只能经 CLI 内部会话协议使用：
      POST /session                    → 创建会话
      POST /session/:id/message       → 发送消息并等待完整响应（同步）
    model 字段为对象：{"providerID": "opencode", "modelID": "<free模型名>"}

    本项目每次 chat 都是独立上下文，故每次调用新建 session、用后即删，
    避免会话历史污染与内存堆积。流式/非流式均按同步 message 接口处理。
    """

    def __init__(self, cfg: dict):
        base = cfg.get("base_url", "http://127.0.0.1:8567").rstrip("/")
        self.base_url = base
        self.model = cfg["model"]
        # 兼容 "opencode/deepseek-v4.1-flash-free" 或裸 "deepseek-v4.1-flash-free"
        self.model_id = self.model.split("/")[-1]
        self.temperature = float(cfg.get("temperature", 0.85))
        self.max_tokens = int(cfg.get("max_tokens", 8192))
        self.timeout = int(cfg.get("timeout", 300))
        self.retries = int(cfg.get("retries", 3))
        self.limiter = RateLimiter(float(cfg.get("qps", 0) or 0))
        self.session = requests.Session()
        import requests.adapters as _ad
        self.session.mount("http://", _ad.HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0, pool_block=False))
        self._ensure_once = False

    def _ensure_serve(self):
        """健康检查；本地 serve 未启动时自动拉起（opencode CLI 已安装时）。"""
        if self._ensure_once:
            return
        self._ensure_once = True
        try:
            requests.get(f"{self.base_url}/global/health", timeout=3)
            return
        except Exception:  # noqa: BLE001
            pass
        # 未启动 → 尝试拉起 `opencode serve`（Windows 后台进程，日志到 workspace/logs）
        import shutil, subprocess, os
        exe = shutil.which("opencode")
        if not exe:
            raise LLMError("opencode serve 未启动，且未找到 opencode CLI（PATH 中无 opencode），无法调用免费模型")
        port = self.base_url.rsplit(":", 1)[-1]
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace", "logs")
        os.makedirs(log_dir, exist_ok=True)
        info = open(os.path.join(log_dir, "opencode_serve.log"), "a", encoding="utf-8")
        err = open(os.path.join(log_dir, "opencode_serve_err.log"), "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [exe, "serve", "--port", port, "--print-logs"],
                stdout=info, stderr=err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:  # noqa: BLE001
            info.close(); err.close()
            raise LLMError(f"拉起 opencode serve 失败：{e}")
        # 等待就绪（最多 30s）
        import time as _t
        for _ in range(30):
            _t.sleep(1)
            try:
                requests.get(f"{self.base_url}/global/health", timeout=2)
                return
            except Exception:  # noqa: BLE001
                continue
        raise LLMError(f"opencode serve 拉起后仍未就绪（端口 {port}），请手动执行 `opencode serve --port {port}` 排查")

    def _new_session(self) -> str:
        self._ensure_serve()
        r = self.session.post(f"{self.base_url}/session", json={}, timeout=30)
        r.raise_for_status()
        sid = r.json().get("id")
        if not sid:
            raise LLMError(f"opencode serve 创建会话失败：{r.text[:200]}")
        return sid

    def _record(self, prompt_chars: int, completion_chars: int, usage: dict | None):
        try:
            from . import runlog

            runlog.note_llm(self.role, self.model, int((usage or {}).get("completion_tokens") or completion_chars / 1.6))
        except Exception:  # noqa: BLE001
            pass
        pt = ct = None
        if usage:
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
        if pt is None:
            pt = int(prompt_chars / 1.6)
        if ct is None:
            ct = int(completion_chars / 1.6)
        record_usage(self.role, self.model, pt, ct, est=usage is None)

    def _drop_session(self, sid: str):
        try:
            self.session.delete(f"{self.base_url}/session/{sid}", timeout=15)
        except Exception:  # noqa: BLE001
            pass

    def _post_message(self, sid: str, system: str, user: str, json_mode: bool) -> str:
        parts: list[dict] = []
        if system:
            parts.append({"type": "text", "text": system})
        parts.append({"type": "text", "text": user})
        if json_mode:
            parts.append({"type": "text", "text": "\n\n严格只输出合法 JSON 对象（{...}，可被 json.loads 解析），不要数组、解释、注释或 markdown 围栏。"})
        body = {
            "model": {"providerID": "opencode", "modelID": self.model_id},
            "parts": parts,
        }
        r = self.session.post(f"{self.base_url}/session/{sid}/message", json=body, timeout=self.timeout)
        if r.status_code in (400, 404, 422, 429):
            raise LLMError(f"opencode serve [{self.model_id}] HTTP {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        data = r.json()
        out_parts = data.get("parts", [])
        text = "".join(p.get("text", "") for p in out_parts if p.get("type") == "text")
        if not text:
            # deepseek-v4.1-flash-free 等思考型模型长文输出时内容可能全在 reasoning 里
            text = "".join(p.get("text", "") for p in out_parts if p.get("type") == "reasoning").strip()
            if text:
                print(f"    [llm] opencode serve [{self.model_id}] 内容在思考字段（reasoning），已回退使用")
        if not text:
            raise LLMError(f"opencode serve [{self.model_id}] 返回空文本：{str(data)[:300]}")
        return text

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 2):
            self.limiter.wait()
            sid = None
            try:
                sid = self._new_session()
                text = self._post_message(sid, system, user, json_mode)
                self._record(len(system) + len(user), len(text), None)
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[llm] opencode serve 调用失败（{attempt}/{self.retries + 1}）：{e}")
                time.sleep(min(2 * attempt, 10))
            finally:
                if sid:
                    self._drop_session(sid)
        raise LLMError(f"opencode serve [{self.model_id}] 重试 {self.retries} 次仍失败：{last_err}")

    def chat_messages(self, messages: list[dict], json_mode: bool = False) -> str:
        system = ""
        user_parts: list[str] = []
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", "")
            else:
                user_parts.append(m.get("content", ""))
        return self.chat(system, "\n\n".join(user_parts), json_mode)


class AnthropicCompatLLM(BaseLLM):
    """Anthropic 原生协议客户端（Claude Haiku 5.5 在 OpenCode Go 走 /v1/messages，不是 /v1/chat/completions）。"""

    def __init__(self, cfg: dict):
        base = cfg["base_url"].rstrip("/")
        if base.endswith("/messages"):
            base = base[: -len("/messages")]
        self.base_url = base
        self.api_key = cfg.get("api_key", "")
        self.model = cfg["model"]
        self.temperature = float(cfg.get("temperature", 0.85))
        self.max_tokens = int(cfg.get("max_tokens", 8192))
        self.timeout = int(cfg.get("timeout", 300))
        self.retries = int(cfg.get("retries", 3))
        self.disable_thinking = bool(cfg.get("disable_thinking", False))
        self.limiter = RateLimiter(float(cfg.get("qps", 0) or 0))
        self.session = requests.Session()
        import uuid as _uuid
        hdrs = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "User-Agent": "openai-python/1.0",
        }
        if "opencode.ai" in self.base_url:
            hdrs["x-opencode-session"] = str(_uuid.uuid4())
        self._is_opencode = "opencode.ai" in self.base_url
        self.session.headers.update(hdrs)

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        return self.chat_messages(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode,
        )

    def chat_messages(self, messages: list[dict], json_mode: bool = False) -> str:
        url = f"{self.base_url}/messages"
        system_text = ""
        msgs = []
        for m in messages:
            if m.get("role") == "system":
                system_text = m.get("content", "")
            else:
                msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": msgs,
        }
        # claude 新版本模型已弃用 temperature（opencode Go 网关会 400），配置值忽略
        if self.model.startswith("claude-"):
            pass
        else:
            payload["temperature"] = self.temperature
        if system_text:
            payload["system"] = system_text
        if self.disable_thinking:
            # 关闭扩展思考：否则长任务会把 max_tokens 全耗在 thinking 上、正文为空
            payload["thinking"] = {"type": "disabled"}
        if json_mode:
            payload["system"] = (payload.get("system", "") + "\n\n严格只输出合法 JSON 对象，不要 markdown 围栏或解释。").strip()

        last_err = None
        for attempt in range(1, self.retries + 3):
            self.limiter.wait()
            try:
                # OpenCode 网关每次请求都要新 session UUID（复用会触发网关断连）
                extra_headers = {}
                if self._is_opencode:
                    import uuid as _uuid
                    extra_headers["x-opencode-session"] = str(_uuid.uuid4())
                resp = self.session.post(url, json=payload, timeout=self.timeout, headers=extra_headers)
                if resp.status_code in (400, 404, 422):
                    body = resp.text[:500]
                    raise LLMError(f"HTTP {resp.status_code}: {body}")
                resp.raise_for_status()
                data = resp.json()
                parts = data.get("content", [])
                text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
                if not text:
                    raise LLMError(f"Claude 返回空 content: {str(data)[:300]}")
                usage = data.get("usage", {})
                self._record(len(system_text) + sum(len(m.get("content","")) for m in msgs), len(text),
                             {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")})
                return text
            except Exception as e:
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt >= self.retries + 2:
                    break
                wait = min(120, 2 ** attempt * 5)
                print(f"    [llm] 第{attempt}次调用失败：{e}，{wait}s 后重试…", flush=True)
                time.sleep(wait)
        raise LLMError(f"Anthropic 调用失败（已重试{self.retries}次）：{last_err}")

    def _record(self, prompt_chars: int, completion_chars: int, usage: dict | None):
        try:
            from . import runlog
            runlog.note_llm(self.role, self.model, int((usage or {}).get("completion_tokens") or completion_chars / 1.6))
        except Exception:
            pass


class MockLLM(BaseLLM):
    """离线模拟：解析任务标记，返回合法结构的假内容。"""

    def __init__(self, cfg: dict | None = None):
        self.calls = 0
        self.target_words = 1500

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        self.calls += 1
        stage = task_of(user) or task_of(system) or "UNKNOWN"
        if stage == "MINE":
            dm = re.search(r"必须优先产出「(.+?)」方向", user)
            direction = dm.group(1) if dm else "彩礼纠纷"
            return json.dumps(
                {
                    "concepts": [
                        {
                            "hot_title": "【 mock 热点】女子退婚被索要88万彩礼，村委会回应",
                            "source": "mock",
                            "direction": direction,
                            "core_trope": "退婚后天价彩礼引发全村站队，女主用一张十年前的旧婚书反杀",
                            "emotion": "怒→爽",
                            "conflict": "女主退婚 vs 男方全家+部分村民索要天价彩礼",
                            "hook": "判决书下来那天，男方母亲带着七八个亲戚堵在女主家门口",
                            "potential_score": 88,
                            "reason": "mock：贴合指定方向，情绪浓度高",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        if stage == "PITCH":
            return json.dumps(
                {
                    "titles": [
                        "退婚之后，全村人求我高抬贵手",
                        "三千八的彩礼，八十八万的债",
                        "我把彩礼判决书贴在了村口",
                    ],
                    "title": "退婚之后，全村人求我高抬贵手",
                    "logline": "被退婚还要赔八十八万的秀兰，用一张旧婚书把整个村的嘴都堵上了。",
                    "protagonist": {"name": "王秀兰", "age": 29, "occupation": "镇上裁缝铺老板娘", "traits": "外软内硬，认死理"},
                    "antagonist": {"name": "陈美凤", "role": "前婆婆", "traits": "泼辣护短，吃准秀兰要脸面"},
                    "core_conflict": "退婚天价彩礼 vs 旧婚书反杀",
                    "emotional_peaks": ["被堵门羞辱", "旧婚书当众揭晓", "村委评理大会反杀"],
                    "ending_type": "爽文打脸+恶人现世报",
                    "chapter_count": 6,
                    "first_person": True,
                },
                ensure_ascii=False,
            )

        if stage == "LEAD":
            lines = [
                "结婚一个月，陈志强睡了九百九十九个女人，还让我在门外听房记录。",
                "我记着日子，一笔一笔，写在他送我的爱马仕笔记本上。",
                "他以为我在记情诗。",
                "今晚他第十次放我鸽子，去陪林晚晚过生日。",
                "我拨通他助理的电话：“明早十点，民政局，我答应净身出户。",
                "不过，得先把他爸妈留给我的老宅过户手续办了。",
                "他不知道，那本子的最后一页，写着他的死刑。",
            ]
            return "导语：\n" + "\n".join(lines)

        if stage == "OUTLINE":
            return json.dumps(
                {
                    "chapters": [
                        {
                            "no": i + 1,
                            "title": ["判决书贴上村口", "堵门", "旧婚书", "评理大会", "反杀", "尘埃落定"][i],
                            "goal": f"第{i+1}章推进核心冲突并留钩子",
                            "beats": [f"冲突升级{i+1}A", f"关键信息揭晓{i+1}B", f"章末反转钩子{i+1}C"],
                            "open_hook": "上一章冲突的直接后果",
                            "end_hook": f"第{i+1}章结尾新危机",
                            "words_target": 1800,
                        }
                        for i in range(6)
                    ]
                },
                ensure_ascii=False,
            )

        if stage == "BIBLE":
            return json.dumps(
                {
                    "characters": [
                        {"name": "王秀兰", "role": "主角", "age": 29, "occupation": "裁缝铺老板娘", "traits": "外软内硬", "secret": "手里握着陈家的旧婚书"},
                        {"name": "陈美凤", "role": "反派", "age": 55, "occupation": "前婆婆", "traits": "泼辣护短", "secret": "当年挪用了秀兰父亲的工钱"},
                    ],
                    "foreshadows": [
                        {"id": "F1", "plant_chapter": 1, "content": "炕柜底压着的旧婚书", "payoff_chapter": 3},
                        {"id": "F2", "plant_chapter": 2, "content": "旧婚书复印件", "payoff_chapter": 4},
                    ],
                    "timeline": "腊月二十三起到正月十五",
                    "setting": "北方某县清河镇的秀兰裁缝铺与陈家村",
                    "tone_note": "第一人称‘我’=王秀兰，全篇克制但句句带刺",
                },
                ensure_ascii=False,
            )

        if stage == "DRAFT":
            # 章节正文：编号小节头 + [本章摘要] 行
            m = re.search(r"小节\s*(\d+)(?:/\d+)?[：:]\s*(.+)", user)
            no, title = (m.group(1), m.group(2).strip()) if m else ("01", "小节")
            n = max(500, min(self.target_words, 900))
            body = _mock_paragraph(self.calls * 7 + len(title), n)
            return (
                f"{int(no):02d} {title}\n\n{body}\n\n[本章摘要] mock摘要：本章围绕「{title}」推进冲突，秀兰与陈美凤正面交锋，结尾埋下新钩子。"
            )

        if stage == "REWRITE":
            m = re.search(r"小节\s*(\d+)\s*重写", user)
            no = m.group(1) if m else "01"
            mt = re.search(r"\n(\d{1,2})\s+([^\n]+)\n", user)
            title = mt.group(2).strip() if mt else "重写小节"
            n = max(500, min(self.target_words, 900))
            body = _mock_paragraph(self.calls * 11 + len(title), n)
            return f"{int(no):02d} {title}\n\n{body}\n\n[本章摘要] mock重写摘要：已吸收打回意见，冲突推进，结尾留钩子。"

        if stage == "REVIEW":
            m = re.search(r"审查轮次：(\d+)/(\d+)", user)
            rnd = int(m.group(1)) if m else 2
            # mock 也必须遵守「证据化评审」契约（prompts/review.md）：
            # 只回旧格式 {verdict,score,issues} 的话，整条 criteria→覆盖率→交付状态
            # 的链路在离线测试里永远走不到，等于新功能没有测试覆盖。这里从正文里
            # 真摘一句当 evidence，让 coverage/quality 在 mock 下也是真实算出来的。
            quote = _mock_quote(user)
            crit = [
                {"id": "C01", "name": "开头钩子", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：开篇有具体动作"},
                {"id": "C02", "name": "卡点密度", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：中段有对峙"},
                {"id": "C03", "name": "章末钩子", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：结尾留新危机"},
                {"id": "C04", "name": "情节拍完整", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：起承转齐"},
                {"id": "C05", "name": "文风", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：口语化"},
                {"id": "C06", "name": "连续性", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：与上章衔接"},
                {"id": "C07", "name": "人物一致", "status": "evaluated", "earned": 9,
                 "evidence": [quote], "rationale": "mock：人设未崩"},
                {"id": "C09", "name": "题材符合度", "status": "evaluated", "earned": 10,
                 "evidence": [quote], "rationale": "mock：符合追妻火葬场"},
            ]
            if rnd <= 1:
                # mock 故意首轮打回，验证“审查→重写→通过”闭环
                return json.dumps(
                    {
                        "verdict": "fail",
                        "score": 6.2,
                        "issues": [
                            {"criterion": "章末钩子", "location": "结尾", "note": "mock：钩子乏力", "fix": "结尾加新危机", "severity": "high"},
                            {"criterion": "卡点密度", "location": "中段", "note": "mock：两个拍之间过平", "fix": "插入一次当众对峙", "severity": "mid"},
                        ],
                        "criteria": crit,
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"verdict": "pass", "score": 8.6, "issues": [],
                               "criteria": crit}, ensure_ascii=False)

        if stage == "READER":
            m = re.search(r"评分轮次：（?(\d+)）?", user)
            rnd = int(m.group(1)) if m else 2
            pm = re.search(r'"persona":\s*"([^"]+)"', user)
            persona = pm.group(1) if pm else "mock读者"
            if rnd <= 1:
                # mock 首轮压分，验证“读者低分→带反馈重写→达标”闭环
                return json.dumps(
                    {
                        "persona": persona,
                        "score": 6.3,
                        "feel": "mock：中间有点拖",
                        "best_scene": "开头堵门那段",
                        "drop_point": "中段说理太多",
                        "would_pay": False,
                        "advice": "中段加点冲突，结尾钩子狠一点",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "persona": persona,
                    "score": 8.4,
                    "feel": "mock：这回带劲，看完还想追",
                    "best_scene": "评理大会",
                    "drop_point": "无",
                    "would_pay": True,
                    "advice": "保持",
                },
                ensure_ascii=False,
            )

        if stage == "EMOTION":
            m = re.search(r"情绪审查轮次：(\d+)/(\d+)", user)
            rnd = int(m.group(1)) if m else 2
            if rnd <= 1:
                # mock 首轮情绪不达标（偏虐/钩子弱），验证情绪门打回闭环
                return json.dumps(
                    {
                        "emotion_types": ["憋屈", "心疼"], "density_ok": True,
                        "strongest": "mock：堵门羞辱段", "strongest_score": 6.5,
                        "nue": 6, "shuang": 2, "nue_ratio": 0.62, "ratio_ok": False,
                        "dominant": "虐", "hook_type": "危机钩", "hook_score": 6.0,
                        "prev_hook_caught": True, "flat_paragraphs": ["中段说理"],
                        "toxic": [], "emotion_score": 6.2, "verdict": "fail",
                        "issues": [{"note": "mock：虐多爽少", "fix": "章尾加反击爽点"}, {"note": "mock：钩子弱", "fix": "结尾加新危机"}],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "emotion_types": ["解气", "爽", "期待"], "density_ok": True,
                    "strongest": "mock：评理大会反杀", "strongest_score": 8.5,
                    "nue": 2, "shuang": 7, "nue_ratio": 0.25, "ratio_ok": True,
                    "dominant": "爽", "hook_type": "期待钩", "hook_score": 8.0,
                    "prev_hook_caught": True, "flat_paragraphs": [], "toxic": [],
                    "emotion_score": 8.3, "verdict": "pass", "issues": [],
                },
                ensure_ascii=False,
            )

        if stage == "DECON_SECTION":
            seqs = re.findall(r"【段落 (\d+)", user)  # 兼容「【段落 N】」与「【段落 N｜全书位置约 M%】」两种排版
            rows = []
            for q_ in seqs:
                rows.append({
                    "seq": int(q_), "goal": "mock：立人设并引爆核心冲突",
                    "hook": "mock：门外传来了砸门声", "hook_type": "危机",
                    "conflict": "mock：钱 vs 面子", "escalation": "mock：筹码加大",
                    "emotion": "憋屈→爽(4)", "shuang": "mock：当众对质",
                    "shuang_type": "打脸", "nue": "mock：被堵门羞辱",
                    "info_gap": "mock：读者先知道旧婚书存在", "persona": "mock：只认账不认情的冷面女",
                    "technique": "mock：对话推剧情+具体钱数", "quote": "这钱，一分都不能少。",
                })
            return json.dumps({"sections": rows}, ensure_ascii=False)

        if stage == "DECON_BOOK":
            return json.dumps({
                "title_guess": "mock书名：她撕碎请假条那天，全厂都安静了",
                "direction": "县城职场反杀", "structure": "起25%立冲突/承50%叠加受压/转最大反转/合现世报",
                "pacing": "前30%密集爽点，中段蓄力，90%大爆发",
                "techniques": ["mock：对话占比过半", "mock：每500字卡点", "mock：钱数具体到个位"],
                "golden_quotes": ["忍？我忍了二十九年。"],
                "title_formula": "动作宣告+身份反差", "lessons": ["mock：反派别降智"],
            }, ensure_ascii=False)

        if stage == "MATERIAL_FROM_HOT":
            ids = re.findall(r"^([0-9]+)\. ", user, re.M)
            items = []
            for k, sid in enumerate(ids):
                usable = (k % 4 != 3)  # 每4条丢1条不可用的
                items.append({
                    "id": sid, "usable": usable,
                    "core_trope": "mock核心梗：妹妹顶替恩情，姐姐假死掀桌" if usable else "",
                    "hook": "mock钩子：判决书贴上村口那天，全家人才知道钱进了谁的口袋" if usable else "",
                    "direction": "彩礼纠纷", "conflict": "mock：尊严 vs 全家算计",
                })
            return json.dumps({"items": items}, ensure_ascii=False)

        if stage == "MATERIAL_CURATE":
            items = []
            for m in re.finditer(r"^(M\d+) \|", user, re.M):
                items.append(m.group(1))
            out = []
            for i, mid_ in enumerate(items):
                low = (i % 3 == 2)
                out.append({
                    "id": mid_,
                    "quality": "low" if low else ("high" if i % 3 == 1 else "mid"),
                    "keep": not low,
                    "reason": "mock：低质量删除" if low else "mock：可用",
                    "better_type": "", "better_direction": "", "tags_add": [],
                })
            return json.dumps({"items": out}, ensure_ascii=False)

        if stage == "IMPROVE":
            return json.dumps(
                {
                    "diagnosis": "mock：阅读曲线整体增长，但第3章留存明显掉队，与门禁预测的读者分存在偏差",
                    "lessons": [
                        {"dimension": "开头", "lesson": "第3章留存掉队，中段卡点不足", "action": "下一部前三章每章至少1个中爽点+1个章中钩子"},
                        {"dimension": "钩子", "lesson": "完读率低于预期", "action": "章尾钩子从危机钩升级为反转钩"},
                        {"dimension": "虐爽比", "lesson": "互动集中在反击段", "action": "虐段压缩到2章内，反击提前"},
                    ],
                    "next_try": "下一部验证：第3章放中反转能否拉起留存",
                },
                ensure_ascii=False,
            )

        if stage == "KB_DISTILL":
            dm = re.search(r"【题材方向】(.+?)（核心冲突", user)
            direction = dm.group(1).strip() if dm else "测试题材"
            return json.dumps(
                {
                    "direction": direction,
                    "summary": f"mock：写{direction}要钱数具体、亲戚站队、当众对账",
                    "facts": ["行情参考：县城彩礼普遍18.8万起", "镇中学晚自习到十点", "裁缝铺改羽绒服充绒一次80块"],
                    "scenes": ["村委评理大会", "镇医院走廊分摊医药费", "年三十堵门"],
                    "dialogues": ["这钱你必须给个说法", "我不是不讲理，我是讲不起这个理"],
                    "hooks": ["判决书贴上村口那天", "年三十的堵门"],
                    "pitfalls": ["反派别写成纯恶人", "别让主角靠巧合翻盘"],
                    "redlines": ["不影射真实案件当事人"],
                    "cases": [],
                },
                ensure_ascii=False,
            )

        if stage == "KB_QUERIES":
            return json.dumps({"queries": ["q1", "q2"]}, ensure_ascii=False)

        if stage == "POLISH_FIX":
            return json.dumps(
                {
                    "verdict": "pass",
                    "issues": [
                        {"location": "第1章 中段", "severity": "low", "type": "卡点密度", "note": "mock：该段可加一个小反转", "fix": "建议在旧婚书出现前加一句村妇窃语"}
                    ],
                    "stats_note": "mock 体检：结构完整，卡点密度达标",
                },
                ensure_ascii=False,
            )

        if stage == "POLISH_FINAL":
            # 直接回传正文（mock 不改写）
            m = re.search(r"<<FULL_TEXT>>(.*)", user, re.S)
            return m.group(1).strip() if m else user

        # 兜底
        return json.dumps({"mock": True, "stage": stage}, ensure_ascii=False)

    def chat_messages(self, messages: list[dict], json_mode: bool = False) -> str:
        # mock 不分轮次：取 system + 最后一条 user 走 chat（保持 DRAFT/REWRITE 分流一致）。
        # 注意 system 必须保留——`<!--TASK:XXX-->` 阶段标记在 system 段，丢了会落到兜底分支。
        system = next((m.get("content", "") for m in messages
                       if m.get("role") == "system"), "")
        last_user = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"), "")
        return self.chat(system, last_user, json_mode)


def get_llm(cfg: dict, role: str | None = None) -> BaseLLM:
    """按角色取 LLM：cfg.llm_roles 里配置了该角色就用独立模型，否则用主 llm。

    角色规划：writer(写手) / reviewer(主编+体检) / panel(读者团) /
             planner(提梗/方案/大纲/圣经) / light(知识卡蒸馏)
    """
    if role:
        rcfg = (cfg.get("llm_roles") or {}).get(role)
        # 角色配置必须至少有 base_url/provider/model 之一，否则回落主 llm（防空配置）
        if rcfg and (rcfg.get("base_url") or rcfg.get("provider") or rcfg.get("model")):
            key = "role:" + role
            if key not in _LLM_CACHE:
                merged = {**cfg["llm"], **rcfg}
                if merged.get("provider") == "local":
                    merged["api_key"] = ""  # 本地引擎不校验，也不该带云端密钥
                inst = _build_llm(merged, key)
                inst.role = role
                _LLM_CACHE[key] = inst
            return _LLM_CACHE[key]
    if "main" not in _LLM_CACHE:
        inst = _build_llm(cfg["llm"], "main")
        inst.role = "main"
        _LLM_CACHE["main"] = inst
    return _LLM_CACHE["main"]


def resolve_llm(cfg: dict, base: BaseLLM, role: str) -> BaseLLM:
    """管线内使用：主 provider 为 mock 时强制全链路 mock（忽略角色路由）。"""
    if isinstance(base, MockLLM):
        return base
    return get_llm(cfg, role)


_LLM_CACHE: dict[str, BaseLLM] = {}


def _build_llm(llm_cfg: dict, key: str) -> BaseLLM:
    prov = llm_cfg.get("provider", "auto")
    if prov == "mock":
        return MockLLM()
    # 本地 opencode serve：免费层模型（*-free）只能经 CLI 会话协议调用
    if prov == "opencode_serve" or (llm_cfg.get("model") or "").endswith("-free"):
        return OpenCodeServeLLM(llm_cfg)
    # Claude 走 Anthropic 原生协议 /v1/messages（OpenCode Go 上 claude-* 模型不支持 OpenAI 兼容）
    if (llm_cfg.get("model") or "").startswith("claude-"):
        return AnthropicCompatLLM(llm_cfg)
    if prov in ("openai", "openai_compat", "api", "local"):
        # local = 本地 OpenAI 兼容引擎（Ollama/LM Studio/llama-server），不校验 api_key；
        # 本地思考型模型默认关思考（thinking 进 reasoning 字段会掏空正文）
        if llm_cfg.get("provider") == "local" and "disable_thinking" not in llm_cfg:
            llm_cfg = {**llm_cfg, "disable_thinking": True}
        return OpenAICompatLLM(llm_cfg)
    # auto：有 key 用真实接口，否则 mock 并提示
    if llm_cfg.get("api_key"):
        return OpenAICompatLLM(llm_cfg)
    print(f"[llm] [{key}] 未配置 api_key，自动降级为 MockLLM（离线模式，内容为占位假文）")
    return MockLLM()
