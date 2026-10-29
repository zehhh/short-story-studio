# 本地小模型执行方案

> 用本地推理引擎跑写手/审查/评分，把 token 成本降到零。三种引擎都给 OpenAI 兼容端点，
> 本项目直连（`provider: local`，无需 API Key）。

三大本地推理引擎都提供 OpenAI 兼容端点，本项目的 LLM 客户端可直连：

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
