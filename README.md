# 短篇小说工作室（Short Story Studio）

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/license-CC%20BY--NC--SA%204.0-lightgrey?style=flat-square)](LICENSE)
[![Commercial Use](https://img.shields.io/badge/commercial%20use-license%20required-orange?style=flat-square)](COMMERCIAL.md)
[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)

自动抓取各大平台短篇小说热点 → 提炼**核心梗** → 多 agent 协作扩写出完整的**下沉市场短篇小说**的全流程生产线。纯 Python 标准库实现，无第三方框架依赖（`requests` / `pyyaml` 之外全部内置）。

```
                     ┌──────────── 题材知识库（自动搜集）────────────┐
                     │  Bing 搜索 → 网页抓取 → LLM 蒸馏成知识卡      │
                     ▼                                              │
热点抓取 → 提梗打分 → 书名方案 → 章节大纲 → 故事圣经 ──→ 写手 agent ─┴─→ 润色体检 → 成稿
(7大平台)  (梗卡候选)  (标题即钩子) (每500字卡点) (人物/伏笔)    │              (md+txt)
                                                               ▼
                                              ┌── 毒舌主编 agent 审查 ──┐
                                              │  fail → 打回重写，循环    │
                                              ▼  pass                   │
                                              读者团 agent 评分（多画像） │
                                              低分 → 带反馈打回重写 ─────┘
                                              达标 → 定稿
```

下沉市场方向（彩礼、婆媳、扶弟魔、赘婿、重生、黄昏恋、拆迁、分家赡养……）虽然题材各异，但底层逻辑相通：
**强冲突 + 高情绪浓度 + 强代入 + 现世报闭环**。这些"大差不差"的逻辑被沉淀在四个数据资产里，
换题材方向 = 换数据，不用改代码：

| 资产 | 位置 | 作用 |
|---|---|---|
| 梗库 | `data/tropes.json` | 42 个题材方向（下沉世情 + 古言后宫 + 脑洞强设定）：核心冲突、情绪主轴、炸裂开头模板、常见反转、标题范例 |
| 素材库 | `workspace/materials.jsonl` | 自动沉淀的创作素材（标题/钩子/反转/人设/教训…），类型×题材×标签三级分类 |
| 人设模板 | `data/character_templates.json` | 实战验证的四角色人设模板（小可怜→真清醒/强势自负/纯坏女二/温柔男二）+ 16 个可叠加梗 + 关系组合 + 夸张写法 |
| 读者画像 | `data/readers.json` | 6 类下沉读者 agent 画像（含题材亲和力），组成评分团 |
| 平台规范 | `data/platforms.json` | 知乎盐选 / 番茄 / UC / 七猫的字数、人称、卡点密度、投稿要点 |
| 文风指南 | `data/style_guide.md` | 下沉文风硬约束（短句、对话过半、钱数具体、每500字卡点、章末钩子、语言红线、下狠手往俗了写） |
| 题材知识卡 | `data/knowledge/` | 自动搜集的各题材真实细节/行情/场景/雷点（`kb collect` 生成） |

---

## 快速开始

**一键启动**：双击 `启动工作室.bat`（Windows）或 `python launcher.py`，菜单式操作：
一键创作 / 蹭热点创作 / 看板 / 热点榜 / 发布投稿包 / 数据复盘 / 环境体检。

```bash
git clone <本仓库地址> && cd 短篇
pip install -r requirements.txt          # 只需 requests + pyyaml

# 1) 离线试跑（MockLLM，不需要 API Key，验证流程）
python -m studio run --idea "婆婆把我陪嫁房过户给小叔子，我用公证反击" --provider mock

# 2) 配置真实模型后正式生产（云端 API 或本地 Ollama 均可）
cp config.example.yaml config.yaml       # Windows: copy
# 填入 base_url / api_key / model，然后：
python -m studio run --idea "退婚被索88万彩礼，女主用一张十年前的借条反杀"
```

> **`config.yaml` 含 API Key，已在 `.gitignore` 中排除，不会被提交。** 仓库只提供
> `config.example.yaml` 模板；首次运行请自行复制并填写（key 留空时 `python -m studio check` 会明确提示）。

支持任何 **OpenAI 兼容接口**：DeepSeek、智谱 GLM、Kimi、通义 Qwen、本地 Ollama/LM Studio 等，
在 `config.yaml` 改三个字段即可（见 `config.example.yaml` 内注释）。

## 常用命令

| 命令 | 说明 |
|---|---|
| `python -m studio hot --source weibo,zhihu --top 20` | 只看热点榜（保存到 `workspace/hotspots/`） |
| `python -m studio mine --source zhihu,baidu,douyin` | 抓热点 + 提梗卡（打分排序，可选定） |
| `python -m studio run --idea "核心梗"` | **自拟核心梗一键成稿**（跳过热点） |
| `python -m studio run --source zhihu,baidu,douyin,toutiao --pick ask` | **蹭热点一键成稿**，交互选梗 |
| `python -m studio kb collect 彩礼纠纷` | 手动搜集某题材知识卡；`--all` 全部、`--refresh` 强制重搜 |
| `python -m studio kb list` / `kb show 彩礼纠纷` | 查看已有知识卡 |
| `python -m studio kb batch-decon --workers 2` | **批量深拆**：把 `workspace/kb/raw_text/` 里的原文逐篇深拆成叙事弧单元入库；断点续跑、失败自动补试、收尾全库去重（`--no-dedup` 跳过，`--dedup-wait 0` 免等模型卸载） |
| `python -m studio continue` | 续跑最近的项目（断点续跑，已完成的阶段自动跳过） |
| `python -m studio tropes` | 查看梗库方向 |
| `python -m studio check` | 体检：LLM/角色端点连通性 + 资产完整性 + 热点源可用性 |
| `python -m studio verify` | **项目自检**：编译 + 静态可达性 + 提示词契约 + 规则漂移 + 功能矩阵一条命令跑完；`--full` 追加 MockLLM 冒烟，`--json` 机器可读 |
| `python -m studio dashboard` | **可视化看板**：实时页面（自动刷新/项目下钻）；`--export` 导出静态 HTML |
| `python -m studio publish` | **发布成稿**：manual 投稿包 / custom 接口 / mock；`--probe` 探测接口 |
| `python -m studio analytics fetch --source mock` | **回收阅读数据**（custom 接口或 mock）；`import` 手动录入；`show` 查看趋势 |
| `python -m studio improve` | **数据复盘**：对比门禁预测与真实数据 → 改进教训自动注入下次选题 |
| `python -m studio schedule add --time 20:00 --count 2 --directions 追妻火葬场,彩礼纠纷 --words 6000` | **定时任务**：到点自动抓热点按题材批量写文；`--interval 180` 间隔触发；`list/toggle/remove/run` 管理 |
| `python -m studio scheduler` | 启动调度器常驻进程（每 60s 检查到期任务；`--once` 单轮测试） |
| `python -m studio material stats` | **素材库**：分类统计；`list` 检索；`deposit` 沉淀；`curate` AI 整理；`trash/restore` 回收站 |

常用参数：`--platform zhihu|fanqie|uc|qimao`（目标平台）、`--words 12000`（目标字数）、
`--chapters 8`（章节数）、`--pick auto|数字|ask`（选梗方式）、`--provider mock`（离线）、
`--no-review`（跳过门禁单稿直出）、`--threshold 8`（读者团阈值）、`--rounds 4`（审查上限）、
`--no-kb` / `--kb-refresh`（知识库开关/强制重搜）、`--refresh-polish`（强制重跑体检）、
`--deposit`（成稿后沉淀素材入库，**默认关**——见下）。
全局参数（`--config/--provider/--platform/--words`）放在子命令前或后均可。

> **成稿默认不自动沉淀进素材库/知识库。** 机器写的稿子回灌素材库，下一篇又会被当成
> 「爆款素材」检索回来注入提示词——自回声，越写越像自己、越写越薄。要反哺得显式开口子：
> `run --deposit`、看板「生成」页勾选「沉淀素材」，或事后 `material deposit`。

## 管线各阶段（都在防"扑街"）

1. **抓热点**（`studio/hotspots/`）：微博/知乎/百度/抖音/头条/B站/贴吧 直连适配器，
   并发抓取 + 跨平台近似去重 + 热度归一排序；微博风控时可配 cookie，或指向任意自建的
   聚合热榜服务（`dailyhot_base`，兼容常见的 `/api?type=xxx` 聚合接口返回结构）。
2. **提核心梗**（`mine`）：把热点榜单喂给 LLM，对照梗库方向产出 N 张"梗卡"
   （核心梗/情绪主轴/冲突/开头钩子/爆款潜力分），热点只是由头，转成虚构世情故事。
3. **题材知识库**（`kb` / `stage_kb`）：按所选方向自动 Bing 搜索（百度兜底）公开资料，
   抓取正文后由资料员 agent 蒸馏成"知识卡"（真实细节/钱数行情/场景/对话语气/读者雷点/安全红线），
   注入书名方案与每章扩写；断网时可降级为 LLM 内化生成（卡片会标注"未联网核实"）。
4. **书名方案**（`pitch`）：5 个"标题即钩子"候选书名（数字冲击/身份颠覆/偷听反差/对话冲突公式）、
   logline、主角/反派（含"合理的歪理"）、3 个情绪峰值、结局类型、章节数；
   言情/世情向自动注入实战人设模板与可叠加梗（真假千金/替身/假死/死亡倒计时…）。
5. **导语**（`lead`）：商业短篇标配的 300-400 字导语，六句框架——
   第一句噱头 → 第二句接情绪放大卖点 → 中段叠加矛盾反转 → 末句下饵戛然而止。
5. **章节大纲**（`outline`）：每章 3-6 个情节拍，逐拍标注卡点类型（盐选二审标准：每500字一个卡点），
   第1章炸裂开头、逐级加码的章末钩子、伏笔表（埋设章→回收章）。
6. **故事圣经**（`bible`）：人物卡（口癖/秘密/弧线）、伏笔表、时间线、场景设定、叙事口吻样本。
   伏笔表带状态机（`open/paid/dropped`），写每一章时只注入**本章该埋/该收/已逾期**的义务
   （`studio/foreshadow.py`，借鉴 openwrite B08），不再把全量伏笔平铺进提示词。
7. **四道门禁循环**（`write`，每章按序执行，最便宜的门在最前——不过关不浪费后面的 LLM 审）：
   - **语言质检门**（确定性正则/统计，**零 token 成本**）：AI 口癖黑名单（眼底闪过一丝/嘴角勾起/
     只见/就在这时/不是…而是…）、情绪直白词（很+情绪）、句式词频（XX了很久/对话标签/心理标签/
     连续同主语句/段落短语重复）——命中即带清单打回写手重写（`data/lang_rules.json` 可自行增删规则）；
   - **毒舌主编 agent**：8 项硬指标 + 零存疑六维（动机/认知/行动/对话/道具/情感）+ 毒点清单
     （圣母/降智/拖沓/流水账/金手指无代价/工具人配角）+ **平淡检测表 14 项**
     （情绪平板/节奏拖沓/对话水化/反派乏力/赌注缺失/转折生硬/行动被动…每项带解决方案），
     fail 带具体修改意见打回重写；
   - **读者评分团 agent**（按题材亲和力挑画像）真实读者打分，均分低于阈值带"想弃读的地方"打回；
   - **情绪量化门**：情绪分/章尾钩子强度（≥7）/虐爽比（3-5成）/毒点逐项打分，不达标打回重写情绪线；
     **跨章情绪衔接**——上一章偏虐则自动注入"本章必须安排爽点补偿"提示，防连续虐弃读；
   - 每道门达到轮次上限按现稿放行并标记 `maxed_*`，全过程落盘 `reviews/chXX.json` 供追溯。
8. **润色体检**（`polish`）：卡点密度/连续性/文风/内容安全四维审稿 + 全稿读者团终评
   （低于阈值整稿打回重写一轮）+ **伏笔账本体检**（`report.json` 的 `foreshadow_audit`：
   超期未收 / 回收章正文无痕迹 / 回收章早于埋设章三类出账，warn 级不阻断发布），
   输出 `final/成稿.md`、`final/成稿.txt`、`report.json`。

## 项目产物结构

```
workspace/projects/<时间>_<题名>/
├── project.json      # 元信息 + 各阶段状态 + 章节门禁摘要（断点续跑依据）
├── hotspots.json     # 聚合去重后的热点
├── concepts.json     # 梗卡候选（打分排序）
├── concept.json      # 选定梗卡
├── kb.json           # 本篇注入的题材知识卡摘要
├── pitch.json        # 书名 + 故事方案
├── outline.json      # 章节大纲 + 卡点表 + 伏笔表
├── bible.json        # 故事圣经
├── chapters/ch01.md …  # 各章正文（过门禁后的定稿）
├── reviews/ch01.json … # 每章审查轮次/评分/读者团意见
├── final/成稿.md .txt  # 成稿（txt 首行为书名，可直接投稿）
└── report.json       # 体检报告 + 全稿读者终评 + Token 用量
```

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

## 本地小模型执行方案

三大本地推理引擎都提供 OpenAI 兼容端点，本项目的 LLM 客户端可直连（`provider: local`，无需 API Key）：

| 引擎 | 端点 | 特点 |
|---|---|---|
| [Ollama](https://ollama.com) | `http://127.0.0.1:11434/v1` | 最省心：一键拉模型、自动量化/GPU 加速（推荐入门） |
| [LM Studio](https://lmstudio.ai) | `http://127.0.0.1:1234/v1` | GUI 友好，适合摸索期 |
| [llama.cpp llama-server](https://github.com/ggml-org/llama.cpp) | `http://127.0.0.1:8080/v1` | 最可控：`-c` 直接设上下文、磁盘占用最小 |

**⚠ Ollama 最大的坑**：其 OpenAI 兼容层传不了 `num_ctx`，默认上下文仅 4K，中文长稿必被截断。
解法（二选一）：设环境变量 `setx OLLAMA_CONTEXT_LENGTH 16384` 后重启 Ollama；
或用 Modelfile 写 `PARAMETER num_ctx 16384` 后 `ollama create`。

**按硬件档位的模型分工**（`python -m studio local --setup` 可随时查看）：

| 档位 | 分工 | 预期 |
|---|---|---|
| 纯CPU / 6GB显存 | qwen3:4b 全角色 | 3-10 字/秒，适合过夜批量 |
| 8GB 显存 | qwen3:8b 全角色 | 15-30 字/秒 |
| 12GB 显存 | qwen3:14b 写 + qwen3:8b 审评 | 写 8-20 字/秒 |
| 16-24GB 显存 | qwen3:14b / glm4:9b 写 + 8b 审评 | 20-50 字/秒，32K 上下文跑全稿润色 |
| **混合模式（推荐）** | 云端 API 写 + 本地 4b/8b 审评 | 质量不降，审查/评分零成本 |

**混合路由**通过 `llm_roles` 实现：`writer/reviewer/panel/planner/light` 五个角色可各自指定模型，
未配置的角色沿用主 llm。管线路径：章节扩写/终稿改写→writer，主编审查+全稿体检→reviewer，
读者团评分→panel（调用量最大，本地零成本收益最高），提梗/大纲/圣经→planner，知识卡蒸馏→light。
`--mock` 模式下路由强制失效（全链路假模型，保证联调确定性）。

其他已内建：**流式输出**（本地引擎自动开启，长文生成实时显示进度，失败自动降级）、
**JSON 修复重试**（小模型输出解析失败时带纠错提示重试一次）、
**用量统计**（按角色/模型汇总 tokens 写入 report.json 并在结束后打印，供成本核算）。

上手三步：

```bash
python -m studio local                # 自动探测本机引擎与已装模型
python -m studio local --bench qwen3:8b   # 实测中文生成速度 + JSON 指令遵循（审查/评分关键能力）
python -m studio local --snippet      # 生成混合模式 config.yaml 片段，粘贴即用
```

实测参考（某 CPU 机器 + Ollama + qwen3）：生成 2.1 字/秒但 **JSON 指令遵循 3/3**——
写正文太慢，但做审查/评分完全够格，正是混合模式的价值；生成样例文风已相当"下沉"
（"腊月里飘着细雪，老张婆子把儿媳陪嫁的金镯子往小姑子手心一塞"）。
进一步可探索社区网文微调模型（如 HuggingFace 上的 Qwen 系爽文 LoRA/GGUF）；若要做领域微调，
用任意训练框架以 2000-5000 条过稿文本自训写手模型即可，本项目不绑定具体训练方案。

## 自动发布与数据闭环

平台均无公开发布 API（番茄作家助手为网页端，接口未公开且随时可变），因此提供三档发布模式：
`manual` 生成投稿包（正文+书名简介+平台要求+逐步指引，**永远可用**）、`custom` 抓包后自配接口
（config 填 endpoint 模板与字段映射，`--probe` 自检）、`mock` 离线测试。发布记录落盘 `publish.json`。

数据回流同样三档：custom 接口抓取、`analytics import` 手动录入（后台肉眼抄数，最可靠的兜底）、mock。
快照形成时间序列，自动计算阅读增长/完读率/互动率/**最弱章节**（章节留存掉队检测）。

`studio improve` 把真实数据与四道门禁的预测分对照，生成复盘报告（`final/improvement.md`），
并把改进教训写入 `feedback_lessons.json`——**下次 `run` 选题时自动注入**，形成
"生产 → 发布 → 数据 → 改进 → 再生产"的完整闭环。

## 运行日志

每次运行（任意 CLI 命令）自动在 `workspace/logs/` 生成一份运行日志 `run_时间_命令.log`：
- **全量输出镜像**：所有模块的控制台输出（各阶段、各门禁、各 Agent 的运行情况）完整落盘；
- **结构化阶段计时**：抓热点/提梗/方案/导语/大纲/圣经/扩写/润色/复盘等每个阶段的耗时与成败；
- **LLM 统计**：每次调用的角色/模型/Token 数；
- **运行总结**：结束时写入阶段耗时表 + 错误数 + LLM 统计，并登记到 `logs/runs.jsonl` 索引；
- 看板"运行日志"页签：运行列表 + 点击查看日志全文（只读，限 logs 目录内）。
关闭：config.yaml `logging.enabled: false`。

## RAG 知识库与拆书

**统一 RAG 检索**（`studio/rag.py`）：知识卡 + 素材库 + 教训库 + 拆书结果统一索引，
**BM25 + 中文字符 bigram**（零嵌入模型、纯 CPU、离线可用，社区验证的轻量本地方案），
源文件变化自动重建。写作时按"核心梗+方向+本章目标"检索，命中素材直接注入章节提示词。

**拆书**（`studio/deconstruct.py`，按五维结构拆解：每章核心目标/开篇钩子/核心冲突/
爽点虐点/章末悬念 + 起承转合 + 节奏标记）：

```bash
# 单本拆解（自动分段：识别 01/第X章/编号标记）
python -m studio deconstruct 某书.txt --title 书名
# 批量拆书（目录下所有 txt）
python -m studio deconstruct ./拆书目录/
# 检索知识库
python -m studio rag search --q "假死 公证 倒计时"
```

产物：`workspace/decon/<书名>.json`（逐段核心目标/钩子/冲突/爽虐/技法/金句 + 全书结构模板/
技法清单/书名公式）→ 自动进素材库（打"拆书"标签）与 RAG 索引。看板"生成小说"页签可
inbox 文件一键拆解/批量拆书/查看进度，并提供**生成小说后台入口**（核心梗或热点模式、
篇数/题材/字数/平台可设）与 RAG 检索框。

## 全流程制作（一条命令从头到尾）

```bash
# 一条命令：写 2 篇（追妻/彩礼轮换）→ 自动发布 → 回收数据 → 复盘改进 → 制作报告
python -m studio produce --count 2 --directions 追妻火葬场,彩礼纠纷   --words 6000 --publish-mode mock --data-source mock --with-improve
```

各模块不再是孤岛：produce 串起"抓热点→转换门→提梗→知识卡→方案→导语→大纲→圣经→
四道门禁写作→润色→发布→数据回流→复盘"，结束后输出制作报告并写 `production_*.txt`。
看板"生成小说"页签的三个复选框（自动发布/回收数据/复盘改进）同样走这条链。
定时任务亦可设 `auto_publish/auto_data/auto_improve`，到点全自动。

## 定时批量创作

到点自动"抓热点 → 匹配题材 → 四道门禁 → 成稿"的无人值守生产线：

```bash
# 创建任务：每晚 20 点写 2 篇（追妻火葬场、彩礼纠纷轮换），单篇 6000 字
python -m studio schedule add --name 晚间档 --time 20:00 --count 2   --directions 追妻火葬场,彩礼纠纷 --words 6000
python -m studio scheduler        # 启动调度器（常驻；--once 单轮测试）
```

- 触发两种：每日 HH:MM / 每 N 分钟；多篇自动**轮换题材**；
- 指定题材时提梗阶段强制优先该方向（热点仅作由头），选题结果按方向优选；
- 执行用子进程隔离，日志落 `workspace_logs/`；运行锁防并发，心跳供看板判断存活；
- 看板"定时任务"页签：任务卡片/新建表单/启停/立即运行/调度器心跳；
- 调度器模式下 mock 任务自动 `--no-kb`（假产出不入知识库）。

## 小说平台热点与素材转换门

热点抓取除社交平台外，增加**小说平台/故事向聚合源**（`novel` 源）：七猫排行榜直连 +
知乎故事类提问过滤（经历/体验/怎么看待…）+ 贴吧热议，按梗库**题材关键词做相关度排序**——
指定题材（`--direction`）时相关条目优先。番茄网页端因字体反爬无法直连，README 如实说明。

**素材转换门**（`materials.convert_hotspots`）：热点事件**不得原样进素材库**。
入库前由转换 agent 改写为虚构世情素材（抽象冲突 + 核心梗 + 黄金开头钩子 + 题材归属），
不可转化的（体育/科技/政治等）直接丢弃：

```bash
python -m studio material from-hotspots --directions 彩礼纠纷,黄昏恋银发
# → 抓 31 条热点 → 转换 → 可用钩子入库，其余丢弃（原始事件 0 条入库）
```

成稿自动沉淀时同样经过转换门（走 light 角色模型，本地小模型即可）。

## 素材库（知识库系统）

成稿后**自动沉淀**素材入库：书名候选、导语、开头钩子、逐章章末钩子、反转设计、伏笔表、
人设卡、情绪爆点（带门禁评分）、通过章节的修改教训、关联热点——按指纹去重，重复沉淀不翻倍。

三级分类：**类型**（11 类）× **题材**（自动归属梗库方向）× **自动标签**（数字梗/偷听/倒计时/
假死/打脸/公证…关键词规则，见 `data/material_tags.json` 可扩展）。支持关键词检索。

**整理 Agent 自动整理**：沉淀后（或看板点"✨ AI 整理"）由整理 Agent 批量评估——
质量分级（高质/可用/低质）、类型与题材纠偏、补充标签、给每条一句话整理意见；
**低质素材自动进回收站**（`materials_trash.jsonl`，可恢复，不直接销毁）。
按角色路由走 light 模型（本地小模型即可，成本极低）。

反哺生成：选题（mine）阶段自动注入素材库高分范例（高质优先）；看板"素材库"页签可
筛选/搜索/手动添加/删除/回收站恢复；CLI：`material stats / list / deposit / add / remove /
curate（跑整理）/ trash（看回收站）/ restore --id（恢复）`。

## 知识系统（结构拆解 + 向量建模 + 共现矩阵联想）

四层知识系统（详细方案见 `docs/knowledge_system_plan.md`）：结构拆解定骨架，向量层管语义相近，
共现层做无字面重叠的联想扩散，检索层三路融合按弧位加权——每层解决上一层解决不了的问题：

1. **结构拆解层**：拆书自动标注叙事弧——每段输出 `arc`（起/承/转/合）+ `arc_role`（弧内功能）+
   开头片段 `lead_pattern`（数字悬念/身份反差/偷听反差/倒计时）；全书聚合为 `arc_map` 弧线图；
2. **向量层**：每条知识编码为向量（qwen3.5-embedding，MRL 512 维），结构信息编入向量文本，
   同弧位情节在向量空间聚拢；存储 `workspace/kb/`（units.jsonl + vectors.npy，零依赖 numpy 余弦）；
3. **共现关联层**：tag/弧位/情绪 共现矩阵按 **PPMI 加权**（认知科学：联想强度即 PMI），离线预计算；
   检索时扩散激活（钟型阻尼 0.5、两跳），零字面重叠的关联知识也能被激活；
4. **引力检索层**：BM25 + 向量 KNN + 共现扩散 三路 RRF 融合，按当前章节弧位定向加权，
   写作时自动注入（大纲=结构骨架对照 / 导语=模式库 / 逐章=同弧位情节+技法）。

CLI：`kb stats / recall --q "查询" --arc 转 / rebuild / migrate`；
看板：素材库页签 →「📚 知识结构」→ 联想检索测试。

## 记忆库

素材库即记忆单元池，按多层级持久化记忆的思路实现四条机制（`studio/memory.py`，零依赖）：

- **记忆引力**：写作时信息主动流向模型——逐章上下文自动注入相关记忆，不等检索指令；
- **标签语义河道**：共享标签的记忆互相增权，BM25 命中做种子后沿标签河道一跳联想扩散
  （检索结果带「联想」标记），命中即强化（`usage_count++`）；
- **冷热分层**：L1 工作记忆（高质且高频复用）/ L2 短期 / L3 长期 / L4 冷归档，
  选题阶段注入「引力摘要」（热记忆上浮）；素材 `digest` 排序也计入热度；
- **冷置衰减**：30 天未用热度 ×0.85；长期零复用的低质素材建议沉降入回收站。

入口：CLI `memory layers / recall --q / consolidate [--dry-run]`；
看板"素材库"页签 →「🧠 记忆库」按钮查看四层分布与整理沉降。

## 番茄草稿箱自动发布与自动排版

自动排版引擎 `studio/typeset.py`：小节标题统一「01 标题」两位数字编号（全流程不用"第X章"）、无标记成稿按 ≤2000 字自动
切章、段间空行/段首去缩进、半角标点转全角、省略号破折号规范化、markdown 痕迹清理、
超长段落与敏感表述自检。投稿包自动附带 `submission/03_番茄排版稿.txt`。

草稿箱适配器 `studio/fanqie_draft.py`（Playwright 浏览器自动化，走官方 UI 流程）：
首次 `python -m studio.fanqie_draft login` 人工登录保存态，之后
`studio publish --mode fanqie_draft` 自动建作品、逐章粘贴、**只存草稿不提交审核**
（不可逆动作留给人），章节间随机 3–7 秒频控。方案详见 `docs/fanqie_publish.md`。

## 控制台（看板即工作台）

看板顶部四个页签：**总览 / 文稿编辑 / Agent 管理 / 模型与配置**。

- **模型与配置**：手动切换模型（本地 Ollama 下拉或任意 OpenAI 兼容 API：填 base_url + model +
  api_key 即完成接入）、按 Agent 角色分配不同模型（策划/写手/审查/读者团/轻量五路 llm_roles）、
  增删热点抓取来源（七大平台 + 自建聚合代理 + mock/custom 勾选即存）、高级 JSON 增量改配置；
- **Agent 管理**：14 个 Agent 的门禁开关（语言质检/主编/读者团/情绪门/知识库随手启停），
  点击即可在线编辑任意 Agent 的提示词（保存自动备份 .bak），也可新建自定义提示词模板；
- **文稿编辑**：选择项目 → 打开成稿或任意章节，在线修改，保存自动备份；支持**删除项目**（二次确认，连带数据快照清理）；
- **创作页一键全流程**：⚡ 按钮填空即跑（内容可全留空用默认），自动串起写→发→数据→复盘；
- **模型与配置**：🔌 连通测试按钮——不保存即可测试任意 base_url/model/api_key 组合的连通性与生成速度；
- 所有写操作仅写本机 `config.yaml` / `prompts/` / 项目目录，服务只绑定 127.0.0.1。

## 可视化看板

```bash
python -m studio dashboard            # http://127.0.0.1:8600 （端口占用自动后移）
python -m studio dashboard --export   # 导出自包含静态 HTML（数据内嵌，离线可看，可发群里）
```

看板内容（纯标准库实现，图表为纯 CSS，无外部 JS 依赖，离线可用）：

- **总览 KPI**：项目数/成稿字数/平均读者分/门禁通过率/知识卡数/Token 用量；
- **项目流水线**：每个项目的六段进度（梗卡→方案→大纲→圣经→章节→成稿）、字数、主编分、
  读者分、门禁通过 x/y、平台字数达标；点击项目名下钻到详情（每章的审查轮次/评分、
  读者画像打分明细、候选书名、体检报告、本项目用量）；
- **题材与知识库**：题材分布饼图 + 42 方向知识卡覆盖清单；
- **热点榜**：平台分布 + 热度 Top 条形图（读最近一次 hot 快照）；
- **Token 用量**：按角色(模型)汇总调用次数与 tokens，核算成本。

## 二次开发

**开发流程规范见 [`docs/DEV_WORKFLOW.md`](docs/DEV_WORKFLOW.md)；提交前必跑 `python -m studio verify`**（编译 + 静态可达性 + 提示词契约 + 规则漂移 + 功能矩阵，`--full` 加冒烟）。

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

## 合规与风险提示

- 热点仅用作虚构故事的"由头"，成稿禁止影射真实人物/机构；文风指南已内置内容安全约束，体检阶段复查。
- 抓取仅限各平台公开榜单，频率克制（默认每平台≤30条）；请遵守目标平台协议，商用分发前自行评估。
- AI 成稿需人工通读校订后再投稿；知乎盐选要求全稿 ≥8000 字且过稿率低，`report.json` 会提示字数达标情况。

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
| 开头三章定生死（黄金三章） | 平台签约/推荐机制的普遍实践 | `studio/diagnose.py` |
| 分层质量门（语言 / 句式 / 情绪拉扯分道把关） | 多 agent 创作流水线公开实践中的分工设计 | `studio/langqa.py`、`prompts/review.md` |
| AI 味词表与句式模板禁用 | 社区多轮实测沉淀的八类高频词 | `data/lang_rules.json` |
| 毒点 / 弃书雷区清单 | 读者侧弃书原因的公开归纳 | `data/craft.json` |
| 检索用 BM25 + 字符 n-gram 而非向量嵌入 | 中文短文本上词法检索优于稠密向量的公开复现结论 | `studio/kb*.py` |
| 拆书五维（核心目标/钩子/冲突/爽虐/悬念） | 拆文方法论的公开拆解 | `studio/dissect.py` |
| 书名必须带卖点（4 字书名签约必死） | 签约数据与编辑公开经验 | `studio/title_gate.py` |

**说明**：以上均为**方法论层面的借鉴**，用于确定「该检测什么、阈值定在哪」；
**代码全部为本项目独立实现**，未复制任何第三方项目的源码，也未引入其依赖。

## License

本项目采用 **[CC BY-NC-SA 4.0](LICENSE)**（署名-非商业性使用-相同方式共享 4.0 国际）——
**非商业可以自由使用，商业用途须另行取得书面授权**。

简单说三条你需要遵守的：

- **署名**：保留版权声明，并说明你是否做了修改。README 或页面底部放一句话即可，
  建议格式见 `LICENSE` 末尾。
- **非商业**：不得用于商业目的——包括集成进付费产品、代写工作室收费生产、
  以本项目产出的批量内容直接变现、公司内部营利性生产。判定标准是「产出是否进入收费链路」。
- **相同方式共享**：修改后再分发，必须以 CC BY-NC-SA 4.0 或兼容协议开源，不得改闭源。

**需要商用授权？见 [COMMERCIAL.md](COMMERCIAL.md)**（一事一议，非独占按年授权）。
纯个人使用、学习研究、非营利场景**永远不需要走这一步**。

需要你知道的几条边界：

- **成稿的著作权**不在本许可范围内。你用它写的小说能否投稿、签约、出售，取决于平台条款
  与当地法律，作者不作任何声明或保证，也不主张其权利。
- **`data/knowledge/` 的题材知识卡**是虚构创作参考（人工撰写或公开资料蒸馏），不含真实人物
  与机构影射；二次使用前请自行评估。
- **抓取与生成内容的合规责任**在使用者：抓取频率、数据用途、内容审核由你自行承担。

**CC 管代码与内容，不管你的成稿。** 这两件事分开看，就不会踩坑。

第三方数据与素材的合规提示见「合规与风险提示」一节。

*历史：早期提交 `7a1b2e3`～`a2355c7` 期间曾使用 PolyForm Noncommercial 1.0.0 与 MIT；
自本次变更起统一为 CC BY-NC-SA 4.0。旧版本已发布的副本仍适用其当时的许可。*
