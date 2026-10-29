# 决策笔记（DECISIONS）

> 目的：把"为什么这样定"留档，防止下一个人（或下一个 agent）把已裁定的事再翻一遍、或反向改回去。
> 纪律：**非平凡改动先写提案，落地后补"结果"**；决策翻转另开一篇并互相链接；不建索引，按日期+主题命名。
> 适用位置：本文件存"已经定的原则级决策"；具体某次改动的提案可另存 `docs/dev/decisions/YYYY-MM-DD-主题.md`。

## 0. 决策笔记模板（复制即用）

```markdown
## YYYY-MM-DD 决策标题（一句话）

- **状态**：已定 / 提案中 / 已废弃（被 #xxx 取代）
- **背景（Problem）**：出了什么事 / 观察到什么数据 / 不改会怎样
- **决策（Decision）**：做了什么，一句话说清"从此以后必须…"；写清生效范围（哪些文件/阶段）
- **替代方案（Alternatives）**：考虑过但没选的 1–3 个，各自为什么不选（成本/风险/违反哪条铁律）
- **后果（Consequences）**：好的方面 / 代价 / 需要同步改哪些地方（代码、prompt、文档、守卫脚本）
- **怎么验证生效**：一条可执行的检查命令或一个可观察指标
- **回滚方式**：怎么撤回，撤回后要同步改哪里
```

写不出「怎么验证」的决策，通常说明它还不是一个可执行决策，只是愿望。

---

## 1. 单一权威源（Single Source of Truth）

- **状态**：已定（2026-10-25）
- **背景**：同一规则被复制到多处后必然漂移——「X了一下」在 `lang_rules.json` 是 3 次/2000 字、
  `review.md` 写成"全篇≥6 次"、`draft.md` 写成"每节≤3"；账本/欠条道具词表在 4 个文件各一份；
  字数区间在 `config`(8000–12000)、`platforms.json`(8000–30000)、`style_guide`(8000–20000) 三个口径。
  `tests/check_rule_drift.py` 首轮就报出 20 处漂移（K1–K9）。
- **决策**：**一个对象只能有一个 owner 文件**，其余位置只引用编号/占位符，不复制内容。

  | 层 | 唯一 owner | 其它位置怎么写 |
  |---|---|---|
  | L1 平台/结构参数（字数区间、每节字数、卡点密度、人称、格式） | `data/platforms.json` + `config.yaml` | prompt 只写 `{{PLATFORM_SPEC}}`/读 config，不写数字 |
  | L2 语言规则（词表/正则/阈值/severity/suggest） | `data/lang_rules.json` | prompt 只写"按规则库 R-xx 核对" |
  | L3 判据/举证 | `prompts/review.md` | 不再抄词表，引用 L2 的 label |
  | L4 写作技法 | `prompts/draft.md` + `data/style_guide.md` | 不写禁用词数量线（那是 L2） |
  | L5 连续性真源 | `prompts/bible.md` + 项目 `state.json` | `memory.py` 只存作者偏好，不存设定 |

  例外只有一处：禁用道具词表的唯一词表暂定 `config.yaml.kb.negative_terms`（因为它是用户可改的运行参数），
  `lang_rules.json` 那条只保留"仅提示非生意主线"的说明并注明"词表见 config"。
- **替代方案**：
  1. 每处都保留副本、靠 review 盯同步 —— 已被证伪（K9 四副本，历史上"账单/存折/债主"逐次绕过）。
  2. 全部塞进代码常量 —— 作者改不了，也不适合 `data/` 已有的资产体系。
- **后果**：
  - 新增规则先想"owner 是谁"；owner 之外只能写引用。
  - `tests/check_rule_drift.py` 成为守护者：漂移数增加即 FAIL。
  - 代价：prompt 里会多出"见规则库"的间接层，写手需要注入清单才看得见具体词。
- **怎么验证生效**：`python tests/check_rule_drift.py` → 漂移数 ≤ 基线 20 且不增加。
- **回滚方式**：不单独回滚此决策；若某项确实需要就近冗余，必须在此登记"例外的对象/文件/理由"，并在守卫脚本里加白名单。

---

## 2. 确定性优先（Deterministic First）

- **状态**：已定（2026-10-25）
- **背景**：能精确写成规则的检查（词表、频次、字数、占位符对应）交给 LLM 判，既贵又不稳定；
  反过来，把"文风好不好"写成正则，又会误伤合法写法（历史缺陷 14：规则误伤 → 打满放行）。
- **决策**：
  1. **能确定判的，先由确定性层判**（`studio/langqa.py`、字数门、`tests/check_*.py`），零 token；
  2. **不能确定判的，写"判不了"**，交给主编/读者 LLM 或人，且状态要标 `maxed_*`/`degraded-*`，不许伪装 `pass`；
  3. 检查脚本必须纯标准库、纯确定性、**不调真实 LLM API**（`config.yaml` 里是真 key）。
- **替代方案**：
  1. 全 LLM 判 —— 成本高、不可复现、无法在 CI 跑。
  2. 全正则判 —— 用正则冒充质量判定，必然越修越窄、误伤合法表达。
- **后果**：新增检查先问"这条能不能确定性写出来"；不能就进 prompt 判据，不进扫描器。
  确定性层只报事实（命中/位置/计数），**不下质量结论**。
- **怎么验证生效**：`python tests/static_check.py && python tests/check_prompt_contract.py && python tests/check_rule_drift.py`
  全程无网络、无 token、秒级返回；`python tests/smoke_test.py` 强制 `provider=mock`。
- **回滚方式**：若某条确定性规则被证明误伤严重，降级为 `severity: low`（只提示不阻断）并在此登记，而不是删掉检查。

---

## 3. 测试不许随生产配置漂移（Tests Must Not Drift With Production Config）

- **状态**：已定（2026-10-25）
- **背景**：`config.yaml` 是作者天天改的运行参数（轮次、阈值、hybrid/agent 模式）。
  测试如果直接读它，作者调一个 `max_reader_rounds` 就会让测试失败或"虚假通过"。
  `smoke_test.py` 已踩过这个坑，所以它内部强制 `mode=agent`、恢复门禁轮次、锁 `provider=mock`。
- **决策**：
  1. 测试**只依赖自己的最小 fixture/常量**，不依赖生产 config 的具体数值；
  2. 需要真实配置形状时，读 `config.example.yaml`（示例是契约），不读 `config.yaml` 的取值；
  3. 任何测试/检查脚本都不得写真实 `config.yaml`、不得发真实网络请求、不得调用真实 LLM；
  4. 生产阈值改动只允许影响生产行为，**不允许**让某个测试从绿变红（除非测试本身在验证该阈值）。
- **替代方案**：
  1. 测试直接读 `config.yaml` —— 作者一改就红，最终大家选择"改测试"而不是"改代码"。
  2. 维护一套完全独立的测试配置副本 —— 会变成第二权威源，与第 1 条决策冲突。
- **后果**：新增测试先问"我依赖了哪个生产值"；必须依赖时在测试里显式覆盖（如 `cfg["review"]["max_reader_rounds"]=2`），并写注释说明为什么。
- **怎么验证生效**：把 `config.yaml` 的 `review.max_reader_rounds`、`pipeline.mode` 改任意值后跑
  `python tests/smoke_test.py`，仍应全绿；`python tests/check_rule_drift.py` 只有在改 `word_floor/word_ceiling`
  这类**被检查对象本身**时才允许变化（且要同步基线）。
- **回滚方式**：无（这是防"测试跟着生产烂掉"的底线）。

## 4. 兜异常必须留痕（Never Swallow Without a Trace）

- **状态**：已定（2026-10-26）
- **背景**：`stage_polish` 里语言打磨是"失败不阻断"的，写法是
  `except Exception as e: print(f"[lang] 语言打磨跳过（...）")`。
  于是 `language.polish()` 里的 `len(_count_sec_heads(...))`（对 int 取 len）
  每次改写都抛 `TypeError`，被这层兜住——**语言打磨上线以来从未真正运行过**，
  成稿照样体检、照样评分、照样入库，只有人肉读稿才发现"四类语言病还在"。
- **决策**：
  1. 凡是"失败不阻断主流程"的 `except`，**必须**同时把失败写进该阶段的 report 字段
     （例：`report["lang_polish"] = {"enabled": True, "pass": False, "error": "..."}`）；
  2. 日志用 `⚠` 前缀并说明后果（"成稿可能带四类语言病"），不要只写"跳过"；
  3. 下游（看板/复盘/淘汰）不得把"没有该字段"当成"通过"，只能把 `pass: true` 当通过。
- **替代方案**：让异常直接冒泡 —— 会把整篇成稿一起丢掉（曾因一次语言打磨失败丢掉 9000 字），
  不可取。折中是"降级 + 显式留痕"。
- **后果**：新增兜异常代码时多写两行；换来的是"静默空转"这类事故不可能再藏着。
- **怎么验证生效**：`tests/test_lang_polish_behaviour.py` 用桩写手跑真链路，
  断言"模型被调用≥1 次 / 至少一节改写成功 / 缺陷分真下降"——只检查源码里有字符串的
  旧用例对本次事故完全无效。
- **回滚方式**：无。

## 5. 禁用道具表不得否决用户选题（Banned Props Yield to the Premise）

- **状态**：已定（2026-10-26）
- **背景**：`config.kb.negative_terms` 存折/存单/欠条/借条/算盘 等 15 个词，
  语义是"模型别自己去发明这些道具"。用户 `--idea` 写"我妈把一百二十万拆迁款
  全给了我弟"，存折是天然核心道具，却在大纲阶段被判"命中用户禁用道具，
  整篇重新设计主线冲突"，连挂两轮（白烧一轮大纲 + 数万 token），
  最后模型硬凑等价物把用户给的设定改歪。
- **决策**：
  1. 选题阶段先跑 `langqa.exempt_terms_from_idea(cfg, idea)`：**只放行出现在用户选题里的词**，
     其余照禁；放行清单写 `project.meta["banned_exempt"]` → `report["banned_exempt"]` → 看板列；
  2. 禁用表仍然是"模型自由发挥时的红线"，不因为一次放行而松动；
  3. 放行是本篇级（cfg 副本 + project.meta），不写回 `config.yaml`。
- **替代方案**：① 直接删掉 config 里那几个词 —— 下次模型又会到处写存折，反弹；
  ② 让用户每次手工改 config —— 体验差且容易忘。
- **后果**：选题自带禁用词时日志会打 `[props] 选题本身含禁用道具 X、Y，本篇已临时放行`。
- **怎么验证生效**：`tests/feature_matrix.py::test_idea_props_exemption`
  （放行命中词、其余仍禁、无关选题零放行、重复调用幂等）。
- **回滚方式**：删掉 `cmd_run`/`cmd_produce` 里的两处调用即可。

## 6. 机械性语言病先零 token 清零，再交模型（Deterministic Repair First）

- **状态**：已定（2026-10-26）
- **背景**：用户反馈的四类问题里，「一句话拆成好几个短句」「重复表述」「超长无对话段」
  「AA 叠词口癖」都是**规则唯一**的机械病——判定条件和改法都不需要理解情节。
  但历史上它们全交给模型整节重写：e2e5 实测 polish 两轮 4 改 4 回滚，
  converge 三轮采纳 3 节拒绝 8 节，**整稿缺陷密度反而 1.84 → 2.95**（模型合并碎句链时
  引入了等量铺陈）。同一篇稿子改用确定性返修后 chain 0.95 → 0.0、detail 0.95 → 0.36，
  体检从「C 不可投」直接变「A 可投」，零 token、零秒级。
- **决策**：
  1. `language.deterministic_repair()` 在 `stage_polish` 里**跑两遍**：
     第一遍在 polish/converge 之前（让模型只处理真要判断的欧化句/冗余/详略），
     第二遍在 balance 之后、落盘之前（后三步都是整节重写，会把机械病写回来）；
  2. 返修只做可逆的机械变换：同主语碎句链合并（≤3 句合成一句，句号换逗号）、
     超长句按次级分隔切开、超长纯叙述段按句切开、顿号罗列去重、AA 叠词改一次；
  3. 模型路线（polish/converge）保留，但 `converge` 增加**轮级回滚**：
     整轮缺陷总分没下降就整轮撤销——宁可不动，不许越修越差；
  4. 两遍的统计分别落 `report["lang_repair"]` 与 `report["lang_repair_final"]`。
- **替代方案**：只靠模型（已证伪，越修越差）；只靠确定性（欧化句/冗余/详略仍需模型判断）。
- **后果**：语言打磨时间基本不变，token 反而省下（模型不再被浪费在机械病上）。
- **怎么验证生效**：`tests/feature_matrix.py::test_language_deterministic_repair_zero_token`
  （幂等、不越节头、不碰对白内句号、四类变换各自生效）＋对历史成稿跑
  `tools/manuscript_audit.py` 看 chain/detail 是否落地。
- **回滚方式**：`review.lang_repair_enabled = false`。

## 7. 阶段门禁分层：结构门不替后面的定点门花大钱（Gate Layering）

- **状态**：已定（2026-10-26）
- **背景**：大纲阶段同时检查「结构完整」和「禁用道具」，两组问题混判硬检不合格 →
  整份大纲重做。实测 e2e6 一次 run 里大纲被重做 3 轮、烧 270s + 3 次 2600-token 调用，
  最后仍以 `maxed_stage` 放行：**花了钱没买到合规**。而「存单」这类词在写章阶段
  本来就有定点替换门，一次短调用就能改掉真正会提交的正文。
- **决策**：
  1. `_det_check` 拆成 `_det_check_split()` 返回（结构问题、禁用道具问题）；
  2. `_BAN_CARRY_STAGES = ("大纲", "圣经")`：这两阶段若**结构合格、只命中禁用道具**，
     不重做整稿，打 `runlog` 警告并把命中清单记进 `gate["ban_carry"]`，
     由 `stage_write` 的终稿复查定点处理；
  3. 结构问题仍然硬打回（那是本阶段唯一能修的地方）；导语阶段不享受此豁免
     （导语是投稿第一屏，必须当轮干净）。
- **替代方案**：降低禁用道具检查强度（会真的漏出禁词）；给大纲加一次"只改命中词"的
  小模型调用（也行，但章级门已经做了同一件事，重复实现违反单一权威源）。
- **后果**：`gate` 值为 `pass` 但带 `ban_carry` 字段，看板/复盘能看出"这稿是带着
  禁用道具词进章级的"。
- **怎么验证生效**：跑一次含禁用道具发明的 run，日志应出现
  `[gate] 大纲 结构合格，仅命中禁用道具 → 不在本阶段重做`，且大纲轮次 = 1。
- **回滚方式**：把 `_BAN_CARRY_STAGES` 置空即可。

## 8. 检测判据与修复判据必须同源（Detector/Repairer Parity）

- **状态**：已定（2026-10-26）
- **背景**：碎句链有两处实现——`_local_chain`（判分）和 `merge_chain_sentences`
  （动手合并）。合并器更严：只碰「。」结尾、跳过时间状语开头、跳过引号句；
  检测器却把「！」「……」「？」结尾的句子也算链成员。后果是一类链**永远改不掉**：
  e2e6 成稿 `chain 1.3/千字`（投稿线 0.6）卡死，`lang_repair` 五轮迭代只降 0.07。
- **决策**：抽 `_chain_member(s)` 作为唯一判据，检测与合并都调它。判据本身写清
  四种"不算链成员"的语义理由（对话／含引号／非句号结尾／时间状语开头）。
- **替代方案**：放宽合并器去吞叹号问号——会改语气（把提问并进陈述），否决。
  另一个方案是让模型改——实测引入等量新病（e2e5 节02 chain 8→4 而总分 11→21）。
- **后果**：`chain` 密度口径变窄（只计句号链），历史档案的 `chain` 会小幅下降；
  返修到达不动点后 `chain == 0` 是**构造保证**，不再是运气。
- **怎么验证生效**：`tests/feature_matrix.py::test_chain_detect_and_repair_share_rule`
  —— 每种链型「检测 > 0 且返修后 == 0 且二跑幂等」。
- **回滚方式**：把 `_chain_member` 内联回两处（不推荐，等于恢复口径分裂）。

## 9. 章级门阈值必须高于「最小可测量子」

- **状态**：已定（2026-10-26）
- **背景**：章级语言病门曾用投稿线 ×5（`chain 3.0/千字`）。而 `_local_chain` 每段
  文本只取最长一条链、计 `(n-3+1)*4` 分——一条 3 句链在千字章里就是 `4.0/千字`。
  阈值 3.0 低于量子 4.0，等于「短章永远过不了门」：e2e7 实测每章白烧 2 轮写手
  预算才 `maxed_langdefect` 放行，两次改写几乎全打在"不可达阈值"上。
- **决策**：`CHAPTER_TARGET_SCALE = 15.0`（`chain 9.0/千字`）＝「每千字最多容忍
  一条 4 句同主语短句链，5 句起回修」。章级门定位为**止血门**，不是达标门；
  投稿线由出口的 `deterministic_repair` + 整稿体检把关。
- **替代方案**：把 `_local_chain` 的分值改小（3 句链 = 2 分）——会让投稿线阈值
  全部漂移，历史标定失效，否决。
- **怎么验证生效**：`test_market_target_single_source` 断言
  `chapter_targets()["chain_per_k"] > 4.0`，并用「千字章 + 3 句链 / 5 句链」两个
  合成样本分别断言通过/拦截。
- **回滚方式**：改回 5.0（会恢复"每章必然打满"的旧行为）。

---

## 2026-10-26 决策 10：对话边界按引号深度判，不按"句内有引号"判

- **背景**：e2e7 成稿 8938 字，chain/order/euro/redundancy 全 0、detail 0.35，
  却整篇判「C 不可投」，失败项只有 `frag 0.49/千字 > 0.3`。追到唯一一条命中：
  `那是借款！我有凭证！你卷走我买房钱！你是想独吞家产！」\n\n「保安！` ——
  `_sent_list` 在 `！` 处断句，升引号落在**前一句**，中段那几句台词里一个引号都没有，
  `_is_dialog_sent` 判它"非对话"，5 句台词被当成 5 个连续短句扣 4 分。
  同一件事还有两个实现并存：`langqa.fragment_report` 命中 0，`language._local_frag`
  命中 4——两个口径，读者只信更严的那个。
- **决策**：`studio/language.py::sents_with_dialog(text) -> list[tuple[str, bool]]`
  是**对话边界的唯一判据**：按引号深度（`_DQ_OPEN = "「『“‘"` / `_DQ_CLOSE = "」』”’"`）
  逐句累计，句首或句末深度 >0 即视为对话句。`_local_frag`、`_local_chain`
  与 `_chain_member(s, in_dialog)` 全部改用它；任何新增的"句级"语言判据也必须用它，
  不得再直接调 `_is_dialog_sent(s)` 判整句。
- **替代方案**：让 `_sent_list` 不在引号内断句——会改动所有按句切分的下游
  （碎句/链/语序/标点统计），风险面太大，否决。
- **怎么验证生效**：e2e7 复评 `frag 0.49 → 0.0`、`language_fails(full) == []`、
  体检 C → A 可投；`test_chain_detect_and_repair_share_rule` 覆盖"引号被断句后
  中段台词不算碎句"。
- **回滚方式**：`_local_frag`/`_local_chain` 改回 `_is_dialog_sent`（会重新引入
  台词假阳，A 级稿被误判 C）。


---

## 2026-10-26 决策 11：扣分只能来自 high 级硬伤，两个评分口径必须同档

- **背景**：e2e8 主编按评审卡给 7.69/10（=77 分，合格可发），同一份稿子整稿终审
  只给 58 → 62，低于放行线 72。终审是"内部质检分"，但它直接驱动"定点修复→复评"
  循环，于是两轮修改全花在 low 级瑕疵上；同时用户在看板/报告上看到"未达 72"，
  会以为稿子不能用（而投稿线体检同时判它 A 可投）。同一稿子两个分数打架，
  和之前"主编手填分 vs 自己评分卡"是同一类缺陷。
- **决策**：
  1. `prompts/polish_fix.md` 增加判分纪律：**扣分只能来自 high 级硬伤**；
     low/mid 瑕疵每条最多 -1、累计不超过 -6、不许同一问题的拆成多条；
     没有 high 级问题且能读通 → **必须落 72–82**；`score < 72` 时必须至少列一条
     `severity=high` 并写明第几节哪一句。与 `prompts/review.md` 的分档锚点同源。
  2. `report["final_review"] = {score, score_best, threshold, reached, rounds, note}`，
     `note` 固定写明"内部质检分（卡点/连续性/文风/安全），非投稿线"。
     **终审分不得单独作为"能不能投"的判据**——投稿判定只看
     `report["market_lang"]` + `tools/manuscript_audit.py`。
- **替代方案**：把 `review.final_score_floor` 从 72 降到 65 —— 只是把阈值挪到模型
  实际分布上，不解决两处口径打架，否决。
- **怎么验证生效**：下一轮端到端应满足 `abs(终审分 - 主编分×10) < 10`，且
  `final_review.reached` 或 `final_review.score ≥ 68`；`prompts` 契约检查通过。
- **回滚方式**：删掉 `prompts/polish_fix.md` 的"判分纪律"段（会恢复"永远 62 分"）。

---

## 2026-10-26 决策 12：禁用道具只能定点替换，不能触发整稿重做

- **背景**：e2e9 导语连打 3 稿（42.6s），第 1、2 稿打回理由**只有一条**
  「命中用户禁用道具 欠条」，第 3 稿才是真实的「首句不够炸」。禁用道具表
  （`config.kb.negative_terms`）的语义是"别自己发明这些东西"，是**局部用词问题**，
  但 `stage_quality_gate` 把它和"情节不达标"同等对待 → 整份重写。
  更糟的是"留优"因此失效：`ok_det` 因禁用道具为 False，第 2 稿 7.2 分不能入选，
  最终留下 6.5 分那稿（日志 `保留评审最优稿（6.5 分）`）。
- **决策**：
  1. 新增 `pipeline._fix_banned_text(cfg, llm, text, what)`：一次 LLM 定点替换
     （提示词要求"除被点名道具外逐字保留，±10% 字数"），替换后**必须复扫**；
     换不掉就退回原文并留痕 `{before, after, applied, reject}`，交下游章级门兜底。
  2. `_BAN_CARRY_STAGES` 从 `("大纲","圣经")` 扩到 `("大纲","圣经","导语")`：
     纯禁用道具问题 = 结构合格，不在本阶段重做。
  3. 定点替换自带长度守卫（<80% 视为夹带重写，拒收）与异常兜底。
- **替代方案**：把禁用道具表默认置空 —— 等于放弃用户硬约束，否决。
- **怎么验证生效**：`tests/feature_matrix.py::test_banned_text_fixed_in_place_not_rewritten`
  （6 个子用例：换掉采纳 / 换不掉退回 / 夹带重写拒绝 / 异常兜底 / 干净零调用 /
  成语白名单「如意算盘」不误报）。
- **回滚方式**：把"导语"从 `_BAN_CARRY_STAGES` 移除（会恢复整稿重做），
  删除 `stage_lead` 里对 `_fix_banned_text` 的调用。

---

## 2026-10-26 决策 13：留优必须连体检结论一起回填；给判官参照系而非纪律

- **背景**：
  1. e2e11 终审循环里 `best_score` 已达 72（达标线），但回滚时只回填 `full`，
     `report` 仍是对较差版本的结论 → 报告写 `score 70 / reached false`，
     4 轮修复与"已达标"事实全部丢失。
  2. e2e8/9/10 三篇终审分（58/58/66）与评审卡折算（77/80/74）系统性差一档以上。
     在 `prompts/polish_fix.md` 里写"两个分数不该差出 1 档"**无效**——判官看不到
     评审卡分，无从比较。
- **决策**：
  1. `best_report` 与 `best_full`/`best_score` 同刻记录，回滚时三者一起回填。
  2. `prompts/polish_fix.md` 新增必填占位符 `{{RUBRIC_HINT}}`，由 `pipeline.py`
     注入"评审卡均值 X 分（百分制 X×10）、最低一章 Y 分、共 N 章"，并要求
     相差 >10 分时必须指出评审卡漏看的 high 级硬伤及具体位置。
- **替代方案**：把 `review.final_score_floor` 从 72 降到 66 —— 只是把阈值挪到模型
  的实际分布上，不解决口径分歧，且会放过真正不达标的稿子，否决。
- **怎么验证生效**：`final_review.score` 与 `review_score×10` 的差 <10；
  `final_review.reached` 与实际 `score >= threshold` 一致（不允许出现
  `score_best >= threshold` 而 `reached=false`）。
- **回滚方式**：删掉 `polish_fix.md` 的 `{{RUBRIC_HINT}}` 段与两处
  `render_prompt` 传参；回填逻辑改回只 `full = best_full`。

---

## 决策 13：碎句与长句分开计量，不合成一个 frag 分

**背景**：e2e12（9263 字，`20261026_150847_我照顾瘫痪婆婆八年…`）体检判
`frag 1.28/千字 > 0.3`（"碎句超标"），但该篇真实问题是**长句偏多**——22 句超 46 字
（预算 18.5）。`_local_frag` 把长句罚分（每超一句 ×3）并进了 `frag`。

**决策**：`_local_frag` 返回 `(score, hits, long_score)`；`defect_score` 新增
`longsen` 键单列；`MARKET_TARGET["longsen_per_k"] = 0.60`；`language_fails` 单独报
`longsen`；长句预算加 1.25 倍容忍带（`LONG_TOLERANCE`）。`defect_brief` 的切句
统一走 `sents_with_dialog()`，且换行算句子边界（否则「标题+导语首句+正文首句」
会粘成假长句，返回给模型的样例以标题开头，无法照着改）。

**为什么不能合成一个键**：两类病的**返修动作相反**——碎句要合并、长句要拆分。
合成一个 `frag` 时模型收到"连续短句没合并"的指令，方向反了，返修必然无效；
而且 `frag_per_k = 0.3` 是按"短句连排每命中 4 分"标定的，长句残余每点 3 分
折算成密度必然越线，等于"有一句擦边即不可投"。

**后果**：历史报告的 `frag` 会变小（长句分移出），跨判据版本的数字不可直接比；
`tools/manuscript_audit.py` 增加"长句/千字"一行；`dense()` 多一个键（老报告缺该键，
读侧用 `.get`）。

**验证**：`tests/feature_matrix.py::test_frag_and_longsen_are_separate_metrics`；
e2e7–e2e12 六篇 `longsen` 实测 0.0。

**回滚方式**：恢复 `_local_frag` 返回二元组并把 `long_score` 加回 `score`，
删除 `longsen` 键与 `longsen_per_k`。

---

## 决策 14：投稿判定收敿成一个模块，三处消费它

**背景**：同一件事——"这篇能不能投"——此前三处各自实现：
1. `studio/cli.py` 只看字数上下限（历史事故：99617 字的稿子 `platform_check.pass=true`
   被留在工作台）；
2. `studio/pipeline.py` 只写 `platform_check` 与 `market_lang` 两个**并列**字段，
   没有任何字段是"结论"，看板/复盘只能各读一半；
3. `tools/manuscript_audit.py` 自己又算一套 A/B/C 等级。

三处口径不同 → 看板说"通过"、体检工具说"C 不可投"、主编给 7.8 分，用户无法判断
（这正是用户反复问"到底达没达标"的直接原因）。

**决策**：新建 `studio/verdict.py`，只提供两个函数：
- `grade(fails, warns) -> str`：体检项 → A/B/C；
- `submission(platform_check, market_lang, review_score, words, audit_fails=None) -> {ready, grade, reasons, checks}`：
  **先硬后软**——字数（平台硬线）、语言（投稿线）、体检为硬；主编分低于 7.0 只降为
  B 不判死（模型的绝对分有方差，不能拿它当否决权）。

`studio/pipeline.py` 落盘 `report["submission"]`；`studio/cli.py` 把它写进
`meta.submission_grade`/`submission_ready` 并打印一行；`studio/dashboard.py`
列表页加一格徽标（悬停看原因）；`tools/manuscript_audit.py` 的等级改调
`verdict.grade()`，不再自写三档字符串。

**后果**：老报告没有 `report["submission"]`，看板那一格为空（不误报）；CLI 在缺字段
时用 `verdict.submission()` 现算一次兜底。

**验证**：`tests/feature_matrix.py::test_submission_verdict_single_source`
（A/B/C 三档、字数越界不可投、语言超线不可投、主编分只降级、分数缺失不降级；
并断言体检工具源码里不得自己写 `"A 可投" if not fails`、流水线必须落盘 `submission`）。
看板 `_render` 实测渲染出 6 个「A 可投」+ 1 个「C 不可投」徽标。

**回滚方式**：删 `report["submission"]` 与看板徽标，`tools/manuscript_audit.py`
恢复内联三档字符串。

---

## 决策 15：终稿复查"改不动"必须止损，不许空转打满轮次

**背景**：e2e13 全稿 8111 字，`deterministic_recheck` 三轮分别只收到
556 / 495 / 462 字的"修改片段"（模型只回改过的段落）。`_guard_rewrite` 正确地把
它们判为坍缩并保留原稿，但循环照样打满 3 轮，每轮约 900 token，稿子一个字没改，
最后仍记 `pass=false`——报告看起来"修了 3 轮"，实际零进展。

**决策**：
1. `deterministic_recheck` 每轮重写后比较 `text.strip() == body.strip()`，
   一致即 `report["gate"]="no_progress"` 并 break（不再伪装成修过）；
2. `WriterAgent.rewrite_full` 的提示词显式要求"必须是完整正文，只输出修改片段
   视为无效"——这是根因侧（模型只想省 token 回片段）。

**后果**：单篇省下约 2 次全稿级调用（~1800 token）；报告里的 `gate` 变成
`no_progress`，一眼能看出"这篇没改动着过"。

**验证**：`tests/feature_matrix.py::test_recheck_stops_when_rewrite_makes_no_progress`
（片段 LLM 下 `out == full`、`gate == no_progress`、LLM 调用 ≤2 次）。

**回滚方式**：删掉 no_progress 分支与提示词里的"必须完整正文"一段。

---

## 决策 16：体检判据上收为 `studio/audit.py`，投稿判定消费它

**背景**：判据曾分散在三处——`tools/manuscript_audit.py`（自成一套 A/B/C）、
`studio/pipeline.py`（并列的 `platform_check` / `market_lang`）、`studio/dashboard.py`
（各读一半）。实测后果：
- e2e8/e2e9/e2e11：体检报 B（AI 味警告），`report["langqa_full"].pass=true`，
  投稿判定 A —— 三者对同一篇稿子给出三种结论；
- e2e7：`market_lang` 因判据 bug 停在 False，体检复检 A，投稿判定又按旧字段判 C。

**决策**：
1. 判据上收为 `studio/audit.py::audit_text(text, report=None)`（纯计算，不碰文件）；
   `tools/manuscript_audit.py` 退化为"读文件 + 渲染 + 写复检"的 CLI 外壳，
   不得再出现 `add("B语言", ...)` 这类自建判据；
2. `studio/pipeline.py` 在成稿落盘后用**同一函数**扫 `submit_text`（成稿.txt），
   写 `report["audit"]={grade,fails,warns}`，并把体检硬失败项作为 `submission()`
   的硬线输入；
3. `langqa` 黑名单也进体检（mid → warn → B，high → fail → C）——此前体检完全不看
   黑名单，是"体检 A / langqa false"分裂的直接原因；
4. 去重：`A结构/字数`、`E门禁/平台检查`、`B语言/*` 已由 platform_check / market_lang
   报过，作为 `submission.reasons` 时不重复。

**后果**：`report["audit"]` 与 `tools/manuscript_audit.py` 的结论恒等（同一函数）；
`submission.grade` 与体检等级一致；老报告无该字段时 CLI/看板回落到现算。

**验证**：`tests/feature_matrix.py::test_submission_verdict_single_source`
（工具必须调 `_audit.audit_text(`、源码里不得有 `add("B语言"`、流水线必须扫
`submit_text` 并落盘 `report["audit"]`）；mock 端到端实测打印
`[audit] C 不可投 | 失败项 …` 与 `[submission] 投稿判定：C 不可投　…`。

**回滚方式**：删 `studio/audit.py`，把判据搬回 `tools/` 并把 pipeline 的
`audit_fails` 参数去掉。

---

## 决策 17：交付前再校一次节长（全稿重写会把比例拉歪）

**背景**：e2e14 的 `lang_balance` 报 `ratio_after 1.79`，而落到 `成稿.txt` 实测
**2.08**（01 节 1700 字 / 07 节 817 字），体检判 `A结构/节长极差` 失败 → C 不可投。
根因：`balance()` 跑在**全稿读者团重写之前**，读者团打回时会整稿重写（提示词要求
"不得低于原稿 90%"），把已经调好的节长比例重新拉歪。

**决策**：在装配 `final_md` / `submit_text` 之前再判一次
`section_lengths(full)` 的极差；超 `MARKET_TARGET.section_ratio_max` 才调
`balance()`（达标零消耗），结果落 `report["lang_balance_final"]`
（含 `ratio_before_all`）。判据与体检同源。

**后果**：成稿.txt 的节长比例就是体检看到的比例；极端情况下多花 1-4 次 LLM 调用。

**验证**：`tests/feature_matrix.py::test_submission_verdict_single_source` 断言
流水线含「交付前节长极差」分支与 `lang_balance_final` 留痕。

---

## 决策 18：知识卡对白条目的废料判据是「分类标签」，不是「有没有引号」

**背景**：用户要求"丰富知识库内容、质量优先"。第一步体检发现 562 条 dialogues 里
259 条（46.1%）不含引号，于是几乎要发起一次 400 条规模的 LLM 重写。

**推翻过程（真实教训）**：把不含引号的条目逐条看下来，它们全是能直接抄的台词——
`妈，这收据上连个公章都没有，你花四千块就换张纸条？`、`药可以换，婚帖不能换，名分更不能换。`
引号只是排版，不是"台词"的本质。真正不能用的只有**分类标签**：
`婆婆与儿媳关于带娃的争执。`、`节庆祭祀引发的观念分歧。`

**决策**：
1. `studio/kb_lint.py::is_vague_dialog()` 只认分类标签（`_VAGUE_LABEL` 结尾词表
   争执/冲突/矛盾/分歧/博弈/尴尬/压力/指责/抱怨/不满/角力/拉扯/隔阂/算计/较量，
   允许尾随标点），**不用语气词或引号**做判据；
2. `CODE_LABEL` 增 `K08 对白条目没有台词`，只在 `is_vague_dialog()` 命中时报；
3. `kb_enrich.accept()` 对 dialogues 用同一函数，`_weak_items()` 也用它决定是否重写；
4. 引号形态覆盖 `「」『』“”‘’`：`_DIALOG_LINE` 同时认四种，避免 `‘…’` 被漏检；
5. `_unwrap()` 不对 dialogues 脱壳——旧实现 `strip("「」\"'“”")` 会把
   `婆婆说：「费那电干啥。」` 的收尾引号削掉，导致好条目被自己的质量门拒收。

**后果**：`kb lint` 全库从 404 处假阳性降到 0 处真问题；省下约 400 次无意义改写。

**验证**：`tests/feature_matrix.py::test_kb_vague_dialog_vs_real_line`
（3 条标签必须被抓、4 条真实台词/格言必须放过、4 种引号形态必须识别、
概述必须被质量门拒、真实台词必须被收）。

---

## 决策 19：对白占比只认字面占比，章级门下限 0.28

**背景**：同一篇稿子的「对话占比」曾有三个数——`audit` 数段（0.53）、`langqa` 数含冒号的段
（0.55）、`language` 数字面（0.22）。前两个把「他说：……」的转述也算成对话，于是
「场面被写成说明文」的稿子在库里显示 0.55，谁都没发现。市场真实区间是 0.30–0.40，
而项目 60 篇实测只有 0.19–0.32。

**决策**：
1. 唯一实现 `studio/language.py::dialog_ratio()`（引号内字数 ÷ 正文字数），
   `langqa._dialog_ratio` 与 `audit` 都委托它；
2. 章级门 `DIALOG_MIN_CHAPTER = 0.28`（高于投稿线 0.25 留 3pt 余量）；
   标定依据：门设在 0.30 时实测 24.3 → 25.5 → 28.6 后仍差一口气，两轮白烧；
3. 改写带**单调性守卫**（改写后不提升 → 回滚、不再重试）——实测有 23.4% 被改成 18.1% 的
   事故，模型可能把叙述改成更长的叙述；
4. `prompts/draft.md` 的「对话占比≥50%」换成 30–40% 的可执行口径 + 可数口径
   （每节 ≥10 处带引号对答、引号内合计 ≥150 字）——50% 从未被达到过，等于没写。

**后果**：看板/体检/门禁对同一篇稿子给出同一个数；对白门最多烧 2 轮写手（可配
`review.dialog_max_rounds`）。

**验证**：`tests/feature_matrix.py::test_dialog_ratio_single_source`
（三处必须相等、纯对白 >0.95、章级门存在且用批发判据、提示词不得再出现不可达的 50%）。

---

## 决策 20：知识库「废料」判据只认分类标签，不认引号

**背景**：`kb_lint` 曾用「有没有引号」判 `dialogues` 里的废料，结果把 404 条真实台词
（「孩子姓氏」清单）判成废料——**判错了对象**：错的是分类标签（把清单塞进 dialogues），
不是内容。

**决策**：
1. 把 `"我问过镇上的大夫，他说..."` 这类可抄进正文的句子一律视为「真台词」，不管有没有引号；
2. 只对**冒号式标引 + 抽象宾语/属性宾语**（`他说："教育问题"`）判废料，且要求宾语不是引号内容；
3. 标签明显错位的（清单/情境描述塞进 `dialogues`）由**改写**修，而不是删除。

**后果**：`dialogues` 字段的「无引号占比 46%」不再是缺陷指标——那 46% 里大部分是真台词
的叙述记录；真正需要补引号的是「某某关于某事的争执」这类概述。

**验证**：`tests/feature_matrix.py::test_kb_vague_dialog_vs_real_line`（真实台词不得报 K08、
空壳概述必须报、清单式条目按 K05 处理）。

## 决策 21：交付前补对白与「对话不足三连」合并成一个门

**背景**：2026-10-26 古言实测（`20261026_200344_我被打入冷宫那晚皇后…`，8922 字）
同时踩三条线：`C节奏/对话段占比 0.233 < 0.25`、`C节奏/超长无对话段 1 处`、
`B语言/详略/千字 0.86 > 0.6`。章级对白门逐章放行（每章都在 0.28 上下），
但终审整体重写把对白改回概述，交付时全篇掉到线下。

**决策**：新增 `language.dialog_raise()`，交付前统一处理：
① 按节对白占比升序选节；② 把最紧张的一段改写成 4–6 句来回的当场对峙；
③ 守卫：字数 0.88–1.18×、节内对白必须 +0.02 以上、无对话长段不得变多、
人名不得丢、全篇占比必须上升、补完不得跌破字数下限（跌破则整篇放弃）。
配置 `review.lang_dialog_enabled`（默认 true）/`review.lang_dialog_max_edits`（默认 3）。

**为什么合并而不是各修各的**：三条失败项是同一个动作的三个观测面——补对白同时
提高占比、切掉超长无对话段、降低详略密度。分开修会互相拆台（补完对白又要压字数，
压字数又把对白压没了）。

**替代方案否决**：只调阈值放行。否决理由：对白占比是读者可感知的市场标准
（短篇平台 0.30–0.40），放行等于把"能过体检"变成"能过体检但不好看"。

**验证**：`tests/feature_matrix.py::test_language_dialog_raise_adds_dialogue`
（补对白生效 / 坍缩拒收 / 跌破字数下限放弃 / 达标零调用）。

## 决策 22：题材正文提示卡做成「每章短约束」，不进整份题材指导

**背景**：本地开源项目 `oh-story-research` 的 `genre-prose-cards/` 有 32 张同题材长篇抽样卡
（每张含开场抓手/冲突发动机/爽点/对话声线/章尾钩子/场景颗粒/前中后期打法/节奏密度/禁止漂移
+ 1626 本样本的段落中位字数与对话占比）。全量注入会把每章提示撑到几千字，挤掉知识卡预算。

**决策**：抽成 `data/genre_cards.json` + `studio/genre_cards.py`，`_genre_note()` 只拼
「节奏密度 / 禁止漂移 / 样本统计」三行（<150 字），整份题材指导仍由 `prompts/genre_*.md` 负责。

**替代方案**：全量注入（否：每章多花 2000+ 字预算且与 genre_*.md 重复）；只写进 system 提示一次（否：
每章写作时模型看不到，约束失焦）。

**后果**：`_genre_note` 从「（按方向常规写法）」变成带具体节奏回合的短约束；题材卡缺失时静默降级。
**验证**：`test_genre_cards_note_and_ancient_guide`（宫斗方向含「规矩回合」「段落中位」；现代方向不被塞古代卡）。

## 决策 23：古装方向不得注入现代买股文题材模板

**背景**：`config.knowledge.genre_guides` 把「女主后宫」「买股文」都映射到 `genre_hougong.md`，
而那份指导写的是现代买股文（贵族学院/娱乐圈/选秀综艺赛道）。用户点名「女主后宫向古言」时，
写手同时拿到宫廷知识卡和校园赛道模板，题材自我矛盾。

**决策**：`load_genre_guide()` 里，若 `langqa.genre_tags(direction) == ("guyan",)` 且命中
`genre_hougong.md`，改注入 `genre_guyan.md`，并在末尾追加「古言后宫补充」三条硬约束
（结局收敛 1v1 或开放、情感线必须落在宫规上、禁现代买股术语）。

**替代方案**：新写一份 `genre_guyan_hougong.md`（更彻底但要维护第三份题材文件，且与 genre_guyan
大量重复）；改 config 映射（否：配置里「后宫」键对现代买股文仍然有效，改配置会伤到那一支）。

**后果**：古装后宫方向拿到 2693 字的宫廷指导而非 6318 字的买股文模板。
**验证**：`test_genre_cards_note_and_ancient_guide`（含 GENRE_GUYAN + 古言后宫补充、不含买股文总述；
现代「买股文」方向仍是买股文模板）。

## 决策 24：同一套 AI 味词表按题材分生效，不靠删规则消警告

**背景**：古言 #5 初评 B，唯一警告是「些许」（命中句「银霜炭比上月轻了些许」）。这条规则在现代稿
里是对的（「她心里些许不安」是典型 AI 腔），在古装稿里是错的（「轻了些许」是正经书面语）。
两种结论都成立，冲突点不在词，在题材。

**决策**：`lang_rules.json` 的规则支持 `not_genre: ["guyan"]`（反向题材条件），`langqa.scan()` 里
与 `genre` 同处过滤；「些许」从原规则拆出单列，标 `not_genre: ["guyan"]`。

**替代方案**：直接删「些许」（否：都市稿会漏掉最高频的套词之一）；全题材放行（否：等于放弃这条
规则）；按题材维护两份完整词表（否：两份必然漂移，本项目已吃够「同一判据多份实现」的苦）。

**后果**：规则数 56 → 57，判据仍一份，只多一个题材条件。新测试
`test_lang_rule_not_genre_scoping` 锁死「现代命中 / 古装不命中 / 通用规则两侧都生效」。
**验证**：古言 #4/#5 重扫均为 A 可投零警告；`python -m studio verify` 全绿（7 通过 / 1 跳过）。

## 决策 25：书名单独成门，且卖点分必须能被复盘看见

**背景（2026-10-26，番茄实测与新规）**
番茄 2026-10-12 新规把 Lv.0/Lv.1 的签约压到「完本签约」（15 万 / 30 万字），
同时公开的签约被拒归因里，「书名没卖点」排在「正文差」前面：4–9 字短名、
纯名词堆叠、文艺空词（时光/岁月/余生/萤火）在书城信息流里根本没有点击率。
项目原来的 `_title_ok()` 只挡三件事——长度 <10、含引号、问号/省略号结尾——
等于「只要不是明显违规就放行」。e2e 实测里它会把「萤火之城」判为合格。

**决策**
1. 新建 `studio/title_gate.py`，把书名判据从「格式合规」升级为「**卖点分**」：
   两种战法取高分——`score_relation()`（身份/关系 + 冲突动作 + 反转钩，15–30 字，
   世情短篇主流）与 `score_concept()`（高概念 + 数量等级 + 关系事件，6–30 字，
   脑洞/古言/系统流），`≥5` 才放行；`issues()` 输出可执行问题清单，
   `hint()` 引用 `data/market_notes.json` 里的**真实榜单书名**当作正例。
2. `pipeline._title_ok()` 退化为一行委托，`stage_pitch` 重试时把 `hint()` 拼进提示词，
   并把 `title_score` / `title_family` 随 `pitch.json`、`report.json` 落盘。
3. 投稿结论 `verdict.submission(title_score=…)` 增加一条**软线**：<5 分只降 B、
   不判死（正文还能救，但复盘必须看见这一关），并在 `checks.title` 留布尔值。
4. 看板加「书名 N」chip（≥5 绿 / <5 黄），标题上写清「签约第一道门槛」。

**替代方案与否决理由**
- 只改提示词、不加确定性门：模型会稳定吐出「文艺四字名」。e2e 里已经发生过。
- 直接把 4 字名判死刑：`萤火之城`/`娇养玫瑰` 确实是番茄女频长篇在用，但它们靠的是
  作者名望 + 长篇曝光位，本项目写 8000–12000 字短篇，不能借这个便宜。
  → 所以判据是「短篇口径」，`acceptable(min_len=4, min_score=3)` 可整体放宽转长篇用。

**后果**
- 书名不合格会多烧一次 pitch 调用（几百 token），换来的是签约第一关的口径统一。
- `data/market_notes.json` 是人工抄录的实证语料（带来源 URL 与抓取日期），
  测试 `test_title_gate_sells_the_hook` 断言它必须被读到 ≥8 条，防止变成死数据。

**验证**
- `test_title_gate_sells_the_hook`：真实榜单 9 条全过 / 5 类坏名全拦 /
  与 `pipeline._title_ok` 同源 / 进投稿结论 / 实证语料真被引用。
- mock 端到端：`stage_pitch` 输出《退婚之后，全村人求我高抬贵手》→ 卖点分 6（relation）。

**回滚**：`_title_ok` 恢复原格式校验即可，`title_gate.py` 可整体不引用。

## 决策 26：字数下限以平台签约门槛为下界

**背景**
`review.word_floor` 是全局 8000，平台 `min_words` 只用于 `platform_check` 的**事后**判定。
番茄官方小课堂明确「短篇作品字数大于 1 万字才可申请签约」，于是出现
「word_gate 判 8099 达标 → `_final_clean` 吃掉 93 字 → 7944 → platform_check 判死」
这种跑完整篇才发现不合格的浪费（e2e3 实测）。

**决策**
`word_gate()` 取 `floor = max(configured floor, load_platform(当前平台).min_words)`，
开跑时打印 `[words] 下限 8000 → 10000（平台签约门槛）`；`platform_check` 的 pass/fail
判据不变（仍用真实平台下界，不虚报达标）。

**后果**：`--words 9000` 投番茄会自动按 10000 字写，多约 10% 成本；但不再白跑一整篇。

## 决策 27：外部探测一律「并发 + 短超时 + 缓存」，绝不挡首屏

**背景**
用户反馈「看板加载模型配置界面运行有些卡顿」。定位到 `dashboard.list_models()`：
逐个端点串行 `requests.get(..., timeout=6)`，前端 `renderConfig()` 又把它和
`/api/config`、`/api/sources` 一起 `await`。于是任何一个端点离线（本地 Ollama 没起、
代理挂了）都让整块界面空白等 6 秒，两个死端点就是 12 秒——而模型下拉只是「建议值」，
表单本身根本不需要等它。

**决策**
1. 端点之间并发探测，连接超时压到 1.5s、读超时 4s；
2. 结果按「端点集合 + api_key」指纹缓存：探测成功 600s、失败 60s（失败要短，
   否则端点刚起来也一直显示离线）；
3. 接口支持 `?refresh=1` 显式绕过缓存，界面上给「刷新 / 重试」链接；
4. 首屏只 await 本地就能给的两个接口，模型列表改成渲染后异步补（失败只影响下拉提示）；
5. 保存配置后就地同步 `self.cfg` 的 `llm`/`llm_roles`——否则同一进程内刚改完端点，
   缓存指纹没变，下拉里还是旧端点的模型（整体替换 `self.cfg` 会丢掉 `workspace_abs`
   等解析后的私有键，所以只同步这两段）。

**后果**：两死端点首屏从约 12s 降到 1.6s、第二次调用 0ms；代价是「端点刚恢复」最多
晚 60 秒才出现在下拉里（点「刷新」立即生效）。同一条规则适用于以后任何打外网的看板接口。

