# -*- coding: utf-8 -*-
"""番茄小说草稿箱适配器（Playwright 浏览器自动化）。

现实约束：番茄作家助手（writer.changdunovel.com）无公开 API，社区接口随时失效，
因此选择「官方 UI 流程自动化」这条最稳的路径：
  - 登录态复用：首次人工扫码/验证码登录后，storage_state 持久化到
    <workspace>/fanqie_state.json，之后脚本直接带态运行（有效期约 30 天）；
  - 只进草稿箱：脚本只完成「新建作品 → 逐章粘贴 → 存草稿」，绝不自动点
    「提交审核」，发布由作者本人在后台确认——把不可逆动作留给人；
  - 频控：章节间随机 3~7 秒间隔，单会话章节上限可配，模拟人工节奏。

前置（一次性）：
  pip install playwright && playwright install chromium
  python -m studio.fanqie_draft login     # 打开浏览器人工登录，回车保存态

使用：
  python -m studio.fanqie_draft push <project_name>   # 排版并推入草稿箱
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

STATE_FILE = "fanqie_state.json"
WRITER_URL = "https://writer.changdunovel.com"


def state_path(cfg: dict) -> Path:
    ws = Path(cfg["studio"].get("workspace", "workspace"))
    ws.mkdir(exist_ok=True)
    return ws / STATE_FILE


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        raise SystemExit(
            "未安装 Playwright。请执行：\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            "  python -m studio.fanqie_draft login   # 首次人工登录保存态\n"
            "安装前也可用 publish --mode manual 的投稿包手动发布。"
        )


# ---------------------------------------------------------------- 登录态

def login(cfg: dict) -> str:
    """打开真实浏览器人工登录，把 storage_state 保存到 workspace。"""
    _require_playwright()
    from playwright.sync_api import sync_playwright

    p = state_path(cfg)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(WRITER_URL)
        input("请在浏览器中完成登录（看到作家后台首页后），回到这里按回车保存登录态…")
        ctx.storage_state(path=str(p))
        browser.close()
    return str(p)


def _new_context(pw, cfg: dict):
    sp = state_path(cfg)
    if not sp.exists():
        raise SystemExit(f"缺少登录态 {sp}，请先执行 python -m studio.fanqie_draft login")
    return pw.chromium.launch_persistent_context(
        str(sp.parent / ".fanqie_profile"),
        storage_state=str(sp), headless=True,
        viewport={"width": 1366, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
    )


# ---------------------------------------------------------------- 推草稿

def push_draft(cfg: dict, ts: dict, max_chapters: int = 50,
               progress=lambda s: None) -> dict:
    """排版结果 ts 推入番茄草稿箱。返回 {status, book, chapters, note}。"""
    _require_playwright()
    from playwright.sync_api import sync_playwright

    settings = cfg.get("publish", {}).get("fanqie_draft", {}) or {}
    max_chapters = int(settings.get("max_chapters_per_run", max_chapters))
    result = {"status": "failed", "book": ts["book"]["title"], "chapters": [], "note": ""}

    with sync_playwright() as pw:
        ctx = _new_context(pw, cfg)
        page = ctx.new_page()
        try:
            page.goto(WRITER_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if "login" in page.url.lower() or page.get_by_text("扫码", exact=False).count():
                raise SystemExit("登录态已失效，请重新执行 python -m studio.fanqie_draft login")
            progress("登录态有效，进入作品管理")
            page.goto(WRITER_URL + "/work/all", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # 新建作品（若同名作品已建则直接进入；找不到按钮时提示人工建一次）
            btn = page.get_by_role("button", name="新建作品").first
            if btn.count() == 0:
                btn = page.get_by_text("新建作品", exact=False).first
            if btn.count():
                btn.click()
                page.wait_for_timeout(1500)
                for sel, val in [(".book-name-input input, input[name='bookName']", ts["book"]["title"]),
                                 (".book-intro textarea, textarea[name='intro']", ts["book"]["intro"])]:
                    if sel and val:
                        box = page.locator(sel).first
                        if box.count():
                            box.fill(val)
                progress("已填书名/简介，封面需后台手动传（点击存草稿后脚本继续逐章）")
            else:
                progress("未找到「新建作品」按钮——若作品已存在请忽略；否则请在后台手动建一次并重跑")

            # 逐章：新建章节 → 填标题/正文 → 存草稿
            for c in ts["chapters"][:max_chapters]:
                page.get_by_text("新建章节", exact=False).first.click()
                page.wait_for_timeout(1200)
                title_box = page.locator(".chapter-title input, input[placeholder*='章节']").first
                if title_box.count():
                    title_box.fill(f"{c['no']:02d} {c['title']}".strip())
                body_box = page.locator(".ProseMirror, .ql-editor, [contenteditable='true']").first
                if body_box.count():
                    body_box.click()
                    # 粘贴式灌入比 fill 更接近人工，且保留段落空行
                    page.keyboard.insert_text(c["content"])
                page.wait_for_timeout(800)
                page.get_by_text("存草稿", exact=False).first.click()
                result["chapters"].append({"no": c["no"], "words": c["words"], "ok": True})
                progress(f"小节{c['no']:02d}已存草稿（{c['words']}字）")
                time.sleep(random.uniform(3, 7))
            result.update(status="draft_ok",
                          note="全部章节已进草稿箱。请到番茄作家后台人工检查排版并提交审核。")
        finally:
            # 回写最新 storage_state，滚动续期
            try:
                ctx.storage_state(path=str(state_path(cfg)))
            except Exception:  # noqa: BLE001
                pass
            ctx.close()
    return result


# ---------------------------------------------------------------- CLI

def _main() -> None:
    import sys

    from .config import load_config

    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        print(f"登录态将保存到：{state_path(cfg)}")
        login(cfg)
        print("已保存。之后可执行：studio publish --project <名> --mode fanqie_draft")
    elif cmd == "push":
        from .store import find_project
        from .typeset import typeset

        name = sys.argv[2] if len(sys.argv) > 2 else ""
        project = find_project(Path(cfg["studio"].get("workspace", "workspace")).resolve(), name)
        pitch = project.load_json("pitch.json") or {}
        body = project.root / "final" / "成稿.txt"
        ts = typeset(body.read_text(encoding="utf-8"), book_title=pitch.get("title", ""),
                     logline=pitch.get("logline", ""))
        res = push_draft(cfg, ts, progress=lambda s: print(s))
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    _main()
