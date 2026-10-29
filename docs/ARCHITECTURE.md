# 架构与工程约束

> 源码分工、数据资产、质量治理的来龙去脉，以及每个环节不达标怎么打回。
> 改代码前请先读 [DEV_WORKFLOW.md](DEV_WORKFLOW.md)，那是操作手册；本文是背景与判据。

## 源码结构

```
studio/                  # 主体包（47 个模块，纯标准库）
├── __main__.py          #   python -m studio 入口
├── cli.py               #   CLI 全部子命令
├── pipeline.py          #   10 个阶段的编排（抓热点→…→成稿）
├── agents.py            #   写手 / 主编 / 读者团 / 情绪门 / 资料员
├── llm.py               #   OpenAI 兼容客户端 + MockLLM + 思考链识别
├── config.py            #   config.yaml 加载与校验
├── store.py             #   项目读写（原子写）
├── util.py              #   小节头判据 / 原子写 / JSON 清洗（单一权威源）
├── langqa.py            #   语言质检：AI 口癖、欧化、思考泄漏、题材化规则
├── language.py          #   语言病检测与返修：碎句链 / 语序 / 冗余 / 详略 / 对白
├── audit.py             #   投稿体检（A/B/C 分级，唯一判据来源）
├── verdict.py           #   投稿结论（字数 / 语言 / 评分 三线合一）
├── review.py            #   证据化评分卡（criteria + 覆盖率 + 校准）
├── title_gate.py        #   书名卖点门（两种战法取高分）
├── genre_cards.py       #   题材正文卡（节奏密度 / 禁止漂移）
├── knowledge.py         #   知识卡选材（相关性 × 具体度）
├── kb_lint.py           #   知识卡体检（K01–K09）与修复
├── kb_enrich.py         #   知识卡 LLM 扩写 + 质量门 + 空壳改写
├── kb.py / rag.py       #   统一检索（BM25 + 字符 bigram + 共现扩散）
├── memory.py            #   记忆库（引力 / 语义河道 / 冷热分层）
├── material.py          #   素材库与转换门
├── deconstruct.py       #   拆书
├── publish.py / typeset.py / fanqie_draft.py   # 投稿包 / 排版 / 草稿箱
├── analytics.py / improve 链路 / scheduler.py  # 数据回流 / 复盘 / 定时
├── dashboard.py         #   本地看板（127.0.0.1，纯 CSS 图表）
├── lock.py / errors.py / drafts.py / runlog.py # 并发锁 / 错误封套 / 断点保稿 / 运行日志
└── verify.py            #   python -m studio verify 一键自检

data/                    # 数据资产（换题材 = 换数据，不改代码）
├── tropes.json          #   42 个题材方向
├── knowledge/*.json     #   42 张题材知识卡（真实细节/行情/场景/台词/雷点）
├── platforms.json       #   平台规范（字数/人称/卡点/签约门槛）
├── readers.json         #   6 类读者画像
├── craft.json           #   7 类病句范例（bad → good → why）
├── genre_cards.json     #   32 张题材正文卡（对标作品统计）
├── lang_rules.json      #   语言黑名单（按题材分生效）
├── market_notes.json    #   真实榜单书名/章节名实证语料
└── character_templates.json / style_guide.md / genre_chains.json …

prompts/                 # 36 个提示词模板（<!--TASK:XXX--> 标记阶段）
tests/                   # 离线回归（冒烟 / 功能矩阵 / 静态可达性 / 契约 / 规则漂移）
tools/                   # 离线工具（成稿体检 / 语言打磨实测）
docs/                    # 开发流程规范 + 审计报告
```

## 质量治理（这几条是踩过坑才立的）

| 机制 | 模块 | 解决什么 |
|---|---|---|
| 小节头唯一判据 | `util.SEC_HEAD` | 曾出现 5 份不同正则，把正文里的「24 岁那年」当节头，切成 393 个碎片、膨胀到 9.9 万字 |
| 思考链识别 | `llm._reasoning_is_cot` / `langqa.think_leak` | 思考型模型把正文全写进 `reasoning` 字段，8 章烧掉 19 万字英文草稿 |
| 重写长度守卫 | `agents._guard_rewrite` | 门禁重写把 1100 字章节替换成 583 字片段（坍缩 47%） |
| 检测与返修同源 | `language._chain_member` | 检测说有病、返修改不动，五轮返修分数不动 |
| 体检判据上收 | `audit.audit_text` | 同一篇稿子「体检 B / 投稿 A / langqa false」三套结论 |
| 投稿三线合一 | `verdict.submission` | 字数、语言、评分各自为政，字幕达标但正文不可投 |
| 书名卖点门 | `title_gate` | 只拦「长度<10」，会把《萤火之城》判合格 |
| 字数下限取平台门槛 | `pipeline.word_gate` | 写完才发现 7944 字不够番茄签约线（1 万字） |
| 知识卡具体度排序 | `knowledge` + `kb_lint.score_item` | 「深夜喂奶时婆媳育儿理念冲突」这类空壳条目挤掉真实素材 |
| 兜异常必须留痕 | `language.polish` | 语言打磨上线以来因 `TypeError` 被静默吞掉，从未真正运行过 |

回归守卫：`python -m studio verify` 一条命令跑完编译 + 静态可达性 + 提示词契约 + 规则漂移 + 功能矩阵（`--full` 追加 MockLLM 冒烟，`--json` 机器可读）。

## 全链路门禁（每个环节不达标即打回重做）

| 环节 | 门禁 | 打回方式 |
|---|---|---|
| 导语 / 大纲 / 圣经 | 阶段达标门：确定性结构检查（字段完整/字数/禁词）+ 毒舌主编 LLM 评审 ≥8.0 分 | 带打回意见重新生成，最多 stage_gate_rounds 轮，仍不达标标 maxed_stage 放行并留运行日志 |
| 逐章写作 | 语言质检门 → 主编审查 → 读者评分团 → 情绪量化门 | 四门全过才进下一节；任何一门不过按该门意见重写本章 |
| 成稿 | 字数门：8000-12000 字（word_floor / word_ceiling 可配） | 不足→最短小节扩写；超标→最长小节压缩；最多 word_gate_rounds 轮 |
| 成稿终审 | 全稿体检（卡点/连续性）+ 全稿读者评分团 ≥7.5 | 整稿打回重写 |

每次打回都写运行日志（`workspace/logs/run_*.log` + `runs.jsonl`），看板「运行日志」页可查。

**小节编号规范**：全流程统一使用 `01`、`02`、`03`… 两位数字编号（不用"第X章"）——
写作、成稿、番茄排版稿、草稿箱推送、拆书识别均按此规范，拆书同时兼容旧"第X章"标记。

## 设计依据与方法论

本项目的规则不是拍脑袋定的，每条判据背后都有可复核的依据。以下按「判据 → 依据 → 落在哪」列出：

| 判据 | 依据 | 代码位置 |
|---|---|---|
| 第一人称 + 炸裂开头 + 每 500-800 字一个卡点 + 标题即钩子 | 下沉市场盐选类爆款的公开拆解共识（多篇同构，非单一来源） | `studio/audit.py`、`studio/title_gate.py` |
| 开头三章定生死（黄金三章） | 平台签约/推荐机制的普遍实践 | `prompts/market_guide.md` |
| 分层质量门（语言 / 句式 / 情绪拉扯分道把关） | 多 agent 创作流水线公开实践中的分工设计 | `studio/langqa.py`、`prompts/review.md` |
| AI 味词表与句式模板禁用 | 社区多轮实测沉淀的八类高频词 | `data/lang_rules.json` |
| 毒点 / 弃书雷区清单 | 读者侧弃书原因的公开归纳 | `data/craft.json` |
| 检索用 BM25 + 字符 n-gram 而非向量嵌入 | 中文短文本上词法检索优于稠密向量的公开复现结论 | `studio/kb*.py` |
| 拆书五维（核心目标/钩子/冲突/爽虐/悬念） | 拆文方法论的公开拆解 | `studio/deconstruct.py` |
| 书名必须带卖点（4 字书名签约必死） | 签约数据与编辑公开经验 | `studio/title_gate.py` |

**说明**：以上均为**方法论层面的借鉴**，用于确定「该检测什么、阈值定在哪」；
**代码全部为本项目独立实现**，未复制任何第三方项目的源码，也未引入其依赖。

## 二次开发

**开发流程规范见 [DEV_WORKFLOW.md](DEV_WORKFLOW.md)；提交前必跑 `python -m studio verify`**（编译 + 静态可达性 + 提示词契约 + 规则漂移 + 功能矩阵，`--full` 加冒烟）。

- **加热点平台**：在 `studio/hotspots/sources.py` 写一个 `fetch_xxx(n) -> list[HotItem]`，登记进 `ADAPTERS`。
- **加题材方向**：往 `data/tropes.json` 的 `directions` 里加一条（keywords 供无 LLM 兜底匹配），然后 `kb collect 该方向`。
- **加读者画像**：往 `data/readers.json` 加一条（profile/hates/loves 写得越像真人，评分越有区分度；affinity 填题材 id）。
- **调门禁松紧**：`config.yaml` 的 `review` 段（阈值/轮次/画像数），或 CLI `--threshold/--rounds/--readers`。
- **调文风/卡点标准**：改 `data/style_guide.md` 与 `platforms.json`，同时记得改主编的审查标准 `prompts/review.md`，让审查与文风同一套尺子。
- **改提示词**：`prompts/*.md` 是独立模板（`<!--TASK:XXX-->` 标记阶段），直接改文件即可；改完跑 `tests/check_prompt_contract.py` 验占位符对齐。
- **无 LLM 的纯流水线**：`match_direction()` 用关键词兜底；`--provider mock` 可全链路联调（mock 主编首轮必打回、读者首轮必压分，专测循环）。

### 工程约束（本轮新增，改代码前先看）

| 约束 | 位置 | 说明 |
|---|---|---|
| 所有落盘必须原子写 | `studio/util.py: atomic_write_text/atomic_write_json` | 临时文件 + fsync + `os.replace`；强杀/Ctrl-C 不会留半截 JSON 或半截章节 |
| 项目写入必须持锁 | `studio/lock.py: project_lock` | `project.lock` 带 pid/心跳/阶段，死进程自动回收；`run`/`continue` 已接线，看板/调度并发时抛 `PROJECT_BUSY` |
| 失败必须带错误码 | `studio/errors.py: StudioError` | `[CODE] 消息 (request_id=…)` + 位置/实际/期望/改法四元组；CLI 顶层统一兜底 |
| 小节头只有一个判据 | `studio/util.py: SEC_HEAD / is_sec_head` | pipeline / typeset / 测试共用；禁止再写第二份正则 |
| 思考泄漏只有一个判据 | `studio/langqa.py: think_leak` | 写稿/hybrid/润色共用；reasoning 字段回退前也用它判定（`llm._reasoning_is_cot`） |
| 门禁语义要诚实 | `docs/dev/GATE_POLICY.md` | `pass` / `maxed_*` / `skipped_hybrid` / `degraded` 不得混用；"没审"不能记成"通过" |
| 规则只能有一个 owner | `tests/check_rule_drift.py` | 词表/阈值/字数在多处重复时报警；回潮（数字变大）直接 FAIL |
