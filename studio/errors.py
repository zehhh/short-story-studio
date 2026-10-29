# -*- coding: utf-8 -*-
"""统一错误封套：稳定错误码 + request_id + 四元组诊断（借鉴 openwrite B04）。

为什么需要：
    旧代码失败时只 print 一句中文，用户报障时说不出"哪一步、什么位置、期望什么、
    怎么改"，开发者只能靠猜。openwrite 的做法是——所有面向用户的失败都带
    稳定 `code`、`request_id`，以及「位置 / 原文 / 阈值 / 改法」四元组，
    用户贴一行就够定位，日志里能按 request_id 串起整轮。

用法：
    raise StudioError("LLM_UNREACHABLE", f"接口 {base_url} 连接失败",
                      where="studio/llm.py:chat", found=str(e),
                      expect="HTTP 200", fix="检查 api_key / 网络 / 是否用了代理")

    python -m studio run ...   # 失败输出形如：
    [LLM_UNREACHABLE] 接口 ... 连接失败 (request_id=ab12cd34ef56)
      位置: studio/llm.py:chat
      实际: ConnectionError(...)
      期望: HTTP 200
      改法: 检查 api_key / 网络 / 是否用了代理
"""
from __future__ import annotations

import os
import uuid

# 本轮进程的 request_id：日志/错误封套共用，便于把一次运行的所有输出串起来
REQUEST_ID = (
    os.environ.get("STUDIO_REQUEST_ID")
    or f"{uuid.uuid4().hex[:8]}-{os.getpid()}"
)

# 稳定错误码 → 默认改法（面向用户，可直接照做）
CODES: dict[str, str] = {
    "LLM_UNREACHABLE": "检查 config.yaml 的 llm.base_url / api_key / model，或运行 `python -m studio check`",
    "LLM_AUTH": "api_key 无效或过期：换 key，或换 base_url 对应的服务商",
    "LLM_EMPTY": "模型返回空内容：多为思考型模型把输出预算烧在 reasoning 上，"
                 "换非思考模型或在 llm_roles.<role>.extra_body 关思考",
    "LLM_THINK_LEAK": "模型把思维链当正文返回：关思考 / 换模型 / 降低单次输出长度",
    "LLM_JSON": "模型输出无法解析为 JSON：换更强模型、降低 temperature、检查提示词里的 JSON 示例",
    "PROJECT_BUSY": "该项目正被另一个进程写入：等它结束；确认无残留进程后可删除项目下的 project.lock",
    "PROJECT_BROKEN": "项目文件损坏（多半是上次被强杀留下半截 JSON）：删除该文件后重跑对应阶段",
    "ASSET_MISSING": "资产缺失/损坏：检查 data/ 与 prompts/ 是否完整（可跑 `python -m studio check`）",
    "INPUT_INVALID": "参数不合法：看 `--help` 或 README 的命令示例",
    "NETWORK": "网络不可达：检查代理/网关，或用 `--provider mock` 离线跑",
    "INTERRUPTED": "运行被中断：项目状态已保存，用 `python -m studio continue` 续跑",
    "INTERNAL": "内部错误：带 request_id 反馈，并附上 workspace/logs 中同名日志",
}


class StudioError(RuntimeError):
    """带稳定错误码与四元组诊断的业务异常。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        where: str = "",
        found: str = "",
        expect: str = "",
        fix: str = "",
        cause: BaseException | None = None,
    ):
        self.code = code if code in CODES else "INTERNAL"
        self.message = message or CODES.get(self.code, "")
        self.where = where
        self.found = found
        self.expect = expect
        self.fix = fix or CODES.get(self.code, "")
        self.cause = cause
        super().__init__(message)

    def envelope(self) -> dict:
        d = {"code": self.code, "message": self.message, "request_id": REQUEST_ID}
        for k in ("where", "found", "expect", "fix"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d

    def __str__(self) -> str:
        head = f"[{self.code}] {self.message} (request_id={REQUEST_ID})"
        lines = [head]
        if self.where:
            lines.append(f"  位置: {self.where}")
        if self.found:
            lines.append(f"  实际: {self.found}")
        if self.expect:
            lines.append(f"  期望: {self.expect}")
        if self.fix:
            lines.append(f"  改法: {self.fix}")
        return "\n".join(lines)


def classify(exc: BaseException) -> str:
    """把常见底层异常归类成稳定错误码（用于兜底包装）。"""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if name in ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "SSLError"):
        return "NETWORK"
    if "401" in text or "unauthorized" in low or "invalid api key" in low:
        return "LLM_AUTH"
    if "429" in text:
        return "LLM_UNREACHABLE"
    if name == "KeyboardInterrupt":
        return "INTERRUPTED"
    if name == "JSONDecodeError":
        return "LLM_JSON"
    return "INTERNAL"
