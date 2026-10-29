# 短篇小说工作室 · 开发流程总览（一页纸）

> 面向"要改这个仓库"的人和 agent。读这一页 + 按需跳 `docs/dev/PRE_COMMIT.md`、`VALIDATION_MATRIX.md`、
> `GATE_POLICY.md`、`DECISIONS.md`。全部命令在根目录 `E:\小说\短篇` 跑，纯标准库、零 token。

## 1. 目录职责表

| 路径 | 职责 | 谁可以改该目录的唯一权威内容 | 改了必跑 |
|---|---|---|---|
| `studio/` | 生产 Python 包（流水线/门禁/看板/CLI） | 代码 | 全套 5 条 |
| `studio/pipeline.py` | 流程编排、各 stage、`_chapter_ctx`（draft 占位符来源） | 流程 | 5 条 + 真实小样 |
| `studio/langqa.py` | 确定性语言质检（读 `data/lang_rules.json`） | 规则执行器 | 契约 + 漂移 + 冒烟 |
| `studio/prompts.py` | 模板加载/渲染、`_safe_val`、MARKET/GENRE 自动注入 | 渲染机制 | 契约 |
| `prompts/*.md` | 36 个 `<!--TASK:XXX-->` 模板（SYSTEM + USER） | prompt 文案 | 契约 + 漂移 + 冒烟 |
| `data/lang_rules.json` | **L2 语言规则唯一 owner**（词表/正则/阈值/severity） | 规则 | 漂移 + 冒烟 |
| `data/platforms.json` | **L1 平台参数**（字数区间/卡点密度/人称/格式） | 平台 | 静态 + 漂移 |
| `data/{tropes,readers,genres,genre_chains,character_templates,material_tags}.json` | 资产库 | 数据 | 静态 + 冒烟 |
| `data/knowledge/<direction_id>.json` | 每题材方向的知识卡（与 `tropes.json` 一一对应） | 数据 | 静态 |
| `data/style_guide.md` | L4 文风技法（不写禁用词数量线） | 文风 | 漂移 |
| `config.yaml` | **运行参数唯一入口**（真 key 只在此文件） | 作者 | 静态 + 漂移 + 冒烟 |
| `config.example.yaml` | 配置契约示例（不许有真 key） | 作者 | 静态 |
| `tests/` | 检查/冒烟；`static_check`、`check_prompt_contract`、`check_rule_drift` 是守卫 | 开发者 | 自身 5 条 |
| `docs/` | 文档；`docs/audit/` 是历史审计（**规则冲突的裁定依据**） | 文档 | 静态（链接） |
| `workspace/` | 运行产物（项目、报告、日志），不提交 | 运行时 | — |

## 2. 四种"新增"的固定 4 步

### 2.1 新增一个 stage

1. 在 `studio/pipeline.py` 写函数，函数名 `_stage_<name>`，加 `@runlog.staged("中文阶段名")`（否则运行日志断档，复盘找不到证据）。
2. 在主流程（`run`/`produce`/`continue` 的装配处）按依赖顺序调用，参数从 `cfg / llm / project / state` 取，别自己 new 全局状态。
3. 产物落盘走 `project.save_json("xxx.json", ...)` 或写进 `report.json`；**字段名是跨模块契约**，同步 `cli.py`/`dashboard.py` 读取处。
4. 跑 `python -m compileall -q studio`、`python tests/smoke_test.py`，再用 `python -m studio run --no-kb` 跑一次小样。

### 2.2 新增一个 prompt

1. 建 `prompts/<stem>.md`，**三段齐全**：`<!--TASK:大写名-->` + `===SYSTEM===` + `===USER===`（顺序不可换）。
2. 调用点写 `render_prompt("<stem>", KEY=...)`；模板里每个 `{{KEY}}` 必须在调用点有同名实参（`MARKET_GUIDE`/`GENRE_GUIDE` 例外，自动注入）。
3. 跑 `python tests/check_prompt_contract.py`：正向渲染 + 反向传值都必须是 ok；多余注入（如历史遗留的 `NO`）顺手删。
4. 在提交说明里写"新增/删除字数"；prompt 里的数量线一律改引用（见第 5 节坑 3/4）。

### 2.3 新增一个 config 旋钮

1. `studio/config.py` 的 `DEFAULT_CONFIG` 加同结构默认值（缺省时运行才不会 KeyError）。
2. `config.example.yaml` 加带注释的示例；`config.yaml` 加当前取值（真 key 只留 config.yaml）。
3. 代码里用 `cfg["段"]["键"]` 或 `cfg.get("段", {}).get("键", 默认)` 读取；**别在别处再写一份默认值**。
4. 跑 `python tests/static_check.py`：新键必须被 `studio/*.py` 引用，否则是死配置（WARN）。

### 2.4 新增一个题材方向

1. `data/tropes.json` 的 `directions` 加一项：`id`（拼音/短横线）、`name`、`keywords`（≥3 个）、`core_conflict`、`emotion`。
2. 建 `data/knowledge/<id>.json`（缺卡会让 `static_check.py` FAIL，且 `draft` 注入空知识）。
3. （可选）在 `config.knowledge.genre_guides` 加方向名 → `prompts/genre_*.md`；新题材指导文件同样要三段结构。
4. 跑 `python tests/static_check.py`（方向↔知识卡一一对应）+ `python tests/smoke_test.py`（资产断言）。

## 3. 门禁分层：确定性 → LLM

```
写稿/扩写 ──▶ ① 确定性层（零 token，先跑）
                  · 禁用道具词表   config.kb.negative_terms → langqa.scan_banned
                  · 语言规则扫描   data/lang_rules.json    → langqa.scan（severity high 阻断）
                  · 字数门         config.review.word_floor/ceiling
                  · 思考泄漏硬检   langqa.think_leak
              ──▶ ② 判据层（LLM）
                  · 阶段门（导语/大纲/圣经）  stage_gate_threshold
                  · 主编六维 + 举证          prompts/review.md
                  · 读者评分团               data/readers.json + score_threshold
                  · 情绪量化门               prompts/emotion.md
              ──▶ ③ 终审/润色（带问题清单定点改，有授权边界）
```

原则：确定性层只给事实（命中/位置/计数），**不下质量结论**；判不了就标 `maxed_*`/`degraded-*`，
语义见 `docs/dev/GATE_POLICY.md`。新增检查先问"能不能确定性写出来"，不能就进 prompt 判据。

## 4. 必须跑的检查命令

```powershell
python -m compileall -q studio          # 语法/编码
python tests/static_check.py            # 可达性/结构/卫生
python tests/check_prompt_contract.py   # 占位符 ↔ 传参双向契约
python tests/check_rule_drift.py        # 规则单一权威源（防回潮）
python tests/smoke_test.py              # MockLLM 端到端
```

退出码 `0`=无失败项（WARN 允许但不等于通过）、`1`=有失败项。细则见 `docs/dev/PRE_COMMIT.md`。

便捷入口（可选）：`python -m studio verify --full` 把上述检查串成一条命令并打表格；它只是包装器，
表格里的「跳过」不算通过（`GATE_POLICY.md`），最终仍以 5 条命令的退出码为准。

## 5. 常见坑（按踩坑频率排序）

1. **改完 prompt 不重启进程 = 没改**。`studio/prompts.py:15` 有进程级 `_CACHE`，模板启动时加载一次；
   `studio/langqa.py:24` 的规则缓存同理。改 `prompts/*.md`、`data/lang_rules.json` 后必须重开进程（`run`/`draft` 命令）。
2. **占位符必须与 `render_prompt` 传参对齐**。模板写了 `{{FOO}}` 而调用点没传 `FOO=` → 运行时才抛
   `KeyError: prompt [task] 缺少占位符取值`；`check_prompt_contract.py` 会提前拦住（含 `**ctx` 用 `_chapter_ctx` 返回键推断）。
3. **字数有唯一 owner**：`config.review.word_floor/word_ceiling`。`platforms.json` 的字数只是投稿参考，
   `style_guide`/prompt 里不要写"8000–20000"这种第二口径，更不要写"目标字数的八成"（会把下限打折到 6400）。
4. **禁用道具词表唯一 owner**：`config.yaml.kb.negative_terms`。`lang_rules.json` 那条只留说明并注明"词表见 config"；
   prompt 里引用 `{{BAN_PROPS}}`，不要手抄词表（历史上"账单/存折/债主"就是靠抄漏绕过的）。
5. **规则只在 `data/lang_rules.json` 定 severity**：`high` 才阻断、`mid/low` 只提示；review 里写"命中即打回"
   等价于 `high`，两处不一致就是 K3 漂移（`check_rule_drift.py` 会报）。
6. **`_safe_val` 不可删**：抓取文本里的 `{{xxx}}` 会被当成"未提供占位符"导致渲染崩溃，`render_prompt` 统一消毒成〔〕。
7. **`report.json` 是跨模块契约**：新增/改名字段必须同步 `cli.py`（完成摘要）、`dashboard.py`（看板）、`analytics`（回流对比），
   否则看板静默显示 0 或"—"。
8. **MockLLM 只能拿来测**：真实跑一旦 `provider=mock`（`studio/llm.py:1081` 会打印降级），整次产出标 `degraded`，不能当发布依据。
9. **测试不许随生产配置漂移**：`smoke_test.py` 内部强制 `mode=agent`、锁 `provider=mock`；新增测试照此办理（见 `DECISIONS.md` #3）。
10. **规则类改动要"补丁置换"**：因事故往热路径（`review.md`/`draft.md`/`style_guide.md`）加规则时，
    同一提交删掉或合并等量旧规则，并在说明里写新增/删除字数；删不动才显式调高预算并写清事故来源。
