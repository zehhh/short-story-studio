# 门禁状态语义（GATE_POLICY）

> 一句话：**判不了就说判不了，降级不是通过。** 门禁结论必须能被机器统计、被人读懂、被事后复核。

## 1. 五态定义

`report.json`、`reviews/chNN.json`、看板、CLI 汇总统一使用下列五态（字符串枚举，禁止自由发挥）：

| 状态 | 含义 | 是否算"通过" | 落盘/可见性要求 |
|---|---|---|---|
| `pass` | 该门禁实际判过，且达标 | ✅ 是 | 必须带分数或命中数（如 `score`、`hits`、`rounds`） |
| `maxed_<gate>` | 判过了但不达标，轮次用尽 → **打满放行**，稿子继续往下走 | ❌ 不是通过，是"带缺陷交付" | 必须带 `rounds`、最后一轮 `issues` 摘要；提交说明里点名 |
| `skipped_mock` | 该门禁在 MockLLM 下**没有真正执行**（如字数门跳过循环） | ❌ 不是通过 | 必须带 `rounds: 0` 与跳过原因；不得计入达标率 |
| `degraded-local-stats` | 判方降级为本地统计（`mode: "local-stats"`），无 LLM 分 | ❌ 不是通过 | 必须写明 `mode` 与降级原因；看板显示"降级体检，无评分" |
| `fail` | 判过且不达标，且还有轮次可打回 | ❌ | 必须带可执行的打回信息（见第 3 节） |

已落地的具体命名（`gate` 字段取值，可 grep 复核）：

- `pass` — `studio/agents.py:328` 附近初始化、`pipeline.py:3473`（全稿读者团）
- `maxed_langqa` / `maxed_banprops` / `maxed_thinkleak` — `studio/agents.py:389 / :362 / :328`
- `maxed_review` — `studio/agents.py:435`；`maxed_reader` — `studio/agents.py:463`
- `maxed_emotion` — `studio/agents.py:512`；`"maxed"`（情绪 JSON 自报）— `agents.py:502`
- `maxed_stage` — `studio/pipeline.py:2378`（阶段门轮次用尽）
- `maxed_words_low` / `maxed_words_high` — `studio/pipeline.py:2757 / :2760`
- `skipped_mock` — `studio/pipeline.py:2559`（MockLLM 下字数门不循环）
- `degraded-local-stats` — `studio/pipeline.py:3131 / :3169`（`mode: "local-stats"`）

> 变体提示：hybrid 单写手模式没有逐章门禁，`cli.py:1134` 会把 `hybrid` / `skipped_hybrid` 单独列出来，
> `dashboard.py:203` 明确"只有 pass 才算通过；hybrid/skipped_hybrid 是跳过逐章门禁"。
> 它属于"没做门禁"，文案必须写"未做逐章门禁"，不能混入"通过章节数"。

## 2. 三条纪律

### 2.1 判不了就说判不了

- 证据不足（输出解析失败、模型返回非 JSON、扫描器异常）**不得**默认 `pass`。
- 正确做法：能重试就重试；不能重试就落 `fail` 或显式 `skipped_*`/`degraded-*` 并写原因。
- 反例：`agents.py:129` 主编 JSON 解析失败时返回 `verdict: "pass"` 是"带风险放行"，
  必须在报告里标 `degraded`（单独字段）而不是伪装成正常通过——新增代码不要复制这个写法。

### 2.2 禁止把降级当通过

- `skipped_mock`、`degraded-local-stats`、`maxed_*` 一律**不计入达标数**；统计口径为
  「真通过的章节 / 全部章节」并把非 pass 分桶列出。
- 聚合时 **maxed 优先级高于 pass**：同一章既有 `maxed_langqa` 又有后续 `pass`，结论仍是 `maxed_langqa`
  （历史缺陷：`report.json` 的 `rep_core` 被后续结果覆盖导致漏报）。
- MockLLM 只能用于测试；真实跑一旦 `provider=mock`（`studio/llm.py:1081` 会打印降级），
  整次产出视为 `degraded`，不得作为发布依据。

### 2.3 门禁只报告，不自动改文

确定性门禁（`studio/langqa.py`）只做扫描与打回，不替作者改稿；润色/重写由对应 stage 带着问题清单执行。
检查脚本（`tests/check_*.py`、`tests/static_check.py`）同样只报告，**绝不自动改文件**。

## 3. 打回信息四元组

任何 `fail` / 打回项必须让人或模型**直接能定位并改**，四元组缺一不可：

1. **位置**：`文件:行号` 或 `第 N 节 / 第 M 段`（禁止"文中某处"）。
2. **原文**：命中的原句片段（≤80 字，保留上下文），让人确认没误伤。
3. **阈值**：触发的判据与数值（如「`XX了一下` ≤3/节，本节命中 5 次」）。
4. **改法**：一句可执行的动作（如"删掉 2 处'了一下'，把'他愣了一下'改成'他停了半步'"）。

正例：

```
[langqa] 第3节 第7段  │ 原文「他愣了一下，随即把碗放下」  │ 触发：XX了一下 ≤3/节，本节 5 次；旋即 命中 1 次
                            │ 改法：删 2 处"了一下"；"随即"改成"碗还没放稳，人已经站起来"
```

反例（不合格，写手改不中）：

```
[langqa] 第3节：AI 味过重，请优化
```

**纪律**：`suggest`（改法）不得为空；`fail` 时问题清单要**尽量列全**（命中一处即 fail，但不要只报第一处）。

## 4. 阈值归属

| 判据 | 唯一 owner | 门禁实现 |
|---|---|---|
| 语言规则（词表/正则/severity/频次） | `data/lang_rules.json` | `studio/langqa.py` |
| 禁用道具词表 | `config.yaml.kb.negative_terms` | `langqa.scan_banned` + `BAN_PROPS` 注入 |
| 字数区间 | `config.yaml.review.word_floor/word_ceiling` | `pipeline` 字数门 |
| 卡点密度/人称/平台格式 | `data/platforms.json` | `pipeline.load_platform` 注入 |
| 质量判据（六维/举证） | `prompts/review.md` | 主编 LLM 门禁 |

任何"质量结论"不得由 `langqa` 给出；`langqa` 只给确定性事实（命中/计数/位置）。
`review.md` 的 verdict 才是质量结论。新增 `high` 规则必须能在 `review.md` 找到对应判据，否则只是噪声。
