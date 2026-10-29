# 短篇小说工作室（Short Story Studio）

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/license-CC%20BY--NC--SA%204.0-lightgrey?style=flat-square)](LICENSE)
[![Commercial Use](https://img.shields.io/badge/commercial%20use-license%20required-orange?style=flat-square)](COMMERCIAL.md)

抓热点 → 提炼核心梗 → 多 agent 协作扩写出完整短篇小说的一条龙生产线。
产物是可直接投稿的成稿（`final/成稿.md` / `.txt`）。Python 标准库 + 三个依赖：
`requests`（HTTP）、`pyyaml`（配置）、`numpy`（知识库向量检索）。

```
热点抓取 → 提梗打分 → 书名方案 → 章节大纲 → 故事圣经 → 逐章扩写 → 门禁循环 → 润色体检 → 成稿
                                                              │
                                        语言质检 / 毒舌主编 / 读者评分团 / 情绪量化
                                              （任一不过打回重写，直到达标）
```

## 快速开始

```bash
pip install -r requirements.txt          # requests + pyyaml + numpy
cp config.example.yaml config.yaml       # Windows: copy
# 填入 llm.base_url / api_key / model，然后：

# 离线试跑（MockLLM，不需要 API Key）
python -m studio run --idea "婆婆把我陪嫁房过户给小叔子，我用公证反击" --provider mock

# 正式生产
python -m studio run --idea "退婚被索88万彩礼，女主用一张十年前的借条反杀"
```

Windows 也可双击 `启动工作室.bat` 走菜单式操作（一键创作 / 看板 / 热点榜 / 投稿包 / 复盘）。

支持任何 **OpenAI 兼容接口**：DeepSeek、智谱 GLM、Kimi、通义 Qwen，或本地 Ollama / LM Studio
（见 [docs/LOCAL_LLM.md](docs/LOCAL_LLM.md)）

## 命令一览

| 命令 | 说明 |
|---|---|
| `python -m studio run --idea "核心梗"` | 一键成稿（自拟核心梗） |
| `python -m studio run --source zhihu,baidu --pick ask` | 蹭热点成稿，交互选梗 |
| `python -m studio continue` | 续跑最近项目（已完成阶段自动跳过） |
| `python -m studio produce --count 2 --with-improve` | 全流程：写 N 篇 → 发布 → 回收数据 → 复盘 |
| `python -m studio hot --source weibo,zhihu --top 20` | 只看热点榜 |
| `python -m studio mine --source zhihu,baidu,douyin` | 抓热点 + 提梗卡（打分排序） |
| `python -m studio tropes` | 查看梗库方向 |
| `python -m studio fetch --list` | 抓取外部书源原文 |
| `python -m studio kb collect 彩礼纠纷` | 搜集该题材知识卡（`--all` 全部，`--refresh` 重搜） |
| `python -m studio kb stats` | 知识库统计 + **检索模式体检**（embedding / rerank 是否真的生效） |
| `python -m studio kb batch-decon --workers 2` | 批量深拆 `workspace/kb/raw_text/` 原文入叙事弧库 |
| `python -m studio deconstruct 某书.txt` | 拆书（单本或目录） |
| `python -m studio rag search --q "假死 公证"` | 检索统一知识库 |
| `python -m studio material stats` | 素材库统计（`list` / `deposit` / `curate` / `trash`） |
| `python -m studio memory layers` | 记忆库四层分布（`recall` / `consolidate`） |
| `python -m studio schedule add --time 20:00 --count 2` | 定时任务（`list` / `toggle` / `remove` / `run`） |
| `python -m studio scheduler` | 启动调度器常驻进程（`--once` 单轮测试） |
| `python -m studio publish` | 发布成稿：投稿包 / 接口 / mock（`--probe` 探测） |
| `python -m studio analytics fetch --source mock` | 回收阅读数据（`import` 手录，`show` 看趋势） |
| `python -m studio improve` | 数据复盘：门禁预测 vs 真实数据 |
| `python -m studio review` | 查看逐章审查记录（`--recompute` 重算） |
| `python -m studio drafts --show --chapter 3` | 断点保稿查看 / 丢弃 |
| `python -m studio project list` | 项目列表（`delete` 删除） |
| `python -m studio dashboard` | 本地看板（`--export` 导出静态 HTML） |
| `python -m studio local` | 探测本地推理引擎（`--bench` / `--snippet`） |
| `python -m studio check` | 体检：LLM 连通性 + 资产完整性 + 热点源 |
| `python -m studio verify` | **项目自检**（提交前必跑，`--full` 加冒烟） |

常用参数：`--platform zhihu|fanqie|uc|qimao`、`--words 12000`、`--chapters 8`、
`--pick auto|数字|ask`、`--provider mock`、`--no-review`、`--threshold 8`、`--rounds 4`。
全局参数放子命令前后均可。

> **成稿默认不自动沉淀进素材库。** 机器稿回灌会被下一篇当成「爆款素材」检索回来，
> 越写越像自己。要反哺得显式开：`run --deposit`、看板勾选「沉淀素材入库」，或 `material deposit`。

## 内置资产

换题材方向 = 换数据，不用改代码。

| 资产 | 位置 | 内容 |
|---|---|---|
| 梗库 | `data/tropes.json` | 42 个题材方向（下沉世情 / 古言后宫 / 脑洞强设定） |
| 题材知识卡 | `data/knowledge/` | 42 张（真实细节 / 行情钱数 / 场景 / 台词 / 雷点） |
| 题材正文卡 | `data/genre_cards.json` | 32 张（节奏密度 / 禁止漂移） |
| 人设模板 | `data/character_templates.json` | 四角色模板 + 16 个可叠加梗 |
| 读者画像 | `data/readers.json` | 6 类下沉读者（组成评分团） |
| 平台规范 | `data/platforms.json` | 知乎盐选 / 番茄 / UC / 七猫 |
| 文风指南 | `data/style_guide.md` | 下沉文风硬约束 |
| 语言规则 | `data/lang_rules.json` | AI 口癖 / 欧化 / 句式词频（可自行增删） |

## 工程约束

改代码前先读 [docs/DEV_WORKFLOW.md](docs/DEV_WORKFLOW.md)，提交前必跑 `python -m studio verify`。

| 约束 | 位置 | 说明 |
|---|---|---|
| 落盘必须原子写 | `studio/util.py` | 临时文件 + fsync + `os.replace`，强杀不留半截 JSON |
| 项目写入必须持锁 | `studio/lock.py` | 死进程自动回收，并发时抛 `PROJECT_BUSY` |
| 失败必须带错误码 | `studio/errors.py` | `[CODE] 消息` + 位置/实际/期望/改法四元组 |
| 小节头只有一个判据 | `studio/util.py` | pipeline / typeset / 测试共用，禁止写第二份正则 |
| 思考泄漏只有一个判据 | `studio/langqa.py` | 写稿 / 润色 / reasoning 回退共用 |
| 门禁语义要诚实 | `docs/dev/GATE_POLICY.md` | 「没审」不能记成「通过」 |
| 规则只能有一个 owner | `tests/check_rule_drift.py` | 词表/阈值重复报警，回潮直接 FAIL |

## 文档

- [docs/FEATURES.md](docs/FEATURES.md) — 逐阶段功能详解、子系统设计、看板与发布
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 源码分工、数据资产、质量治理判据、门禁表
- [docs/LOCAL_LLM.md](docs/LOCAL_LLM.md) — 本地小模型方案（模型分工 / 混合路由 / 硬件档位）
- [docs/DEV_WORKFLOW.md](docs/DEV_WORKFLOW.md) — 开发流程总览与新增 stage / prompt 的固定步骤
- [docs/dev/](docs/dev/) — 门禁策略、决策记录、预提交检查、验证矩阵

## 合规与风险提示

- 热点仅用作虚构故事的「由头」，成稿禁止影射真实人物/机构；体检阶段复查内容安全。
- 抓取仅限平台公开榜单，频率克制（默认每平台 ≤30 条）；商用分发前请自行评估目标平台协议。
- AI 成稿需人工通读校订后再投稿。知乎盐选要求全稿 ≥8000 字且过稿率低，`report.json` 会提示字数达标情况。

## License

**[CC BY-NC-SA 4.0](LICENSE)**（署名—非商业性使用—相同方式共享 4.0 国际）
