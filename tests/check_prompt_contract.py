# -*- coding: utf-8 -*-
"""Prompt 双向契约检查（纯标准库；确定性、零 token、不调用任何 LLM）。

运行：
    python tests/check_prompt_contract.py

四类检查：
    1. 正向：对每个 prompts/*.md（含 <!--TASK:-->）自动发现 {{占位符}}，排除自动注入的
       MARKET_GUIDE / GENRE_GUIDE，用占位值调用 render_prompt，必须成功且结果无 "{{" 残留。
    2. 反向：静态扫描 studio/*.py 的 render_prompt("<task>", ...) 关键字实参名：
       模板占位符是否都有代码传值（缺失 → FAIL）；注入名在模板里不存在（多余 → WARN）。
       **ctx 这类动态传参按其构造函数返回的 dict 键补齐（标注为"推断"），推断不出才降级 WARN。
    3. 模板结构：<!--TASK:名--> + ===SYSTEM=== + ===USER=== 三段齐全且顺序正确。
    4. 占位符安全：_safe_val 存在且真的被 render_prompt 用于消毒 {{ }}。

退出码：0 = 无失败项（可有 WARN）；1 = 存在失败项。
口径：检查器只报告不改文件；运行时真相由 smoke_test.py 的端到端断言兜底（本脚本不 import 它）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
STUDIO = ROOT / "studio"

AUTO_INJECT = {"MARKET_GUIDE", "GENRE_GUIDE"}  # 兜底值；main() 会从 prompts.py 的注入表动态刷新
TASK_RE = re.compile(r"<!--TASK:([^>]*)-->")
PH_RE = re.compile(r"\{\{(\w+)\}\}")

_ok = 0
_warn = 0
_fail = 0


def item(status: str, msg: str) -> None:
    global _ok, _warn, _fail
    if status == "ok":
        _ok += 1
        print(f"[ok] {msg}")
    elif status == "WARN":
        _warn += 1
        print(f"[WARN] {msg}")
    else:
        _fail += 1
        print(f"[FAIL] {msg}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def auto_inject_map() -> dict[str, str]:
    """从 studio/prompts.py 解析 ("占位名", "指南模板名") 配对，动态得到自动注入表。

    解析不到（注入写法被重构）就退回内置兜底并提示——不许让白名单悄悄放行新占位符。
    """
    src = read_text(STUDIO / "prompts.py")
    pairs = re.findall(r'\(\s*"([A-Z][A-Z0-9_]+)"\s*,\s*"([a-z][a-z0-9_]*)"\s*\)', src)
    return dict(pairs) if pairs else {"MARKET_GUIDE": "market_guide", "GENRE_GUIDE": "genre_guide"}


# ---------------------------------------------------------------------------
# 模板装载
# ---------------------------------------------------------------------------
def load_templates() -> dict[str, dict]:
    """返回 {stem: {text, task, placeholders, has_system, has_user}}。"""
    out: dict[str, dict] = {}
    for path in sorted(PROMPTS.glob("*.md")):
        text = read_text(path)
        m = TASK_RE.search(text)
        out[path.stem] = {
            "path": path,
            "text": text,
            "task": m.group(1).strip() if m else None,
            "placeholders": set(PH_RE.findall(text)),
        }
    return out


# ---------------------------------------------------------------------------
# 代码侧采集
# ---------------------------------------------------------------------------
def render_calls() -> list[dict]:
    """采集 studio/*.py 的 render_prompt 调用：task/kwargs/dynamic/行号。"""
    calls: list[dict] = []
    for path in sorted(STUDIO.glob("*.py")):
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError as exc:
            item("WARN", f"无法解析 {path.name}，跳过其契约扫描：{exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if fname != "render_prompt":
                continue
            task = None
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                task = node.args[0].value
            calls.append(
                {
                    "file": path.name,
                    "line": node.lineno,
                    "task": task,
                    "kwargs": {k.arg for k in node.keywords if k.arg},
                    "dynamic": any(k.arg is None for k in node.keywords),
                }
            )
    return calls


def ctx_builder_keys() -> set[str]:
    """推断 **ctx 动态传参的键集合：取"名字里带 ctx 的函数"返回的 dict 字面量键。

    当前唯一来源是 pipeline._chapter_ctx（注释明写"即 draft prompt 的全部占位符"）。
    找不到就返回空集，调用方降级为 WARN，绝不假装通过。
    """
    keys: set[str] = set()
    for path in sorted(STUDIO.glob("*.py")):
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "ctx" not in node.name.lower():
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    for k in sub.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
    return keys


# ---------------------------------------------------------------------------
# 1. 正向渲染
# ---------------------------------------------------------------------------
def check_forward(templates: dict[str, dict]) -> dict[str, object]:
    print("\n== 1. 正向：占位符全部可渲染 ==")
    sys.path.insert(0, str(ROOT))
    try:
        from studio.prompts import render_prompt

        item("ok", "成功导入 studio.prompts.render_prompt")
    except Exception as exc:  # noqa: BLE001
        item("FAIL", f"导入 studio.prompts 失败，正向检查无法进行：{exc}")
        return {"render": None}

    bad = []
    for stem, tpl in templates.items():
        vals = {ph: f"（占位值-{ph}）" for ph in tpl["placeholders"] if ph not in AUTO_INJECT}
        try:
            system, user = render_prompt(stem, **vals)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{stem}: {type(exc).__name__}: {exc}")
            continue
        leaked = sorted(set(re.findall(r"\{\{(\w+)\}\}", system + user)))
        if leaked:
            bad.append(f"{stem}: 渲染后仍残留占位符 {leaked}")
            continue
        if not user.strip():
            bad.append(f"{stem}: USER 段渲染为空")
    if bad:
        item("FAIL", f"正向渲染失败 {len(bad)} 个模板：")
        for b in bad:
            print(f"        - {b}")
    else:
        item("ok", f"全部 {len(templates)} 个模板均可渲染且无 {{ }} 残留（含自动注入指南）")
    return {"render": render_prompt}


# ---------------------------------------------------------------------------
# 2. 反向：代码传值 ↔ 模板占位符
# ---------------------------------------------------------------------------
def check_reverse(templates: dict[str, dict], calls: list[dict]) -> None:
    print("\n== 2. 反向：注入名 ↔ 占位符一一对应 ==")
    unknown_task = [f"{c['file']}:{c['line']}" for c in calls if not c["task"]]
    if unknown_task:
        item("WARN", f"task 名为动态表达式、无法静态校验的调用点：{unknown_task}")

    by_task: dict[str, list[dict]] = {}
    for c in calls:
        if c["task"]:
            by_task.setdefault(c["task"], []).append(c)

    def where(cs: list[dict]) -> str:
        return ", ".join(f"{c['file']}:{c['line']}" for c in cs)

    no_call = sorted(set(templates) - set(by_task))
    orphan_ph = [t for t in no_call if templates[t]["placeholders"]]
    pure_guide = [t for t in no_call if not templates[t]["placeholders"]]
    if orphan_ph:
        item("WARN", f"有占位符但无 render_prompt 调用方的模板：{orphan_ph}（孤立模板，见 static_check）")
    else:
        item("ok", "所有含占位符的模板都有调用方")
    if pure_guide:
        print(f"       （另有 {len(pure_guide)} 个无占位符的纯注入模板，按文件注入不经过 render：{pure_guide}）")

    builder = ctx_builder_keys()
    missing_all: list[str] = []
    extra_all: list[str] = []
    inferred: list[str] = []
    for task, cs in sorted(by_task.items()):
        tpl_ph = templates.get(task, {}).get("placeholders", set())
        provided: set[str] = set()
        for c in cs:
            provided |= c["kwargs"]
        dynamic = any(c["dynamic"] for c in cs)
        if dynamic and builder:
            provided |= builder
            inferred.append(f"{task}(+{len(builder)} 个 ctx 键)")
        need = tpl_ph - AUTO_INJECT
        miss = sorted(need - provided)
        if miss:
            if dynamic and not builder:
                item("WARN", f"{task}: 走 **ctx 动态传参且找不到 ctx 构造函数，无法静态判定缺失占位符 {miss}（靠正向渲染兜底）")
            else:
                missing_all.append(f"{task}: {miss}（调用点 {where(cs)}）")
        extra = sorted({k for c in cs for k in c["kwargs"]} - tpl_ph)
        if extra:
            hit = [where([c for c in cs if k in c["kwargs"]]) for k in extra]
            extra_all.append(f"{task}: {extra}（{'; '.join(hit)}）")

    if missing_all:
        item("FAIL", f"模板占位符缺代码传值 {len(missing_all)} 处（运行时会抛 KeyError）：")
        for m in missing_all:
            print(f"        - {m}")
    else:
        item("ok", f"反向检查：{len(by_task)} 个被调用模板占位符均有传值（缺失 0）")

    if extra_all:
        item("WARN", f"注入名在模板里不存在（无害但属冗余，建议删）：{extra_all}")
    else:
        item("ok", "无多余注入名")

    # 动态传参的推断结果必须可见：推断不出就 WARN，不许静默当成"通过"
    item("ok" if builder else "WARN", f"**ctx 动态传参键推断：{'命中 ' + str(len(builder)) + ' 个键' if builder else '未找到 ctx 构造函数（降级为 WARN）'}")
    if inferred:
        print(f"        推断覆盖：{', '.join(inferred)}")


# ---------------------------------------------------------------------------
# 3. 模板结构
# ---------------------------------------------------------------------------
def check_structure(templates: dict[str, dict]) -> None:
    print("\n== 3. 模板结构：TASK + SYSTEM + USER 三段 ==")
    bad: list[str] = []
    for stem, tpl in sorted(templates.items()):
        text, task = tpl["text"], tpl["task"]
        if not task:
            bad.append(f"{stem}: 缺 <!--TASK:名-->")
            continue
        if text.count("===SYSTEM===") != 1 or text.count("===USER===") != 1:
            bad.append(f"{stem}: SYSTEM/USER 标记数量异常（SYSTEM×{text.count('===SYSTEM===')} USER×{text.count('===USER===')}）")
            continue
        i_task = text.find("<!--TASK:")
        i_sys = text.index("===SYSTEM===")
        i_user = text.index("===USER===")
        if not (i_task < i_sys < i_user):
            bad.append(f"{stem}: 三段顺序错（应 TASK < SYSTEM < USER）")
            continue
        if not text[i_sys + len("===SYSTEM==="):i_user].strip():
            bad.append(f"{stem}: SYSTEM 段为空")
        elif not text[i_user + len("===USER==="):].strip():
            bad.append(f"{stem}: USER 段为空")
    if bad:
        item("FAIL", f"模板结构不合格 {len(bad)} 个：")
        for b in bad:
            print(f"        - {b}")
    else:
        item("ok", f"全部 {len(templates)} 个模板三段齐全、顺序正确、内容非空")


# ---------------------------------------------------------------------------
# 4. 占位符安全
# ---------------------------------------------------------------------------
def check_safe_val() -> None:
    print("\n== 4. 占位符安全：_safe_val 防回退 ==")
    src = read_text(STUDIO / "prompts.py")
    has_def = bool(re.search(r"^def _safe_val\(", src, re.M))
    used = "_safe_val(v)" in src or "_safe_val(v" in src
    sanitized = 'replace("{{"' in src and 'replace("}}"' in src
    ok = has_def and used and sanitized
    item("ok" if ok else "FAIL",
         f"_safe_val 存在={has_def} 被 render_prompt 使用={used} 替换 {{{{/}}}}={sanitized}"
         + ("" if ok else " ← 抓取文本里的 {{xxx}} 会让渲染崩，禁止移除转义"))


def main() -> int:
    global AUTO_INJECT
    print("短篇小说工作室 · Prompt 双向契约检查（零 token）")
    inject = auto_inject_map()
    AUTO_INJECT = set(inject)
    item("ok", f"自动注入占位符（从 prompts.py 解析）：{ {k: v for k, v in inject.items()} }")
    templates = load_templates()
    calls = render_calls()
    check_forward(templates)
    check_reverse(templates, calls)
    check_structure(templates)
    check_safe_val()
    print(f"\n{_ok} 项通过 / {_fail} 项失败（另有 {_warn} 项警告）")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
