# -*- coding: utf-8 -*-
"""短篇小说工作室 · 一键启动器。

双击「启动工作室.bat」即可进入本菜单；也支持命令行直通：
    python launcher.py run --idea "核心梗"
"""
from __future__ import annotations

import os
import subprocess
import sys


def _find_python() -> str:
    """优先选择装了项目依赖（requests/yaml）的系统 Python。

    避免 PATH 中的 python 指向无第三方包的解释器（如沙箱自带 Python），
    导致 `python -m studio` 报 ModuleNotFoundError。探测顺序：
    系统常见安装目录里的 Python3.x（新版本优先）→ 当前解释器。
    """
    from pathlib import Path

    cands: list[str] = []
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local.exists():
        cands.extend(
            str(p) for p in sorted(local.glob("Python3*/python.exe"), reverse=True)
        )
    cands.append(sys.executable)
    seen = set()
    for py in cands:
        if py in seen:
            continue
        seen.add(py)
        try:
            code = subprocess.run(
                [py, "-c", "import requests, yaml"], capture_output=True, timeout=20
            )
            if code.returncode == 0:
                return py
        except Exception:  # noqa: BLE001
            continue
    return sys.executable


PY = _find_python()


def _setup_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def run(args: list[str]) -> int:
    print(f"\n>>> python -m studio {' '.join(args)}\n")
    code = subprocess.run([PY, "-m", "studio", *args]).returncode
    print()
    return code


def ask(prompt: str, default: str = "") -> str:
    raw = input(f"{prompt}").strip()
    return raw or default


def pick_project() -> str | None:
    from pathlib import Path

    base = Path("workspace/projects")
    if not base.exists():
        print("还没有项目。先创作一篇。")
        return None
    dirs = sorted(d.name for d in base.iterdir() if d.is_dir())
    if not dirs:
        print("还没有项目。先创作一篇。")
        return None
    for i, d in enumerate(dirs[-8:], 1):
        print(f"  [{i}] {d}")
    raw = ask("选择项目序号（回车=最新）：")
    if not raw:
        return dirs[-1]
    try:
        return dirs[-8:][int(raw) - 1]
    except (ValueError, IndexError):
        print("无效序号")
        return None


def menu_create_idea():
    idea = ask("输入核心梗（如：婆婆把我陪嫁房过户给小叔子，我用公证反击）：")
    if not idea:
        print("使用演示核心梗（Mock 模式可直接体验流程）")
        run(["run", "--idea", "婆婆把我陪嫁房过户给小叔子，我用公证反击"])
        return
    platform = ask("目标平台 zhihu/fanqie/uc/qimao（回车=zhihu）：", "zhihu")
    words = ask("目标字数（回车=6000）：", "6000")
    run(["run", "--idea", idea, "--platform", platform, "--words", words])


def menu_create_hot():
    sources = ask("热点来源（回车=zhihu,baidu,douyin,toutiao）：", "zhihu,baidu,douyin,toutiao")
    run(["run", "--source", sources, "--pick", "ask"])


def menu_dashboard():
    print("看板启动后浏览器会自动打开（Ctrl+C 停止）")
    run(["dashboard"])


def menu_hot():
    run(["hot", "--top", "20"])


def menu_publish():
    name = pick_project()
    if name:
        run(["publish", "--project", name, "--mode", "manual"])


def menu_improve():
    name = pick_project()
    if not name:
        return
    src = ask("数据来源 mock/custom（回车=mock 演示）：", "mock")
    run(["analytics", "fetch", "--project", name, "--source", src])
    run(["improve", "--project", name])


def menu_schedule_list():
    run(["schedule", "list"])
    name = ask("要立即运行的任务 ID/名称（回车=跳过）：")
    if name:
        run(["schedule", "run", "--job", name])


def menu_loop():
    while True:
        print("=" * 52)
        print("  短篇小说工作室 · 一键启动")
        print("=" * 52)
        print("  1. 一键创作（输入核心梗 → 自动成稿）")
        print("  2. 蹭热点创作（抓平台热点 → 提梗 → 成稿）")
        print("  3. 打开可视化看板")
        print("  4. 看今日热点榜")
        print("  5. 发布最新作品（生成投稿包）")
        print("  6. 回收阅读数据 + 数据复盘")
        print("  7. 环境体检（模型/热点源/资产）")
        print("  8. 启动定时调度器（到点自动写文）")
        print("  9. 定时任务管理（列表/立即运行）")
        print("  0. 退出")
        print("-" * 52)
        choice = input("选择：").strip()
        try:
            if choice == "1":
                menu_create_idea()
            elif choice == "2":
                menu_create_hot()
            elif choice == "3":
                menu_dashboard()
            elif choice == "4":
                menu_hot()
            elif choice == "5":
                menu_publish()
            elif choice == "6":
                menu_improve()
            elif choice == "7":
                run(["check"])
            elif choice == "8":
                print("调度器到点自动写文；Ctrl+C 停止。建议保持窗口开着。")
                run(["scheduler"])
            elif choice == "9":
                menu_schedule_list()
            elif choice == "0":
                print("再见！")
                return 0
            else:
                print("无效选择")
                continue
        except KeyboardInterrupt:
            print("\n（已中断）")
        except SystemExit as e:
            print(f"\n（命令退出：{e}）")
        input("\n回车返回菜单…")


def main() -> int:
    _setup_stdout()
    if len(sys.argv) > 1:  # 命令行直通：launcher.py run --idea xxx
        return run(sys.argv[1:])
    try:
        return menu_loop()
    except (EOFError, KeyboardInterrupt):
        print("\n再见！")
        return 0


if __name__ == "__main__":
    sys.exit(main())
