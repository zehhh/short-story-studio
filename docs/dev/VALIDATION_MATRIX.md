# 变更验证矩阵（VALIDATION_MATRIX）

> 改动类型 → 必跑检查。默认全跑永远安全；本表解决"只跑一条命令太快、全跑嫌慢"时的取舍。
> `✅` = 必跑且必须绿；`⚠️` = 建议跑（能提前发现边界问题）。
> 命令含义见 `docs/dev/PRE_COMMIT.md`；判不了就说判不了，见 `docs/dev/GATE_POLICY.md`。

## 总表

| 改动类型 | compileall | static_check | check_prompt_contract | check_rule_drift | smoke_test | 其它 |
|---|---|---|---|---|---|---|
| 改 `prompts/*.md` | ✅ | ⚠️ | ✅ | ✅ | ✅ | 附新增/删除字数 |
| 改 `data/lang_rules.json` | ✅ | ⚠️ | ⚠️ | ✅ | ✅ | 每条新规则必须标 `severity` |
| 改 `studio/pipeline.py` / 流水线阶段 | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | 全套 5 条 + 至少一次真实小样跑通 |
| 改 `config.yaml` / `config.example.yaml` | ✅ | ✅ | — | ✅ | ✅ | 只改 schema 不动别人 key |
| 改 `data/*.json`（tropes/readers/platforms/knowledge） | ✅ | ✅ | ⚠️ | ✅ | ✅ | 结构变则整组重跑 |
| 改 `studio/dashboard.py` / `cli.py`（看板/CLI） | ✅ | ✅ | — | — | ⚠️ | 至少手点一次对应页面/命令 |
| 改门禁阈值（review.* / score_threshold / word_*） | ✅ | ⚠️ | — | ✅ | ✅ | 记录调整依据（事故/数据） |

---

## 逐类说明与易踩坑

### 1. 改 prompt（`prompts/*.md`）

**为什么**：模板是热路径，占位符与代码实参必须一一对应；prompt 里的数量线会与 `lang_rules.json` 二次漂移。

- 必跑：`check_prompt_contract.py`（正向渲染 + 反向传值）、`check_rule_drift.py`（阈值/词表副本）、`smoke_test.py`（门禁闭环真的按新 prompt 跑）。
- 易踩坑：
  - 新增 `{{FOO}}` 却没在调用点传 `FOO=` → 正向渲染直接 FAIL；靠 `try/except` 吞只会把问题推到线上。
  - 在 prompt 里写"每节 ≤3 次"这类数字 → 必然与 L2 权威源漂移，写成"见规则库 R-xx"。
  - 模板三段顺序（TASK → SYSTEM → USER）被破坏，`load_prompt` 的 `split` 会静默切错段。
  - `_CACHE` 让同一进程内旧模板不失效：改完 prompt 必须重启进程才算数（见 `DEV_WORKFLOW.md` 常见坑）。

### 2. 改 `data/lang_rules.json`

**为什么**：它是语言规则的唯一 owner（词表/正则/阈值/severity）；`langqa.scan` 按它做确定性扫描。

- 必跑：`check_rule_drift.py`（权威源装载 + 基线对账）、`smoke_test.py`（`langqa` 正反例）。
- 易踩坑：
  - 新规则必须带 `severity: high|mid|low`；`high` 才阻断，`low` 只提示（`langqa.scan` 按此过滤）。
  - `freq` 里的阈值按"每 2000 字/章"标定，扫描时按字数线性折算；prompt 里写"全篇 N 次"就是另一套口径。
  - 新 `pattern` 必须能 `re.compile`：`static_check.py` 会校验，但坏正则只会在运行时炸。
  - 删规则前先搜 prompt/style_guide 是否有同义表述，否则删了词表但 prompt 仍在要求。

### 3. 改 pipeline 阶段（`studio/pipeline.py`、`studio/agents.py`）

**为什么**：核心流程 3500+ 行，改一处影响门禁轮次、落盘字段（`report.json`）与看板读取。

- 必跑：全套 5 条；另外用 MockLLM 跑一次 `python -m studio run --no-kb`（或 `produce`）确认闭环不破。
- 易踩坑：
  - 新增 `render_prompt` 调用点忘了传 `MARKET_GUIDE`/`GENRE_GUIDE`：这两个是自动注入，但业务占位符必须自己传。
  - 报告字段增减未同步看板/`cli.py` 汇总（`report.json` 是跨模块契约）。
  - 把"打满放行（maxed_*）"写成 `pass`：状态语义见 `GATE_POLICY.md`，看板会骗人。
  - 阶段函数忘了 `@runlog.staged(...)`，运行日志里会少一段，复盘时找不到证据。

### 4. 改 config（`config.yaml` / `config.example.yaml`）

**为什么**：配置是运行参数唯一入口；死配置/访问名不一致会让改动"看起来生效了其实没有"。

- 必跑：`static_check.py`（叶子键是否被 `studio/*.py` 引用）、`check_rule_drift.py`（字数/词表读的就是 config）、`smoke_test.py`。
- 易踩坑：
  - 新 key 只加在 `config.yaml`、没加 `config.example.yaml`（或反之）→ 别人拉到就是默认值。
  - 毫秒/秒、字/词单位写错：`timeout` 是秒，`max_tokens` 是 token，`word_floor` 是中文字数。
  - 真 key 只留在 `config.yaml`（已被 `.gitignore` 覆盖），示例文件必须空串。
  - 改 `review.*` 阈值等于改门禁，必须同时跑 `smoke_test.py` 看门禁循环是否还收敛。

### 5. 改 data 资产（`tropes/readers/platforms/knowledge/genres/genre_chains`）

**为什么**：这些 JSON 被 `pipeline` / `langqa` / 看板按字段直接读，结构漂移会静默降级。

- 必跑：`static_check.py`（必填字段 + 方向↔知识卡一一对应）、`smoke_test.py`（资产完整性断言）。
- 易踩坑：
  - 新增题材方向只加 `tropes.json` 没加 `data/knowledge/<id>.json` → `static_check.py` FAIL（缺卡会注入空知识）。
  - `platforms.json` 的字数是"投稿参考"，不参与门禁；门禁看 `config.review.word_floor/ceiling`（K5）。
  - `reader.affinity` 用了不存在的 direction id：画像挑不中，读者团退化成"永远前 N 个"。
  - `genre_chains.json` 的 `families/combos` 与 `tropes.json` 方向名不一致 → 题材链提示对不上。

### 6. 改看板 / CLI（`studio/dashboard.py`、`studio/cli.py`）

**为什么**：128KB 看板 + 57KB CLI，是作者唯一界面；字段改名/状态词直出会让人误读。

- 必跑：`compileall` + `static_check.py`；手点一次相关页面/命令。
- 易踩坑：
  - 直接展示 `maxed_langqa` 这类内部状态词给作者看（应转成"语言质检按上限放行"）。
  - 读 `report.json` 用了 `.get()` 兜底就以为安全：字段缺失时应显示"未生成/跳过"，不是显示 0 分。
  - 前端内嵌 JS/HTML 的大段字符串里改错引号，`compileall` 查不出（那是运行时模板），必须手点一次。

### 7. 改门禁阈值（`review.score_threshold` / `final_score_floor` / `word_floor`…）

**为什么**：阈值直接决定打回次数与 token 成本，也决定"合格稿会不会被反复打回"。

- 必跑：`check_rule_drift.py`（字数区间与 platforms/style_guide 对账）、`smoke_test.py`（门禁循环收敛）。
- 易踩坑：
  - 只改 `config.yaml` 忘了 `studio/config.py` 的 `DEFAULT_CONFIG`（缺 key 时运行走默认值，测试环境与生产不一致）。
  - 阈值与审稿模型的实际分档不对齐（如 8.0 时 7.2 的合格稿被反复打回）→ 先看 `workspace_logs/` 的历史分数分布再定。
  - 调低阈值不写理由：下次事故无法回溯，按 `docs/dev/DECISIONS.md` 留一篇决策笔记。
