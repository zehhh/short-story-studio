# -*- coding: utf-8 -*-
"""一条命令跑完所有离线检查（借鉴 openwrite 的「契约校验 + 一条命令全跑」B15）。

为什么需要：
    质量守卫分散在 5 个脚本里（compileall / static_check / prompt 契约 / 规则漂移 /
    冒烟测试），改完代码后没人会挨个记得跑；一旦漏跑，回归就留到下一次真实生产才炸。
    这里把它们串成一条命令，统一退出码，CI 或提交前直接 `python -m studio verify`。

用法：
    python -m studio verify            # 快速档：编译 + 导入 + 三个静态守卫（约 5-10s）
    python -m studio verify --full     # 全量档：额外跑 tests/smoke_test.py（MockLLM 全链路，约 2-3min）
    python -m studio verify --json     # 机器可读结果

退出码：0 = 全绿（允许 WARN）；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 检查项：(显示名, 命令, 是否属于全量档)
CHECKS: list[tuple[str, list[str], bool]] = [
    ("编译检查", [sys.executable, "-m", "compileall", "-q", "studio", "tests", "launcher.py"], False),
    ("模块导入", [sys.executable, "-c",
                  "import studio.pipeline, studio.agents, studio.llm, studio.cli, studio.dashboard,"
                  " studio.kb, studio.material, studio.memory, studio.rag, studio.publish,"
                  " studio.analytics, studio.scheduler, studio.typeset, studio.verify;"
                  "print('imports ok')"], False),
    ("静态可达性", [sys.executable, "tests/static_check.py"], False),
    ("提示词契约", [sys.executable, "tests/check_prompt_contract.py"], False),
    ("规则漂移", [sys.executable, "tests/check_rule_drift.py"], False),
    ("功能矩阵", [sys.executable, "tests/feature_matrix.py"], False),
    # 语言打磨的行为回归。单列一项而不是塞进功能矩阵：
    # 功能矩阵的断言多为"接线存在"，而这条链路曾经接线齐全却整体抛异常空转
    # （`len(int)` → TypeError 被 except 吞掉），只有喂真病句才测得出。
    ("语言打磨行为", [sys.executable, "tests/test_lang_polish_behaviour.py"], False),
    ("冒烟测试", [sys.executable, "tests/smoke_test.py"], True),
]


def _run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode, out.strip()
    except FileNotFoundError as e:
        return 127, f"命令不存在：{e}"
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    full = "--full" in args
    as_json = "--json" in args
    only = [a.split("=", 1)[1] for a in args if a.startswith("--only=")]

    rows = []
    t0 = time.time()
    for name, cmd, needs_full in CHECKS:
        if needs_full and not full:
            rows.append({"name": name, "code": None, "summary": "跳过（加 --full 运行）", "sec": 0.0})
            continue
        if only and not any(o in name for o in only):
            continue
        # 脚本类检查：脚本不存在时跳过而不是报错（新仓库/精简安装场景）
        if cmd[1].endswith(".py") and (ROOT / cmd[1]).exists() is False:
            rows.append({"name": name, "code": None, "summary": "跳过（脚本不存在）", "sec": 0.0})
            continue
        st = time.time()
        code, out = _run(cmd)
        sec = round(time.time() - st, 1)
        tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or [""]
        rows.append({"name": name, "code": code, "summary": tail[0].strip()[:120], "sec": sec,
                     "detail": out if code != 0 else ""})

    total = round(time.time() - t0, 1)
    failed = [r for r in rows if r["code"] not in (0, None)]
    if as_json:
        print(json.dumps({"ok": not failed, "rows": rows, "seconds": total},
                         ensure_ascii=False, indent=2))
    else:
        print("== 项目自检（studio verify）==")
        width = max(len(r["name"]) for r in rows) + 2
        for r in rows:
            mark = "跳过" if r["code"] is None else ("通过" if r["code"] == 0 else "失败")
            print(f"  {r['name']:<{width}} {mark:<4} {r['sec']:>6}s  {r['summary']}")
        print(f"\n共 {len(rows)} 项：{len(rows) - len(failed) - sum(1 for r in rows if r['code'] is None)} 通过 / "
              f"{len(failed)} 失败 / {sum(1 for r in rows if r['code'] is None)} 跳过，耗时 {total}s")
        for r in failed:
            print(f"\n----- {r['name']} 失败输出（尾部 40 行）-----")
            print("\n".join((r.get("detail") or "").splitlines()[-40:]))
        print("\n结论：" + ("全绿 ✔" if not failed else "有失败项，先修再提交 ✘"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
