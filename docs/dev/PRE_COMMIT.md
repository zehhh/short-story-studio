# 提交前门禁清单（PRE_COMMIT）

> 适用对象：任何改动 `studio/`、`prompts/`、`data/`、`config*.yaml`、`tests/` 的提交。
> 全部命令在仓库根目录 `E:\小说\短篇` 执行；纯标准库 / 零 token；**不要**在检查里调真实 LLM API。
> 退出码约定：`0` = 无失败项（允许 WARN）；`1` = 有失败项。**WARN 不是通过**，见 `GATE_POLICY.md`。

## 一条命令的顺序（5 条，按此顺序跑）

```powershell
python -m compileall -q studio
python tests/static_check.py
python tests/check_prompt_contract.py
python tests/check_rule_drift.py
python tests/smoke_test.py
```

三条新脚本可以单独跑、单独定位；CI/脚本汇总时按上面顺序串，任一 `1` 即视为提交不通过。

**便捷入口（可选）**：`python -m studio verify --full` 会把「编译 + 模块导入 + 三个守卫 + 功能矩阵 + 冒烟」串成一条命令并打印表格。
它只是包装器，**以本页 5 条为准**；表格里的「跳过」（如未加 `--full` 时的冒烟、脚本不存在）**不等于通过**，见 `GATE_POLICY.md`。
注意：`verify` 的「功能矩阵」项跑的是 `tests/feature_matrix.py`，它不在本页 5 条之内，属于另一类回归检查。

---

## 1. `python -m compileall -q studio`

**查什么**：语法错误、缩进错、文件编码/BOM 坏掉（`-q` 只打印失败文件）。
最容易漏的是用编辑器另存后带 BOM 的文件——`ast.parse` 会直接报 `invalid non-printable character U+FEFF`。

**失败了怎么修**
- 报 `File "...", line N`：去该文件第 N 行改语法（括号/冒号/f-string 引号）。
- 报 BOM：用无 BOM 的 UTF-8 另存（本仓库脚本读取用 `utf-8-sig` 兼容，但导入运行仍建议无 BOM）。
- 别用「加 `# type: ignore` / 删报错行」蒙混，语法错误一定会在运行时炸。

## 2. `python tests/static_check.py`

**查什么**：静态可达性 6 类——prompt 模板是否被 `render_prompt`/config 映射引用、`data/*.json` 是否被引用、
`data/knowledge/*` 与 `tropes.json` 方向是否一一对应、config 叶子键是否被代码读过、资产 JSON 必填字段、
裸 `except` / `print` 存量 / 超 250 行函数。

**失败了怎么修**
- `[FAIL] 题材方向与知识卡不一致`：给缺卡的方向补 `data/knowledge/<direction_id>.json`，或删掉 `tropes.json` 里已废弃的方向。缺卡会让 `draft` 注入空知识。
- `[FAIL] 必填字段缺失`：按该节提示补齐（readers 需 `profile/hates/loves`；platforms 需 `name/min_words/ideal_words`；tropes 需 `id/name/keywords`）。
- `[FAIL] 裸 except`：改成 `except Exception as e:  # noqa: BLE001` 并至少 `print` 一句降级原因。
- `[WARN] 孤立 prompt 模板`：模板没人渲染 = 死代码。接进流水线，或移到 `docs/` 归档。当前基线是 `write_section.md` 1 个。
- `[WARN] 超 250 行函数`：不改也能过，但新增功能优先拆函数，别把长函数再拉长。

## 3. `python tests/check_prompt_contract.py`

**查什么**：prompt 双向契约——正向用占位值渲染每个模板（必须成功、结果无 `{{` 残留）；
反向比对 `render_prompt("task", KEY=...)` 的实参名与模板占位符（缺传值 FAIL，多余注入 WARN）；
模板三段结构（`<!--TASK:-->` + `===SYSTEM===` + `===USER===`）；`_safe_val` 防回退。

**失败了怎么修**
- `[FAIL] 模板占位符缺代码传值`：在调用点补 `KEY=...`；若该占位符由 `{{ctx}}` 动态传，确认键在 `pipeline._chapter_ctx` 的返回 dict 里。
- `[FAIL] 渲染失败 KeyError`：模板里写了 `{{NEW_KEY}}` 但没人传。要么删占位符，要么加传值，不要靠 try/except 吞。
- `[FAIL] 渲染后残留 {{...}}`：占位符拼写不一致（大小写/下划线），或 `_safe_val` 被删了。
- `[WARN] 多余注入名`（当前 `NO` 在 review/rewrite/emotion 三处）：模板里没有这个占位符，传了也不生效，顺手删。
- `[WARN] 孤立模板`：同第 2 条。

## 4. `python tests/check_rule_drift.py`

**查什么**：规则单一权威源守卫。以 `data/lang_rules.json` + `config.yaml` 为权威，扫描
`prompts/draft.md`、`prompts/review.md`、`data/style_guide.md` 里重复硬编码的阈值/词表/字数，与
`KNOWN_DRIFTS` 基线对账：**漂移数增加 → FAIL（防回潮）**，持平/减少 → WARN。

**失败了怎么修**
- `[FAIL] 规则漂移超过基线`：说明有人把旧数字写回来了。按提示的 `K?` 回查
  `docs/audit/2026-10-25_skill提炼.md` 第 4 节，把 prompt 里的数字改为引用权威源（如"见规则库 `{{LEXICON_BLOCK}}`"），
  或删掉重复描述。
- 修完确实少了：把 `tests/check_rule_drift.py` 顶部的 `KNOWN_DRIFTS[k]["n"]` 下调到实际值（否则下次真回潮检测不到）。
- 只增不减不算修：本次修复只准减少漂移，不准新增。

## 5. `python tests/smoke_test.py`

**查什么**：MockLLM 全流程端到端（不联网、不花 token）：资产、知识库降级、语言质检、四道门禁循环、
发布/回流/复盘/素材/看板。产物写在 `workspace/.smoke_tmp/` 并自动清理。

**失败了怎么修**
- 先看最后几行 `[ok]` 停在哪一步，再往上找第一个异常；断言失败会直接打印期望/实际。
- 常见原因：`config.yaml` 的 `pipeline.mode` 被改成 hybrid 导致逐章审查断言失效（脚本内部会强制 `agent`，改动时别绕过）；
  `workspace/.smoke_tmp` 残留导致 `rmtree` 失败——手动删掉该目录重跑。
- 该脚本会读真实 `config.yaml` 但强制 `provider=mock`；**不要**把它改成走真实 API。

---

## 提交前最后一遍

1. 核对改动清单：本目录当前**未初始化为 git 仓库**（`git status` 会报 not a git repository），
   用下面这条列出最近改过的文件，确认只包含你打算交的东西；不要提交 `workspace/`、`workspace/.smoke_tmp/`、`__pycache__/`、
   以及 `config.yaml` 里的真 key。

   ```powershell
   Get-ChildItem studio,prompts,data,tests,docs -Recurse -File |
     Sort-Object LastWriteTime -Descending | Select-Object -First 15 LastWriteTime,FullName
   ```

2. 五条命令全绿（0 退出码）；有 WARN 时在提交说明里写一句"已知 WARN，未新增"。
3. 改了规则类文件（`lang_rules.json` / `draft.md` / `review.md` / `style_guide.md`）的提交，
   必须在说明里写"新增/删除字数"（补丁置换纪律，见 `docs/DEV_WORKFLOW.md`）。
