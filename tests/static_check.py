# -*- coding: utf-8 -*-
"""静态可达性检查（纯标准库 + 可选 pyyaml；确定性、零 token、不改任何文件）。

运行：
    python tests/static_check.py

覆盖 6 类（详见 docs/dev/VALIDATION_MATRIX.md）：
    1. prompt 模板可达性：每个 prompts/*.md 是否被 render_prompt("<stem>")/第二个参数/
       config.knowledge.genre_guides 引用；孤立模板只报 WARN（当前预期：write_section.md）。
    2. data 资产可达性：data/*.json 是否被 studio/*.py 按文件名引用；knowledge/*.json 走目录扫描。
    3. 知识卡与题材方向一致性：data/tropes.json 的 direction id 集合 vs data/knowledge/*.json → 不一致即 FAIL。
    4. config 叶子键可达性：config.yaml / config.example.yaml 的叶子键是否在 studio/*.py 出现 → WARN。
    5. 读者画像 / 平台 / 题材方向 JSON 结构完整性（必填字段）。
    6. 代码卫生：裸 except（FAIL）、print( 数量（WARN 阈值）、超 250 行函数（WARN）。

退出码：0 = 无失败项（可有 WARN）；1 = 存在失败项。
设计纪律：检查器只报告，绝不自动改文件；数据可缺时才降级为 WARN，结构/一致性硬伤才 FAIL。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

try:  # 控制台/重定向都按 UTF-8 输出，避免 GBK 编码失败
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
DATA = ROOT / "data"
STUDIO = ROOT / "studio"
# 本脚本从 tests/ 目录启动，sys.path[0] 是 tests/ —— 想 import studio.* 必须显式加仓库根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 自动注入白名单：prompts.py 内部会把这两个占位符替换成 market_guide/genre_guide 模板正文，
# 不是业务代码里的 render_prompt("<stem>") 调用，故单列（并反向校验 prompts.py 仍在做注入）。
# 注意：白名单不是硬编码的，check_prompt_reachability 会从 prompts.py 的注入表动态解析。
MAX_FUNC_LINES = 250
PRINT_WARN_THRESHOLD = 2000

_ok = 0
_warn = 0
_fail = 0


def item(status: str, msg: str) -> None:
    """打印一条检查项并计数。status: ok / WARN / FAIL。"""
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


# ---------------------------------------------------------------------------
# 通用：源码读取 / AST 采集
# ---------------------------------------------------------------------------
def read_text(path: Path) -> str:
    """读文本；utf-8-sig 兼容 kb_dedup.py 的 BOM。"""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def studio_sources() -> dict[str, str]:
    return {p.name: read_text(p) for p in sorted(STUDIO.glob("*.py"))}


def render_calls(sources: dict[str, str]) -> list[dict]:
    """静态采集 render_prompt / load_prompt / prompt_path 的字面量调用。"""
    calls: list[dict] = []
    for name, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:  # 语法坏掉由 compileall 负责，这里跳过不掩盖
            print(f"[WARN] 无法解析 {name}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if fname not in ("render_prompt", "load_prompt", "prompt_path"):
                continue
            consts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            calls.append(
                {
                    "file": name,
                    "line": node.lineno,
                    "func": fname,
                    "task": consts[0] if consts else None,
                    "other": consts[1:],
                    "dynamic": any(k.arg is None for k in node.keywords),
                }
            )
    return calls


def yaml_load(path: Path):
    """读 YAML：优先 pyyaml（项目已依赖），不可用时返回 None 让调用方走正则降级。"""
    try:
        import yaml  # noqa: PLC0415  （延迟导入：缺失时只降级不崩）

        return yaml.safe_load(read_text(path))
    except Exception:  # noqa: BLE001
        return None


def iter_leaves(node, path=()):
    """遍历配置叶子：(键路径, 值)。标量列表的父键视为叶子。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from iter_leaves(v, path + (str(k),))
    elif isinstance(node, list):
        if all(not isinstance(v, (dict, list)) for v in node):
            yield path, node
        else:
            for i, v in enumerate(node):
                yield from iter_leaves(v, path + (f"[{i}]",))
    else:
        yield path, node


# ---------------------------------------------------------------------------
# 1. prompt 模板可达性
# ---------------------------------------------------------------------------
def check_prompt_reachability(sources: dict[str, str]) -> None:
    print("\n== 1. prompt 模板可达性 ==")
    templates = sorted(p.stem for p in PROMPTS.glob("*.md"))
    calls = render_calls(sources)
    referenced = {c["task"] for c in calls if c["func"] in ("render_prompt", "load_prompt", "prompt_path") and c["task"]}
    referenced |= {s for c in calls for s in c["other"]}  # render_prompt 的第二个及以后参数

    cfg = yaml_load(ROOT / "config.yaml")
    if isinstance(cfg, dict):
        guides = ((cfg.get("knowledge") or {}).get("genre_guides") or {})
    else:  # pyyaml 缺失：正则抠 genre_guides 块，保持检查可用
        guides = {}
        text = read_text(ROOT / "config.yaml")
        m = re.search(r"^  genre_guides:\s*$", text, re.M)
        if m:
            for line in text[m.end():].splitlines():
                if line and not line.startswith("    "):
                    break
                hit = re.match(r"\s+([^#:]+):\s*(\S*\.md)\s*$", line)
                if hit:
                    guides[hit.group(1).strip()] = hit.group(2)
    guide_values = {v for v in guides.values() if v}
    referenced |= {Path(v).stem for v in guide_values}

    # 自动注入白名单：从 prompts.py 的 ("占位名","指南模板名") 配对动态解析，
    # 并反向守卫——白名单里的指南文件必须真的还被注入，否则白名单就成了免死金牌。
    prompts_src = read_text(STUDIO / "prompts.py")
    pairs = re.findall(r'\(\s*"([A-Z][A-Z0-9_]+)"\s*,\s*"([a-z][a-z0-9_]*)"\s*\)', prompts_src)
    inject = dict(pairs) if pairs else {"MARKET_GUIDE": "market_guide", "GENRE_GUIDE": "genre_guide"}
    referenced |= set(inject.values())
    alive = bool(pairs) and all(ph in prompts_src for ph in inject)
    item("ok" if alive else "FAIL", f"自动注入仍由 prompts.py 实现：{inject}（{alive}）")

    # config 里的题材指导映射值必须真实存在
    missing_guides = sorted(v for v in guide_values if not (PROMPTS / v).exists())
    item("ok" if not missing_guides else "FAIL", f"config.knowledge.genre_guides 的值均存在于 prompts/（缺失 {missing_guides}）")

    orphans = [t for t in templates if t not in referenced]
    if orphans:
        item("WARN", f"孤立 prompt 模板 {len(orphans)} 个（无 render_prompt 调用/无 config 映射）：{orphans}")
        for t in orphans:
            print(f"        - prompts/{t}.md  ← 当前基线（K-孤立）：要么接进流水线，要么移入 docs/ 归档")
    else:
        item("ok", f"prompt 模板全部可达（{len(templates)} 个）")


# ---------------------------------------------------------------------------
# 2. data 资产可达性
# ---------------------------------------------------------------------------
def check_data_reachability(sources: dict[str, str]) -> None:
    print("\n== 2. data 资产可达性 ==")
    code = "\n".join(sources.values())
    json_files = sorted(DATA.glob("*.json"))
    orphan_json = [p.name for p in json_files if p.name not in code]
    if orphan_json:
        item("WARN", f"未被代码按文件名引用的 data/*.json：{orphan_json}")
    else:
        item("ok", f"data/*.json 全部被代码引用（{len(json_files)} 个）")

    # knowledge/*.json 由 studio/knowledge.py + rag.py 按目录 glob 读取（data/knowledge 目录级可达）
    kdir_ok = "data\" / \"knowledge\"" in code or "\"knowledge\"" in code or "data/knowledge" in code
    kcount = len(list((DATA / "knowledge").glob("*.json")))
    item("ok" if kdir_ok else "WARN", f"data/knowledge 目录被代码按目录扫描（{kcount} 张知识卡，目录级可达={kdir_ok}）")

    # 说明文档类资产（非 json）：同样要求有引用方，否则是死资产
    md_orphans = [p.name for p in sorted(DATA.glob("*.md"))
                  if p.name not in code and p.name not in _prompts_text()]
    if md_orphans:
        item("WARN", f"未被代码/模板引用的 data/*.md：{md_orphans}（确认是否已废弃）")
    else:
        item("ok", "data/*.md 均有引用方")


def _prompts_text() -> str:
    return "\n".join(read_text(p) for p in PROMPTS.glob("*.md"))


# ---------------------------------------------------------------------------
# 3. 知识卡与题材方向一致性（硬约束）
# ---------------------------------------------------------------------------
def check_knowledge_consistency() -> None:
    print("\n== 3. 题材方向 ↔ 知识卡一致性 ==")
    try:
        tropes = json.loads(read_text(DATA / "tropes.json"))
        direction_ids = {d.get("id") for d in tropes.get("directions", []) if d.get("id")}
    except Exception as exc:  # noqa: BLE001
        item("FAIL", f"data/tropes.json 解析失败：{exc}")
        return
    card_ids = {p.stem for p in (DATA / "knowledge").glob("*.json")}
    missing = sorted(direction_ids - card_ids)
    extra = sorted(card_ids - direction_ids)
    if missing or extra:
        item("FAIL", f"方向与知识卡不一致：缺卡 {missing} / 多余卡 {extra}（缺卡会让 draft 注入空知识）")
    else:
        item("ok", f"题材方向与知识卡一一对应（{len(direction_ids)} 个方向 / {len(card_ids)} 张卡）")


# ---------------------------------------------------------------------------
# 4. config 叶子键可达性
# ---------------------------------------------------------------------------
def check_knowledge_quality() -> None:
    """知识卡内容质量：卡会原样注入写作提示词，坏内容=直接污染成稿。

    判据唯一权威源是 `studio/kb_lint.py`（与 `python -m studio kb lint` 同一份），
    这里只做"回归守卫"：一旦有人重采/手改/扩写塞进不可用条目，立刻红。
    """
    print("\n== 3b. 知识卡内容质量（kb_lint）==")
    try:
        import kb_lint  # type: ignore
    except ImportError:
        try:
            from studio import kb_lint  # type: ignore
        except Exception as exc:  # noqa: BLE001
            item("WARN", f"无法导入 studio/kb_lint.py，跳过内容质量检查：{exc}")
            return
    rows = kb_lint.lint_all(DATA / "knowledge")
    if not rows:
        n = len(list((DATA / "knowledge").glob("*.json")))
        item("ok", f"{n} 张知识卡内容质量全部通过（无未核标记/文艺腔/非网文案例/重复条目）")
        return
    for r in rows:
        codes = sorted({i["code"] for i in r["issues"]})
        item("FAIL", f"知识卡 {r['name']} 有 {len(r['issues'])} 处不可用内容 {codes}；"
                     f"跑 `python -m studio kb lint` 看明细，`--apply` 做确定性清理")


def check_config_keys(sources: dict[str, str]) -> None:
    print("\n== 4. config 叶子键可达性 ==")
    code = "\n".join(sources.values())
    unreferenced: list[str] = []
    exempt = 0
    scanned = 0
    for cfg_name in ("config.yaml", "config.example.yaml"):
        path = ROOT / cfg_name
        if not path.exists():
            item("WARN", f"{cfg_name} 不存在，跳过")
            continue
        data = yaml_load(path)
        if data is None:
            item("WARN", f"{cfg_name} 无法用 pyyaml 解析（已降级跳过该类检查）")
            continue
        for keypath, value in iter_leaves(data):
            if not keypath:
                continue
            leaf = keypath[-1]
            scanned += 1
            # 豁免：注释键、题材指导映射（数据键，值是文件名）、extra_body 透传子树
            if leaf.startswith("_") or "genre_guides" in keypath or "extra_body" in keypath:
                exempt += 1
                continue
            if leaf not in code:
                unreferenced.append(f"{cfg_name}:{'.'.join(keypath)}={value!r}")
    if unreferenced:
        item("WARN", f"config 叶子键未被 studio/*.py 引用 {len(unreferenced)} 个（可能是死配置或访问名不一致）：")
        for u in unreferenced:
            print(f"        - {u}")
    else:
        item("ok", f"config 叶子键均被代码引用（扫描 {scanned} 个，豁免 {exempt} 个：注释/数据映射/透传）")


# ---------------------------------------------------------------------------
# 5. JSON 结构与必填字段
# ---------------------------------------------------------------------------
def check_json_shapes() -> None:
    print("\n== 5. 资产 JSON 结构完整性 ==")
    # 5.0 全量可解析（解析不了就是硬伤：运行时会静默降级）
    bad = []
    for p in sorted(DATA.glob("*.json")):
        try:
            json.loads(read_text(p))
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{p.name}({exc})")
    item("ok" if not bad else "FAIL", f"data/*.json 全部可解析（{len(list(DATA.glob('*.json')))} 个）" + (f"；坏文件 {bad}" if bad else ""))

    def expect(rel: str, top: str, fields: list[str]) -> None:
        try:
            node = json.loads(read_text(DATA / rel))[top]
        except Exception as exc:  # noqa: BLE001
            item("FAIL", f"{rel} 缺少顶层键 {top} 或解析失败：{exc}")
            return
        rows = node.values() if isinstance(node, dict) else node
        lack = []
        for row in rows:
            if not isinstance(row, dict):
                lack.append(repr(row)[:40])
                continue
            miss = [f for f in fields if not row.get(f) and row.get(f) != 0]
            if miss:
                lack.append(f"{row.get('id') or row.get('name') or '?'} 缺 {miss}")
        item("ok" if not lack else "FAIL", f"{rel} 必填字段 {fields} 齐全" + (f"；缺失：{lack}" if lack else ""))

    expect("readers.json", "readers", ["profile", "hates", "loves"])
    expect("platforms.json", "platforms", ["name", "min_words", "ideal_words"])
    expect("tropes.json", "directions", ["id", "name", "keywords"])


# ---------------------------------------------------------------------------
# 6. 代码卫生
# ---------------------------------------------------------------------------
# stage 装饰器名 → 期望承载它的函数名前缀。@runlog.staged(...) 一旦挂错函数，
# 阶段耗时统计会算到别的函数头上（曾实测：@staged("提核心梗") 挂在 recent_tropes
# 上、@staged("逐章扩写与门禁") 挂在 _stage_write_hybrid 上，真正耗时数小时的
# stage_mine / stage_write 反而不进阶段耗时表，看板与 runlog 汇总全错）。
_EXPECT_STAGE_FUNCS = {
    "抓取热点": ("stage_fetch",),
    "提核心梗": ("stage_mine",),
    "题材知识卡": ("stage_kb",),
    "书名方案": ("stage_pitch",),
    "导语": ("stage_lead",),
    "章节大纲": ("stage_outline",),
    "故事圣经": ("stage_bible",),
    "逐章扩写与门禁": ("stage_write",),
    "数据复盘": ("stage_improve",),
    "润色体检": ("stage_polish",),
}


def check_stage_decorators(sources: dict[str, str]) -> None:
    """@runlog.staged(...) 必须贴在 _EXPECT_STAGE_FUNCS 指定的函数上。"""
    print("\n== 7. stage 装饰器配对 ==")
    problems: list[str] = []
    seen: set[str] = set()
    for name, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                dotted = ast.unparse(target) if hasattr(ast, "unparse") else ""
                if not dotted.endswith("runlog.staged") and dotted != "staged":
                    continue
                label = ""
                if isinstance(deco, ast.Call) and deco.args:
                    try:
                        label = ast.literal_eval(deco.args[0])
                    except Exception:  # noqa: BLE001
                        label = ""
                seen.add(label)
                expect = _EXPECT_STAGE_FUNCS.get(label)
                if expect is None:
                    problems.append(f"{name}:{node.lineno} 未知阶段标签「{label}」")
                elif not node.name.startswith(expect):
                    problems.append(
                        f"{name}:{node.lineno} 阶段「{label}」挂错函数 {node.name}()，应为 {expect[0]}()")
    missing = sorted(set(_EXPECT_STAGE_FUNCS) - seen)
    if missing:
        problems.append(f"以下阶段标签未出现在任何装饰器上：{missing}")
    item("ok" if not problems else "FAIL",
         "stage 装饰器与函数配对正确" + (f"；命中：{problems}" if problems else ""))


def check_test_registration() -> None:
    """tests/feature_matrix.py 里定义的 @case 必须全部登记进 main() 的 order 表。

    历史缺陷（2026-10-26 实测）：order 是一张手写清单，新写的测试只要忘了加进去
    就永远不会执行，而全量回归依然报「全绿」——B07 评分卡的 3 个用例和
    review 读写联动用例就这样静默失效过。定义与登记必须机器对账。
    """
    print("\n== 8. 测试登记完整性 ==")
    path = Path(__file__).with_name("feature_matrix.py")
    if not path.is_file():
        item("WARN", "feature_matrix.py 不存在，跳过")
        return
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        item("FAIL", f"feature_matrix.py 语法错误：{exc}")
        return
    defined: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        is_case = any(
            (ast.unparse(d) if hasattr(ast, "unparse") else "") .endswith("case")
            for d in node.decorator_list
        )
        if is_case:
            defined.append(node.name)
    registered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    registered.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    registered.add(sub.attr)
    missing = [d for d in defined if d not in registered]
    extra = sorted(registered & {n for n in registered if n.startswith("test_")} - set(defined))
    problems = [f"未登记：{m}" for m in missing] + [f"登记了不存在的测试：{e}" for e in extra]
    item("ok" if not problems else "FAIL",
         f"feature_matrix 已登记 {len(defined)} 个用例" + (f"；{problems}" if problems else ""))


def check_hygiene(sources: dict[str, str]) -> None:
    print("\n== 6. 代码卫生 ==")
    bare: list[str] = []
    long_funcs: list[str] = []
    ctrl: list[str] = []
    print_count = 0
    # 控制字符检查：曾实测 studio/typeset.py 里混入一个字面 \x08（本意是正则 \b），
    # 导致"标题已带编号"判定永远为假、草稿出现「01 01 标题」双编号。
    # 这类损坏肉眼不可见，AST/编译都不报错，必须单独扫字节。
    for name, src in sources.items():
        for i, line in enumerate(src.splitlines(), 1):
            hits = [hex(ord(ch)) for ch in line if ord(ch) < 32 and ch not in "\t"]
            if hits:
                ctrl.append(f"{name}:{i} {','.join(sorted(set(hits)))}")
    for name, src in sources.items():
        print_count += src.count("print(")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                bare.append(f"{name}:{node.lineno}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno)
                length = end - node.lineno + 1
                if length > MAX_FUNC_LINES:
                    long_funcs.append(f"{name}:{node.lineno} {node.name}()={length}行")
    item("ok" if not bare else "FAIL", f"studio/*.py 无裸 except" + (f"；命中：{bare}" if bare else ""))
    item("ok" if not ctrl else "FAIL",
         "源码无控制字符（\\x08 等不可见损坏）" + (f"；命中：{ctrl}" if ctrl else ""))

    level = "ok" if print_count <= PRINT_WARN_THRESHOLD else "WARN"
    item(level, f"print( 总数 {print_count}（WARN 阈值 {PRINT_WARN_THRESHOLD}，CLI 项目允许较多）")

    long_funcs.sort(key=lambda s: -int(s.rsplit("=", 1)[-1].rstrip("行")))
    item("WARN" if long_funcs else "ok", f"超 {MAX_FUNC_LINES} 行的函数 {len(long_funcs)} 个" + (f"：{long_funcs}" if long_funcs else ""))


# 已知方法的"最少位置参数个数"。只登记 studio 内部自己定义的、签名稳定的方法；
# 名字太通用（save/load/get）的不登记，避免误报。
# 数值 = 该方法的"必填位置参数个数"（有默认值的参数不算）。
REQUIRED_ARITY = {
    "save_meta": 1,      # Project.save_meta(meta)
    "save_json": 2,      # Project.save_json(name, obj, subdir="")
    "load_json": 1,      # Project.load_json(name, subdir="")
    "set_stage": 1,      # Project.set_stage(stage, detail="done")
    "chapter_file": 1,   # Project.chapter_file(no)
}


def check_call_arity(sources: dict[str, str]) -> None:
    print()
    print("== 7. 方法调用参数个数 ==")
    bad: list[str] = []
    for name, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            need = REQUIRED_ARITY.get(node.func.attr)
            if need is None:
                continue
            have = len(node.args)
            if node.keywords and any(k.arg is None for k in node.keywords):
                continue  # *args 展开，静态数不准，跳过
            if have < need and not node.keywords:
                bad.append(f"{name}:{node.lineno} {node.func.attr}() 给了 {have} 个位置参数，至少要 {need}")
    item("ok" if not bad else "FAIL",
         f"已知方法调用参数个数正确（{len(REQUIRED_ARITY)} 个方法已登记）" + (f"；命中：{bad}" if bad else ""))

    # `project.meta` 是每次访问都重新读盘的 property，改它等于改一份临时 dict。
    # 实测放行清单从未落盘（report 里恒为 None），排查了两轮才发现。
    mut: list[str] = []
    for name, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                while isinstance(t, (ast.Subscript,)) and isinstance(t.value, ast.Attribute):
                    if t.value.attr == "meta":
                        mut.append(f"{name}:{node.lineno}")
                        break
                    break
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # project.meta.pop(...) / .update(...) / .setdefault(...) 同样改不到盘上
                if node.func.attr in ("pop", "update", "setdefault", "clear") \
                        and isinstance(node.func.value, ast.Attribute) \
                        and node.func.value.attr == "meta":
                    mut.append(f"{name}:{node.lineno}")
    item("ok" if not mut else "FAIL",
         "没有直接改 project.meta（应走 project.update_meta(...)）" + (f"；命中：{mut}" if mut else ""))


def check_undefined_names(sources: dict) -> None:
    """模块里用到但从未定义/导入的全局名字（运行时会 NameError）。

    为什么需要这一项：`compileall` 只验语法，名字解析在运行时。
    实测事故（2026-10-29）：pipeline.py 里把一行改成 `atomic_write_json(lf, ...)`，
    却忘了把它加进 `from .util import (...)` 的列表——编译通过、模块导入通过、
    verify 全绿，直到真正产出数据复盘时才 NameError 崩在流水线尾部。
    静态可达性检查必须能提前拦住这类「导入漏项」。

    判据保守（宁可漏报不误报）：
      - 先收集模块内所有「名字绑定」：def/class/import/赋值目标/参数/except as/
        with as/comprehension 目标/global/nonlocal；
      - 内置名与 dunder 视为已定义；
      - 只报 Load 上下文的 Name 且该名字从未出现在任何绑定位置。
    这一判据会漏掉「局部作用域遮蔽全局缺失」的情形，但不会误报。
    """
    import builtins

    print("\n== 9. 未定义的名字（导入漏项）==")
    problems: list[str] = []
    for name, src in sorted(sources.items()):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue  # 语法错误由编译检查负责
        defined: set[str] = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name != "*":
                        defined.add(a.asname or a.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                defined.update(node.names)
            elif isinstance(node, ast.alias):
                defined.add((node.asname or node.name).split(".")[0])
            # with ... as X / comprehension 目标在 ast 里就是 Name(Store)，已覆盖
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in defined and not node.id.startswith("__"):
                    problems.append(f"{name}:{node.lineno} 未定义 name `{node.id}`")
    # 去重：同一 (文件,名字) 只报首个位置，避免一处漏导入刷出几十行
    seen: set[tuple[str, str]] = set()
    uniq: list[str] = []
    for p in problems:
        fpart, rest = p.split(":", 1)
        ident = rest.rsplit("`", 2)[-2] if "`" in rest else rest
        key = (fpart, ident)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    item("ok" if not uniq else "FAIL",
         "源码无未定义全局名字" + (f"；命中 {len(uniq)} 处：{uniq[:8]}" if uniq else ""))


def main() -> int:
    print("短篇小说工作室 · 静态可达性检查（只报告，不改文件）")
    sources = studio_sources()
    check_prompt_reachability(sources)
    check_data_reachability(sources)
    check_knowledge_consistency()
    check_knowledge_quality()
    check_config_keys(sources)
    check_json_shapes()
    check_stage_decorators(sources)
    check_test_registration()
    check_hygiene(sources)
    check_call_arity(sources)
    check_undefined_names(sources)

    print(f"\n{_ok} 项通过 / {_fail} 项失败（另有 {_warn} 项警告）")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
