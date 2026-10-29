# OpenWrite 可落地借鉴清单（面向 E:\小说\短篇）

- 调研日期：2026-10-25
- 调研对象（本地源码，只读）：dsh-Openwrite 插件仓库 + 已安装的 OpenWrite Core（Python 后端）
- 被优化对象：`E:\小说\短篇`（Python CLI 短篇流水线，纯标准库为主，`numpy` 仅用于 kb）
- 本文性质：**工程借鉴清单**，不是产品评测。每条都给出 openwrite 的真实位置（文件:行 / 函数名）、为什么好、短篇怎么落、工作量与优先级。

## 路径别名（全文引用用别名，可逐条核对）

| 别名 | 真实路径 |
|---|---|
| `OWC/` | `E:\dev\dsh\openwrite\environments\7f64b092e5fec6d58f2f2c3d\Lib\site-packages\`（OpenWrite Core，Python） |
| `OWR/` | `E:\小说\长篇\Openwrite-main\`（dsh-Openwrite 插件仓库） |
| `SP/` | `E:\小说\短篇\` |

> **行号稳定性说明**：调研期间 `SP/studio/pipeline.py`、`SP/studio/llm.py` 正在被并行修改（`pipeline.py` 行数在 3383–3528 之间波动）。本文对这两个文件的引用**以函数名为准**，行号仅作定位提示；对只读的 `OWC/`、`OWR/` 源文件与调研期间未改动的短篇文件（`agents.py`、`store.py`、`dashboard.py`、`workbench.py`、`cli.py`、`runlog.py`、`scheduler.py`、`config.yaml`）保留精确行号（快照时间 2026-10-25 22:50）。

## 0. 一页摘要

**结论先行**：openwrite 真正值得抄的不是它的功能面（90 个工具 / 六域 37 项 / 47 节点 DAG / 前端工作台），而是它把「一次会失败的写作任务」拆成**可恢复的持久化协议**的那套工程习惯：写锁 + 原子写 + 保稿 + 版本寻址 + 证据化评审 + 稳定错误码。短篇项目现在的短板恰好全在这条线上：没有跨进程锁、`project.json` 非原子写、中断丢稿、字数门打满把整篇淘汰、主编解析失败默认放行。

**最值得先抄的 5 条**

1. **跨进程项目写锁 + 死进程锁自动回收**（`OWC/tools/project_lock.py`，Windows 用 ctypes 探活）——短篇的 `dashboard.py` 会 `Popen` 起第二个进程，和 CLI 抢同一个项目目录（`SP/studio/dashboard.py:933-948`）。
2. **统一原子写入口**（`OWC/tools/chapter_pipeline.py:168-181` `_atomic_write`：临时文件 + fsync + `replace`）——短篇 `SP/studio/store.py:32-34,45-48` 直接 `write_text` 覆盖 `project.json`，崩一次就是坏 JSON。
3. **断点保稿与草稿发现恢复**（`OWC/tools/chapter_pipeline.py:686-717` + `OWC/tools/chapter_lifecycle.py:68-109`）——短篇只在门禁全过之后才落盘正文（`SP/studio/pipeline.py` 的 `stage_write` 提交处），第 3 轮中断＝前两轮全丢。
4. **评审证据结构：无证据不得分、四轴分离**（`OWC/tools/review_rubric.py:352-373,410-491`）——短篇主编解析失败直接 `return {"verdict":"pass"}`（`SP/studio/agents.py:92-96`），是 fail-open。
5. **字数门「保稿 + 机器可读标记」而不是整篇淘汰**（`OWC/tools/agent/writer.py:321-325` + `writing_policy.py:12-16`）——短篇 `SP/studio/cli.py:707-730` 把项目整体挪进 `rejected/`，正文和原因都没有留在工作台的可查询结构里。

**规模**：24 条可借鉴项（P0 5 条 / P1 12 条 / P2 7 条），11 条明确不抄。按第 4 节三批落地，预计新增约 5300 行新模块代码 + 约 1300 行测试，不改现有依赖（`requests`/`pyyaml` 已存在，`numpy` 仍只用于 kb）。

---

## 1. openwrite 工程架构速写

### 1.1 分层与职责

| 层 | 位置 | 技术 | 职责 | 刻意不做的 |
|---|---|---|---|---|
| **Core（领域后端）** | `OWC/tools/`、`OWC/models/`、`OWC/craft/`（~150 个 Python 模块） | Python 3.12 + pydantic + FastAPI 风格手写路由 | 存储、校验、上下文装配、章节流水线、评审、伏笔、正典、风格、任务、锁 | 不做 agent 循环、不做 UI（`OWR/DESIGN.md:29-38`） |
| **bridge（工具面）** | `OWR/packages/openwrite-bridge/src/tools.ts`（156KB）、`client.ts`、`tool-policy.ts` | TypeScript + `defineTool` × 90 | 把 Core 的 HTTP 动作面注册成 `novel_*` 工具；统一错误归一；读写效应分级 | 不含领域逻辑（`DESIGN.md:31-32`「领域逻辑零搬迁」） |
| **panel（前端）** | `OWR/packages/studio-panel/src/client/`（~40 个 tsx） | React + Vditor + React Flow/ELK | 三个会话视图（创作/资料/任务）、评审&交付 DAG 画布、模型测试台 | 不做业务判断；只读图 API + 每 1.5s revision 轮询（`DESIGN.md:126-128`） |
| **preset + skills（流程）** | `OWR/presets/openwrite/`（`agent.cordis.yml` 20KB + 24 个 SKILL.md） | YAML + Markdown | 人格、门控规则、斜杠命令、技能流程；随安装复制到 `~/.dsh/.agent-presets/` | 不建第二套存储（每个 SKILL 都写「接到已有 novel_*，不要另建进度/伏笔/设定存储」） |
| **conductor（无人值守编排）** | `OWR/conductor/pipeline.py`（24KB） | Python + dsh SDK | 连续写章 → 六域评审 → 低质量/低覆盖/blocker 自动走修订闭环回炉 | 不做前端，不复刻 agent 会话（`DESIGN.md:158-178`） |
| **dsh-dog（只读取证）** | `OWR/vendor/dsh-dog/` | TS + JSON Schema 0.2 | 只读 materialized artifact 做图验证，不调模型、不重算评分（`DESIGN.md:190-216`） | 不写 OpenWrite |

### 1.2 四个设计母题（这是真正值得学的抽象）

#### ① 工具即 API：动作面 → 工具 → 统一封套

- 90 个 `novel_*` 工具一一对应 HTTP 端点，按域分组（`DESIGN.md:93-114` 有完整表）。
- 所有写请求必须带 `X-OpenWrite-Studio: 1`，错误统一为 `{error, code, recoverable, details, request_id}`（`OWR/packages/openwrite-bridge/src/client.ts:17-22` 的契约注释，`:286-313` 的 `readJson` 执行它）。
- 工具按**效应分三档** `read / write / generate`，未知一律 fail-closed 当 `write`（`OWR/packages/openwrite-bridge/src/tool-policy.ts:36-43`），再叠 plan mode / read-only 沙箱拒绝（同文件 `:45-58`）。
- 长操作不占 HTTP 生命周期：写章/评审/修订都提交为 `chapter_write` / `chapter_review` / `revision_from_review` 任务，客户端轮询 `phase`（`DESIGN.md:165-170`，理由写在 `OWR/presets/openwrite/skills/workflow-manager/SKILL.md:36`：「长操作优先走任务系统，避免 HTTP 超时把服务端写锁做成孤儿任务」）。

#### ② 技能即流程：SKILL.md 是薄壳 + 强制路由

- 每个技能 1–5 KB，结构固定：一句话定位 → 「接到已有 `novel_*`」→ 只列该步要用的 3–5 个工具 → 输出格式 → 与相邻技能的分工。例：`OWR/presets/openwrite/skills/write-next/SKILL.md`（6 步）、`review-chapter/SKILL.md`、`revise-span/SKILL.md`、`foreshadow/SKILL.md`、`canon/SKILL.md`、`learn/SKILL.md`。
- 反重复存储是硬规则：`canon/SKILL.md:10` 「人物、地点、世界观以 OpenWrite 资产为准」；`learn/SKILL.md`「风格、成功钩子是当前 Workspace 作品资产，不是会话本地文件，也不是第二套 RAG / lorebook」；`progress/SKILL.md`「不要另建进度文件或阶段机」。
- 门控规则写在**人格**里而不只靠工具校验：正文/大纲/资产写入一律「preview diff → 用户明确确认 → apply，原样回传 preview_token」（`OWR/presets/openwrite/agent.cordis.yml:63-74`）；trace 只留摘要（同文件 `:75-76`）。

#### ③ 作品目录即数据库：一次写一份，版本可寻址

- 单一事实来源：`data/novels/{id}/src/`（设定/大纲/角色）+ `data/novels/{id}/data/`（manuscript / reviews / revisions / workflows / tasks / foreshadowing / traces / dog）。
- **所有版本都用 sha256 指纹寻址**，不是时间戳也不是自增号：`ManuscriptAcceptanceService.fingerprint` = `"sha256:"+sha256(content)`（`OWC/tools/manuscript_acceptance.py:79-81`）；评审记录 `source_revision`、修订提案 `source_revision`、交付图 `current SHA 必须与 review 的 source_revision 一致`（`DESIGN.md:265-269`）都基于它。
- 写操作只有两条路：原子替换（临时文件 → fsync → `replace`，`OWC/tools/chapter_pipeline.py:168-181`）或「先冻结旧字节再做派生」（`manuscript_acceptance.py:1-13` 的协议注释：freeze accepted bytes → invalidate 依赖 → analyze → rebuild facts → propagate）。
- 读写分离的容错取向：读侧永远用磁盘内容 vs 记录 SHA 比对，崩溃只能表现为 `external_change`，不会让旧摘要进入下一次 prompt（`manuscript_acceptance.py:9-12, 99-129`）。

#### ④ 任务 DAG 与证据链：执行态、质量态、覆盖态、门禁态、交付态五轴分离

- 任务：7 个状态 × 7 个阶段 × `attempt/retryable/phase_started_at`（`OWC/tools/task_store.py:22-39`），并给每个 task 类型派生一个**结果引用**（`task_result_ref`，同文件 `:123-150`）。
- 评审：6 个质量域（权重合计 100）+ 1 个不计分硬门禁（27 号敏感词）；37 个旧检查编号只作查询叶子，每个恰好归属一次（`OWC/tools/review_rubric.py:83-148`，`_validate_rubric` 在 import 时强校验 `:184-211`）。
- 评分语义（这是关键工程决策）：**加分制**，`earned>0` 但 `evidence` 为空 ⇒ 强制降级为 `inconclusive`（`review_rubric.py:352-373`）；问题条数不扣分，`critical/blocker` 只改 `gate_status`；`not_applicable` 移出分母，`inconclusive` 不降质量分但降 `coverage`（`aggregate_review` `:410-491`）。
- 五轴输出：`execution_status`（failed/partial/completed）、`quality_score`、`coverage`、`gate_status`（pass/blocked/inconclusive）、`delivery_status`（pass/revise/inconclusive/blocked）互相独立，**不能用总分掩盖未评估**。
- 未校准防护：`production_gate_enabled` 且 `calibration_status != calibrated` 时直接抛错（`review_rubric.py:421-423`），`DESIGN.md:339-342` 记录了 `threshold_calibration.status=uncalibrated`、`production_gate_status=disabled_uncalibrated` 的真实状态。
- 证据落盘：`data/novels/{id}/data/dog/reviews/{chapter}/` 下 `review.json / context.json / domain-*.json / gate.json / aggregate.json / dim_01..37.json / dog-graph.json`（`DESIGN.md:194-207`）；交付图 7 个文件串 `writing → review → revision → application → rereview → closure`（`DESIGN.md:251-269`）。
- 图本身是**一次性定义、逐章实例化**，带 `schema_version` + `revision=sha256`，校验器强制单根树、无环、编号全覆盖（`OWC/tools/review_dag_framework.py:224-332`；`OWR/docs/REVIEW_DAG_FRAMEWORK.md:13,58-67`）。

### 1.3 一条写章链路的真实时序（把上面四条串起来）

```
project_lock.acquire(operation="chapter:write", stage="context")
  └─ heartbeat 15s 写 {pid, token, heartbeat_at, stage}           OWC/tools/project_lock.py:82-142
require_current(chapter)   ← 磁盘 SHA 必须等于 accepted_revision   manuscript_acceptance.py:191-224
scheduler.load_or_create(ch)                                    workflow_scheduler.py:187-192
run_v2 存在 → 从 draft+fact_extract 已完成阶段恢复                 chapter_pipeline.py:720-750
写手生成 → 长度不合区间 →
    ① 分段续写  ② 最多 3 次「定点 edits」重试（old 必须唯一）     agent/writer.py:226-304
    ③ 仍不合 → 保稿 + length_out_of_range/length_warning 标记     agent/writer.py:321-325
commit:_atomic_write 正文 + 立即 start_acceptance（派生失效）      chapter_pipeline.py:184-235
review v2：证据化评分 → aggregate → dog 快照物化                  review_rubric.py:410-491
任务态：phase=model→validating→committing→complete                task_store.py:31-39
失败/取消：_preserve_failed_draft → uncommitted.md + .uncommitted.json 指针
                                                                chapter_pipeline.py:686-717
operation_trace 只存摘要/SHA（不存正文与 prompt 原文）            operation_trace.py:66-76,270-342
project_lock.release()（token 不匹配则不删，防误删他人锁）         project_lock.py:144-153
```

---

## 2. 可借鉴优点清单（24 条）

> 结构固定：**优先级 / 工作量** → openwrite 做法（可核对） → 为什么好 → 短篇落地（改哪个文件、加什么函数/配置） → 验收方式。
> 工作量：S ≤ 半天，M ≈ 1–2 天，L ≥ 3 天（单人、纯标准库实现）。

---

### B01 跨进程写锁：一个 JSON 文件即锁，带心跳、阶段与死进程回收

**优先级** P0 ｜ **工作量** M（新增约 140 行 + 4 处接线）

**openwrite 做法**
- `OWC/tools/project_lock.py:53-110`：`ProjectWriteLock` 的锁就是 `<project>/data/novels/{id}/data/workflows/project.lock`，用 `os.open(..., O_CREAT|O_EXCL)` 原子创建；payload 是 `{token, pid, operation, created_at, heartbeat_at, stage}`。
- 陈旧判定 `_is_stale()` `:155-176`：先看 mtime 与 5 秒初始化宽限，再看 PID 是否存活；**Windows 上不能用 `os.kill(pid,0)`**，`_process_alive()` `:19-50` 改用 `ctypes` + `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess == 259`。
- 心跳线程每 15 秒刷新 `heartbeat_at/stage`（`:122-142`），失败只 `continue`（「监控失败不能释放活着的写者的排他性」，`:126-128` 注释）。
- 释放时校验 token 再删文件（`:144-153`），避免 pid 复用后误删别人刚建的锁。
- 占用时报错信息带 operation/pid/起始时间/阶段/最近心跳（`:112-120`），并提示「终止的进程锁会自动回收」。

**为什么好**
- 崩溃/被 kill 后不需要人工删锁：PID 死了下次 acquire 直接回收；这正是「无人值守跑一夜」的前提。
- 心跳把「进程活着但卡死」和「正在跑」区分开，`stage` 让占用者能解释自己在干什么。
- token 校验消灭了经典竞态：A 超时清理、B 拿到锁、A 又回来删锁。

**短篇落地**
- 新增 `studio/lock.py`（约 130 行，纯 stdlib：`os/uuid/threading/ctypes/json/time`）：
  `ProjectBusy(RuntimeError)`（`code="PROJECT_BUSY"`）、`ProjectLock(root, operation)`（`acquire/heartbeat(stage)/release/__enter__`）、`_process_alive(pid)`、`_is_stale()`（5s 宽限 + 6h 兜底，同 openwrite）。
- 接线点：
  - `SP/studio/cli.py:313 cmd_run`、`:352 cmd_continue`、`:659 cmd_produce`、`:968 cmd_improve` 入口 acquire；
  - `SP/studio/workbench.py:262 save_manuscript`（看板里手动改稿）与 `SP/studio/scheduler.py:189 run_job` 的子进程（子进程自己 acquire，锁冲突写进 runlog 后退出码非 0）；
  - `SP/studio/dashboard.py:933-948` 的 `Popen` 之前只做**非阻塞探测**（不持锁），把 `PROJECT_BUSY` 直接回给前端，避免 UI 显示"已启动"但其实起不来。
- 加 `studio unlock --project X [--force]` 子命令（对应 openwrite「终止进程锁自动回收」的人工兜底）。
- 配置：`studio.lock.stale_after_seconds: 21600`、`studio.lock.grace_seconds: 5`。

**验收**
1. 两个终端同时 `python -m studio run` → 第二个立刻退出，信息含 pid/operation/stage/锁路径，不进模型调用。
2. 第一个 `taskkill /F` 后（不手工删锁）5 秒内第三次 run 能拿到锁。
3. 单测：伪造 `pid=999999` 的锁文件 → `_is_stale()==True`；伪造存活 pid 且心跳新 → `False`。

---

### B02 原子写统一入口：临时文件 + fsync + replace

**优先级** P0 ｜ **工作量** S（新增约 25 行 + 替换 6 处写点）

**openwrite 做法**
- `OWC/tools/chapter_pipeline.py:168-181` `_atomic_write`：`tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False)` → `write` → `flush` → `os.fsync` → `Path.replace(path)`。临时文件放在同目录（保证同卷 rename 原子）。
- `OWC/tools/file_ops.py:12-23` 有同样的 `atomic_write`；`manuscript_import.py:642-645` 用 `f".{target.name}.{uuid4().hex}.tmp"`（避免并发同名临时文件互踩）。
- JSON 状态同样走原子替换：`operation_trace.py:322-332`、`manuscript_acceptance.py:1057-1072`、`model_profiles.py:1130`。

**为什么好**
- 掉电/被杀/磁盘满只会留下一个 `.tmp`，不会留下半截 `project.json`（半截 JSON 会让后续每次启动都 `json.loads` 崩）。
- `fsync` 是「已经告诉用户保存成功」与「真的在磁盘上」的分界；看板/CLI 都读同一批文件，坏一次就是数据事故。

**短篇落地**
- 在 `SP/studio/util.py` 增加 `atomic_write_text(path, text)` / `atomic_write_json(path, obj, indent=2)`（约 25 行，复用 `tempfile/os`）。
- 替换写点：
  - `SP/studio/store.py:32-34 save_meta`、`:45-48 save_json`、`:56-59 save_text`（三个都是裸 `write_text`）；
  - `SP/studio/workbench.py:266-270 save_manuscript`（保留 `.bak` 逻辑，但正文改成原子替换，且先写 `.bak` 再替换，保证任何时刻 `.bak` 是完整的上一版）；
  - `SP/studio/pipeline.py` 的 `stage_write` 提交处与 `_stage_write_hybrid`／无门禁分支的章节落盘 → 改成 `project.write_chapter(no, body)` 统一走原子写；
  - `SP/studio/cli.py:707-730` 的 `project.root.rename(dest)` 前后补一个 `meta["moved_to"]` 原子写，避免"目录已经移走但 project.json 还写着工作台阶段"。
- 已经原子化的保留：`SP/studio/kb.py:352-355`、`kb_dedup.py:200-203`（可改为调用新工具函数，减少重复实现）。

**验收**
1. 在 `atomic_write_json` 里注入 `os.replace` 之前的 `raise` → 原文件内容字节不变。
2. `python -m studio run` 中途 `taskkill /F` → 下次启动 `project.json` 可解析，且 `.tmp` 残留文件可被 `studio check` 报出来（顺带清理）。
3. `grep -n "write_text(" studio/store.py` 只剩原子封装内部一处。

---

### B03 断点保稿与草稿发现恢复：`.uncommitted` + 指针文件 + 扫描器

**优先级** P0 ｜ **工作量** M（新增约 200 行 + 3 处调用）

**openwrite 做法**
- 失败即保稿：`OWC/tools/chapter_pipeline.py:686-717` `_preserve_failed_draft` 把 `partial_content` 或已完成的 draft artifact 写到 `chapter_runs_v2/artifacts/{run_id}/uncommitted.md`，并在正文旁边写**指针文件** `<chapter>.uncommitted.json`（内容 `{status, run_id_v2, draft_path}`，`:704-711`）。
- 发现恢复：`OWC/tools/chapter_lifecycle.py:68-109` `list_drafts()` 双路扫描——正文目录 `rglob("*.uncommitted.json")` + artifact 目录 `glob("runv2_*/uncommitted.md")`，逐条校验 run 是否存在、路径是否在 store 根内（`:87`）、是否已有 `restored.json`（`:85`），输出 `{chapter_id, run_id_v2, word_count, created_at, draft_revision, restorable}`。
- 断点续跑以「阶段都 completed」为前提：`_load_resumable_writer_result` `:720-750` 只接受 `draft` + `fact_extract` 都完成，否则抛 `RUN_NOT_RESUMABLE`（不假装能恢复）。
- 取消也要保稿：`_raise_if_task_cancelled` `:678-683` 把 `partial_content` 挂到异常上，由上层落盘。

**为什么好**
- 「中断」被建模成**一等状态**而不是异常路径：正文、草稿、指针三者一致，重启后可以枚举、可以恢复、可以丢弃。
- 恢复前做路径越界检查和 `restored.json` 幂等标记，避免第二次 `recover` 又把旧稿盖回去。
- 明确区分「已完成阶段可恢复」与「阶段未完成不可恢复」，不会给出乐观假象。

**短篇落地**
- 新增 `studio/drafts.py`（约 180 行）：
  - `Project.save_draft(no, text, meta)` → 写 `chapters/.drafts/ch{no:02d}.md` + `chapters/.drafts/ch{no:02d}.json`（`{round, stage, words, model, role, created_at, sha256}`）；
  - `Project.commit_chapter(no, body)` → 原子写 `chapters/ch{no:02d}.md` + 写 `chapters/ch{no:02d}.commit.json`（`{sha256, committed_at, gate}`）+ 删除该章 draft；
  - `list_recoverable(project)` → 扫描 `.drafts/*.md`，用 sha256 与已提交正文比对，排除已提交/已恢复（参照 `chapter_lifecycle.list_drafts` 的双路去重与幂等标记）；
  - `restore(project, no)` → 把 draft 提升为正文候选（不直接覆盖，返回文本给管线重新过门禁）。
- 管线写入时机（关键）：`SP/studio/agents.py:276-294`（初稿后）、`:295-345`（禁道具/语言质检每轮后）、`:349-415`（主编/读者团每轮后）、`:418-473`（情绪门每轮后）各存一次 draft；`pipeline.stage_write` 全部通过后 `commit_chapter`。
- CLI：新子命令 `studio recover [--project NAME] [--apply ch05]`、`studio drafts`（列可恢复稿）；`cmd_continue`（`SP/studio/cli.py:352`）开头检测可恢复稿并提示。

**验收**
1. 在主编第 2 轮打回后 `Ctrl+C` → `studio drafts` 列出该章、轮次、字数、sha256；`studio recover --apply` 后重新只跑第 3 轮门禁，不重写第 1–2 轮。
2. 已正常提交的章节不出现在 `studio drafts`（sha256 一致被排除）。
3. 手动把 draft 路径写成 `../../../etc/passwd` → 扫描器拒绝（路径越界检查）。

---

### B04 统一错误封套 + 稳定错误码 + request_id（含传输错误落盘）

**优先级** P0 ｜ **工作量** S（新增约 90 行 + 改 8 处 raise/except）

**openwrite 做法**
- HTTP 层契约写在客户端注释里并被执行：错误体 `{error, code, recoverable, details, request_id}`，非 2xx（`OWR/packages/openwrite-bridge/src/client.ts:17-22, 286-313`）；`failure.requestId` 从响应体取（`:302`）。
- 领域异常自带码：`ProjectBusyError.code="PROJECT_BUSY"`（`project_lock.py:15-17`）；`ManuscriptAcceptanceError(message, code=..., recoverable=..., details=...)`（`manuscript_acceptance.py:53-65`），码表如 `STALE_ACCEPTED_REVISION`、`ACCEPTED_SOURCE_CHANGED`、`CONFIRMATION_REQUIRED`、`OPERATION_NOT_FOUND`；修订侧 `DOCUMENT_CONFLICT` / `REVIEW_STALE` / `HUNK_SELECTION_MISMATCH`（`revision_service.py:180-186, 502, 704-714`）；写作侧 `CHAPTER_LENGTH_OUT_OF_RANGE`（`agent/writer.py:222, 318, 661`）。
- 传输层故障也有证据：请求异常时把 `{source:"transport", code:"NETWORK_ERROR"|"REQUEST_ABORTED", path, tool_name, tool_call_id}` 写到 `<project>/.openwrite/transport-errors/<uuid>.json`，并且**写失败也不能掩盖原错误**（`client.ts:261-279`，注释 `:272`）。
- 请求级追踪：`record_operation_trace(route, request_id, ...)`（`OWC/tools/studio_application.py:298-348`）把 request_id、路由、上下文（workspace/session/tool_call_id）与结果引用写成一个 trace 文件。

**为什么好**
- 用户/agent 看到 `PROJECT_BUSY` 能立刻知道「等一下或换项目」，看到 `STALE_ACCEPTED_REVISION` 知道「正文变了要重新接纳」——错误码把运维手册写进了代码。
- `request_id` 把「用户抱怨」与「某次运行的日志」对上号；短篇现在只有 `print()` 和 `SystemExit`，出问题只能翻全文日志。
- 传输错误单独落盘，解决「模型调用超时到底是网络还是逻辑」这类最常见的扯皮。

**短篇落地**
- 新增 `studio/errors.py`（约 90 行）：
  - `class StudioError(Exception)`：`code / recoverable / details / request_id`；`__str__` 输出 `CODE: message`；
  - 码表常量：`PROJECT_BUSY / INVALID_CONFIG / PROJECT_NOT_READY / PROVIDER_AUTH_ERROR / PROVIDER_RATE_LIMIT / PROVIDER_TIMEOUT / INVALID_MODEL_OUTPUT / CHAPTER_LENGTH_OUT_OF_RANGE / EXTERNAL_CHANGE / STALE_ACCEPTED_REVISION / DOCUMENT_CONFLICT / CANCELLED / INTERNAL_ERROR`；
  - `new_request_id()` = `run_{YYYYmmddTHHMMSS}_{uuid4().hex[:8]}`；`runlog.start()`（`SP/studio/runlog.py:67`）设置全局 `CURRENT`，CLI 顶层 `except StudioError` 统一打印 `[错误] CODE: msg（request_id=...，日志 workspace_logs/...）` 并以非 0 退出。
- 改造点：`SP/studio/llm.py:19-20`（`LLMError` 空类 → 带 code 的子类；`:318-347` 已有 429/连接退避，补上码与 `Retry-After` 记录）、`SP/studio/config.py` 校验错误、`SP/studio/pipeline.py` 的 `raise SystemExit("缺少大纲，无法扩写")`（`stage_write` 开头）→ `StudioError(code="PROJECT_NOT_READY")`。
- 看板：`SP/studio/dashboard.py:951-958` 等 `{"error": ...}` 响应统一加 `code`/`request_id`；`/api/*` 顶层 `except`（`:963-964`）改成回封套 JSON。
- 传输错误落盘：`workspace_logs/errors/<request_id>.json`，只存 `{code, path, role, model, attempt, elapsed_ms, message}`，**不存 prompt 正文**（与 `operation_trace` 的隐私取向一致）。

**验收**
1. `grep -rn "SystemExit(" studio/` 只剩 CLI 参数错误与 2–3 个真正的用户中止。
2. 故意填错 api_key → 输出 `PROVIDER_AUTH_ERROR` + request_id，且 `workspace_logs/errors/<id>.json` 存在。
3. 看板任一失败端点返回 `{"ok":false,"code":...,"request_id":...}`。

---

### B05 写入前就绪预检：模板占位、缺大纲节点、冻结组 → 稳定码

**优先级** P0 ｜ **工作量** S（新增约 110 行 + 2 处调用）

**openwrite 做法**
- `OWC/tools/write_guard.py:1-19` 的 docstring 直接写了它要堵的坑：「项目正典还是模板占位、或章节 id 在大纲里没有节点 → 管线照样装配出薄上下文 → 模型自由发挥还能骗过盲审」。
- 占位检测：只认 `待填写` 这一个标记（`:31` 注释解释为什么不用更宽的「占位」），必需文档 4 个（`:35-40`）。
- `validate_chapter_writable` `:108-163` 返回 `{ok, code, message, issues[]}`，码为 `PROJECT_NOT_READY` / `TARGET_NOT_PLANNED` / `ARC_FROZEN`（`:124-156`）；章 id 必须 `ch_\d+`（`:138`）。
- 冻结组检查前置到写章入口：`ArcAuditService.require_unfrozen(chapter)`（`:124-129` 调用，实现 `OWC/tools/arc_audit.py:436-441`）。

**为什么好**
- 把「上下文薄 → 模型乱写 → 评审也审不出来」这条最贵的事故链，用**零 token 的确定性检查**挡在入口。
- 错误信息直接给出下一步动作（「请先补该章章纲（或先生成滚动规划/确认章纲）再运行」），可被 agent 直接转述给作者。

**短篇落地**
- 新增 `studio/precheck.py`（约 110 行），`ready_issues(cfg, project) -> list[str]` + `validate_run_target(...) -> {ok, code, issues}`：
  - 阶段前置：`pitch.json / lead.json / outline.json / bible.json` 是否存在且非空（对照 `SP/studio/store.py:10` 的 `STAGES`）；
  - 空壳检测：`outline.chapters` 为空 / 任一章缺 `title|goal|beats` / `pitch.title` 为空 / `bible.characters` 为空 → `PROJECT_NOT_READY`；
  - 目标合法性：`--only N` 越界 → `TARGET_NOT_PLANNED`；
  - 配置一致性：`review.word_floor < review.word_ceiling`、`llm.api_key` 非空、`llm_roles.*.model` 非空 → `INVALID_CONFIG`；
  - 知识库不可用只 warning（`kb.recall` 失败在 `SP/studio/pipeline.py` 的 `_chapter_ctx`（`kb.recall` 的 except 分支） 本来就被吞掉）。
- 接线：`SP/studio/cli.py:313 cmd_run` / `:352 cmd_continue` 第一行调用；`SP/studio/dashboard.py` 的 run 端点（`Popen` 之前）调用并把 issues 回给前端。

**验收**
1. 删掉 `outline.json` 后 `studio run` 立即失败（不消耗 token），输出含缺失项与修复建议。
2. 把 `word_floor` 改成大于 `word_ceiling` → `INVALID_CONFIG`。
3. 单测覆盖 6 个分支各一条断言。

---

### B06 字数门「保稿 + 标记回流」，而不是整篇淘汰

**优先级** P1 ｜ **工作量** M（改 `agents.py` 约 80 行 + `cli.py` 20 行 + 报告字段）

**openwrite 做法**
- 契约只有一处：`chapter_word_bounds(target)` = `-20% / +50%`（`OWC/tools/writing_policy.py:12-16`），prompt 与校验器共用（模块 docstring「shared by prompts and validators」）。
- 打回策略是**分级**的（`OWC/tools/agent/writer.py:226-333`）：
  1. 低于下限 → 分段续写 `_segmented_write`（`:231-234`）；
  2. 否则最多 3 轮「定点修改」：要求模型只返回 `{"edits":[{"old","new"}]}`，且 `old` 必须逐字唯一出现（`:258-262, 288-297`），每轮降温度 0.15（`:275`）；
  3. 3 轮用尽 → **不丢弃**：`parsed["length_out_of_range"]=True; parsed["length_warning"]=str(exc)`（`:321-325`，注释明确「保留最后一版正文…标记后回流给调用方，而不是丢弃整份初稿」）。
- 标记一路带到产物与恢复路径：`AgentResult.length_out_of_range/length_warning`（`agent/writer.py:51-52,164-165`）、`_load_resumable_writer_result` 也恢复这两个字段（`chapter_pipeline.py:748-749`）、执行结果返回（`:1011-1012`）。

**为什么好**
- 「字数不达标」和「稿子不能用」是两件事。前者是可修复的质量信号，后者才该淘汰；把两者混在一起会烧掉最贵的写作 token。
- 定点 edits 相比整稿重写：省 token、避免模型把已合格段落改坏（短篇 `_no_shrink` 只防坍缩，不防"改写走形"）。
- 标记进入机器可读记录后，`produce`/看板/复盘都能查询，而不是只留一行 print。

**短篇现状（对照）**
- `SP/studio/agents.py:276-294`：只有 `0.7 × target` 下限、没有上限、扩写是**整稿重写**（`writer.rewrite`），且超 `1.8×` 才回退；`SP/studio/pipeline.py` 的 `word_gate` 有升级式打回但最终 `gate="maxed_words_low"`（函数尾），由 `SP/studio/cli.py:707-730` **把整个项目目录 rename 到 `rejected/`**——正文和原因都离开了工作台的可查询结构。

**短篇落地**
- `SP/studio/agents.py:276-294` 改为读 `review.word_floor/word_ceiling`（配置已有，`SP/config.yaml:60-61`），并按 openwrite 分级：低于 floor → 用现成的 `_rewrite_section` 做定点扩写；高于 ceiling → 同样定点精简。
- 顶端加唯一重试开关 `review.length_fix_rounds`（默认 3），用「只返回 edits + old 唯一」的 JSON 协议；失败降级为整稿（保留现有行为）。
- 轮次用尽：`reviews/chNN.json` 与 `meta.reviews[no]` 写 `{"gate":"maxed_words_low","length_out_of_range":true,"length_warning":"...", "floor":..., "ceiling":..., "words":...}`（`SP/studio/pipeline.py` 的 `stage_write` 中 `reviews/` 落盘处已有落点）。
- `SP/studio/cli.py:707-730`：**保稿**——正文留在 `chapters/`，项目保留在工作台并标 `meta.gate_status="length_out_of_range"`；只有用户显式 `--reject` 或连续 N 篇不达标才移动目录，并在 `report.json` 写 `{"rejected":true,"reason":"CHAPTER_LENGTH_OUT_OF_RANGE","request_id":...}`。
- 看板加一列「门禁未过原因」，直接读 `meta.gate_status`。

**验收**
1. 把 `word_floor` 调到 20000 跑一篇 → 项目仍在 `workspace/projects/`，正文完整，`report.json.word_gate.length_out_of_range=true`，看板可筛选。
2. 长度重试的 token 消耗比整稿重写下降 ≥40%（用 `llm.usage_summary` `SP/studio/llm.py:86-98` 前后对比）。
3. 单测：构造超长/超短模型输出，断言 3 轮后保稿且标记正确。

---

### B07 评审证据结构：无证据不得分 + 四轴分离 + 未校准禁用门禁

**优先级** P1 ｜ **工作量** M（新增 `studio/review.py` 约 350 行 + 改 `agents.py` 40 行 + 报告迁移）

**openwrite 做法**
- 评分单元是 `criterion`：`{id, name, status(evaluated/not_applicable/inconclusive), earned, max, evidence[], rationale, issues[]}`（`OWC/tools/review_rubric.py:352-373`）。
- **硬规则**：`earned > 0` 但 `evidence` 为空 ⇒ 强制 `inconclusive` 且 `earned=0`（`:356-372`）。没证据就是没得分。
- 域归一化带覆盖率：`coverage = evaluated_max / potential_max`（`:376-400`）。
- 聚合产出五轴 + 阈值字段（`:410-491`）：`quality_score`（`earned/evaluated_max*100`）、`coverage`、`gate_status`、`delivery_status`（pass/revise/inconclusive/blocked）、`execution_status`；`critical/blocker` 只改门禁不改分（`:403-407, 434-436`）。
- 校准门：`production_gate_enabled` 需要 `calibration_status=="calibrated"`，否则 `ValueError`（`:421-423`）；默认阈值 70 / 覆盖率 0.80（`:17-18`）。

**为什么好**
- 加法制 + 证据门槛，把「问题越多分越低」变成「证明了才给分」：模型少评几项只会降低 `coverage`，不会伪装成「质量很好」。
- `coverage` 与 `quality_score` 分开，能在报告里说清「85 分但只覆盖了 60% 检查项」这种真实情况。
- 未校准就禁止把评审当交付门禁——这条纪律直接避免了「换了模型后阈值失效仍在上线」。

**短篇现状（对照）**
- `SP/studio/agents.py:92-96`：`chat_json` 解析失败 → `print` 后 `return {"verdict":"pass","score":7.0,"issues":[]}`，**fail-open**；读者团同样默认 7.2（`:122-128`）、情绪门默认 7.0（`:198-204`）。
- issue 结构是非强制的 `criterion/severity/location/note/fix`（`SP/studio/agents.py:151-154` 渲染），没有证据字段、没有"未评估"概念、没有覆盖率。

**短篇落地**
- 新增 `studio/review.py`（约 350 行，纯 stdlib），把短篇自己的 4 道门禁映射成域（**不要抄 37 项**，见第 3 节）：
  - `coherence`（连贯/时间线/设定冲突）、`character`（人物一致性/台词）、`plot`（钩子/爽点/情绪弧）、`prose`（语言质检/文风/禁用项）、`pacing`（节奏/章节功能）、`canon`（bible 契约），权重 100；
  - `gate`：敏感词/禁用道具（复用 `SP/studio/langqa.py` 的确定性扫描，0 分，只改 gate）。
- 改 `SP/studio/agents.py:76-96 ChiefEditor.review`：解析失败 → `{"execution_status":"failed","code":"INVALID_MODEL_OUTPUT"}`（fail-closed 到 inconclusive，不是 pass）；`review.md` prompt 要求每条 criterion 返回 `status/earned/evidence[quote]`。
- 新增 `studio/review.py: aggregate(report)` → 五轴，`SP/studio/pipeline.py` 的 `stage_write` 中 `reviews/chNN.json` 改成 `{schema_version, rubric_version, ...五轴, domains[], gates[]}`；`meta.reviews[no]` 只存摘要（保持看板兼容）。
- 阈值接 `SP/config.yaml:59-81`（`score_threshold/final_score_floor`），新增 `review.calibration_status: "uncalibrated"` 与 `review.production_gate: false`；`uncalibrated` 时 `delivery_status` 一律降级并写入 `report.json`。

**验收**
1. 把 `review.md` 的输出改成空 issues + score 9 → 报告 `coverage<0.8`、`delivery_status=inconclusive`，不会出现 pass。
2. 主编返回一条 `earned>0` 无 evidence 的 criterion → 该 criterion 记 `inconclusive`、得分 0。
3. 看板显示「质量分 / 覆盖率 / 门禁 / 交付」四列而不是单一分数。

---

### B08 伏笔系统：从「散文本声明」升级为带状态的 DAG + 章节义务简报

**优先级** P1 ｜ **工作量** M（新增约 320 行 + 3 处注入 + 迁移脚本）

**openwrite 做法**
- 状态机：`埋伏 / 待收 / 已收 / 废弃`，层级 `主线/支线/彩蛋`，权重 1–10（`OWC/tools/foreshadowing_manager.py:119-165, 245-268`）。
- 校验器（`:311-376`）：DFS 环检测 → 边引用不存在节点 → **超期未回收**（`current_chapter_number > target_chapter` 且状态仍 open，`:357-373`），当前章节号取自 `novel_config.yaml.current_chapter` 与 `manuscript/arc_*/ch_*.md` 的最大值（`:378-404`）。
- 义务简报把 DAG 变成「今天要做什么」：`ChapterWorkBriefService` 每章输出 `must_resolve / overdue / to_plant` 三组（`OWC/tools/chapter_work_brief.py:48-67, 578-595`），写手写章前读它（`OWR/presets/openwrite/skills/write-next/SKILL.md` 第 4 步）。
- 审计侧交叉验证「声明 vs 实际」：`arc_audit.audit` 把 `payoff_of` 声明、`confirmed_recovered`、`net_new_debt`（新增−回收）并列，声明兑现没兑现一目了然（`OWC/tools/arc_audit.py:190-200, 255-262`）。

**为什么好**
- 「伏笔」最容易退化成写在设定文档里的一段漂亮文字；一旦有 id/状态/目标章，它就能参与门禁、能报警、能被审计。
- 三组分类（该收的/已超期的/该埋的）是写手可直接消费的短 prompt，比"把全部伏笔表塞进上下文"省 token 且不遗漏。

**短篇现状（很好的起点）**
- `SP/prompts/bible.md` 已经要求模型输出结构化伏笔表：`{"id","plant_chapter","content","payoff_chapter","payoff_way"}`，落在 `bible.json`。
- `SP/studio/pipeline.py` 的 `_chapter_ctx` 中 `foreshadow_brief` 目前把**全部** `bible.foreshadows` 拼成一行行文字塞进 `BIBLE`，没有状态、没有超期、没有"本章该收哪些"。

**短篇落地**
- 新增 `studio/foreshadow.py`（约 260 行，纯 stdlib，JSON 不用 YAML）：
  - `data/foreshadow/dag.json`：`{"nodes": {id: {content, weight, layer, status, created_at, target_chapter, resolved_at}}, "edges": [{from,to,type}]}`；
  - `migrate_from_bible(project)`：把 `bible.json.foreshadows` 转成节点（`created_at=ch{plant:03d}`、`target_chapter=ch{payoff:03d}`、`status=埋伏`），幂等；
  - `pending(min_weight, layer)` / `update_status(id, status)` / `validate()`（环检测 + 悬空边 + 超期，完全照 `foreshadowing_manager.validate_dag` 的结构）；
  - `obligations(project, chapter_no)` → `{"must_resolve":[...], "overdue":[...], "to_plant":[...]}`（规则同 `chapter_work_brief._foreshadow_category`：`payoff_chapter == 本章` → must_resolve；`< 本章` 且未收 → overdue；`plant_chapter == 本章` → to_plant）。
- 注入：`SP/studio/pipeline.py` 的 `_chapter_ctx` 用 `obligations()` 替代现在的全量 `foreshadow_brief`（保留全量作为 `_ALL_FORESHADOW` 给审计用）；`SP/studio/agents.py:76-96` 的主编 prompt 增加 `OBLIGATIONS`（占小量 token，让评审能判"该收未收"）。
- 写后结算：`pipeline.stage_write:2037-2064` 落盘后，用确定性关键词/`payoff_way` 命中把 `must_resolve` 标 `已收`（不确定就留给 `studio foreshadow resolve <id>` 人工/LLM 确认，并写 `resolved_by`）。
- CLI：`studio foreshadow list|pending|check|plant|resolve|audit`。

**验收**
1. bible 里 F1 payoff_chapter=3 但第 4 章已提交 → `studio foreshadow check` 报 `overdue: F1`，且第 4 章写前上下文含该义务。
2. 构造 A→B→A 的边 → `check` 报环并给出涉及节点。
3. 迁移幂等：连跑两次 `migrate_from_bible`，节点数不变。

---

### B09 正典/真值文件 + 章节快照回滚（canon 与 truth-validation）

**优先级** P1 ｜ **工作量** M（新增约 240 行 + 2 处接线）

**openwrite 做法**
- 真值文件是一组 Markdown（世界当前状态 / 资源账本 / 关系矩阵），由 `TruthFilesManager` 统一读写并带元信息（`OWC/tools/truth_manager.py:40-73, 138-162`）。
- 快照与回滚：`create_snapshot(chapter_number)` / `restore_snapshot(id)` / `load_snapshot_before(chapter)` / `list_snapshots()`（`:295-421`）——每章写完后留一份可回滚的真值副本。
- 事实抽取与对照：`extract_facts_from_chapter`（`:489+`），配合 `truth-validation` 技能定义的 5 类问题：状态矛盾（critical）、时间悖论（critical）、叙事缺失（warning）、伏笔异常（warning）、回溯编辑（info）（`OWR/presets/openwrite/skills/truth-validation/SKILL.md` 的问题类型表）。
- `canon/SKILL.md` 的纪律：「人物、地点、世界观以已注册资产为准…不要另建第二本 lorebook」，改设定必须走 `structured_change_plan`。

**为什么好**
- 短篇只有 9k 字，但「上一节说在 A、下一节人在 B」「账本金额前后不符」是读者最容易抓的硬伤，而且这类问题**确定性可查**，不该浪费 LLM 轮次。
- 先快照再改，才有"改坏了回滚"的选项；否则只能重跑整篇。

**短篇现状**
- 正典事实散在 `bible.json`（`characters/timeline/setting/tone/foreshadows`，`SP/studio/pipeline.py` 的 `_chapter_ctx` 中 `bible_brief`）与各章摘要 `meta.chapters_state.summaries`（`SP/studio/pipeline.py` 的 `chapters_state.summaries`）里，没有统一"当前状态"文档，也没有快照。

**短篇落地**
- 新增 `studio/canon.py`（约 240 行）：
  - `data/canon/current_state.md`（地点/时间/在世人物/关键物品状态）、`data/canon/ledger.md`（金额/期限/欠条等可数事实）、`data/canon/relationships.md`；
  - 生成：每章提交后 `update_from_chapter(no, text)` 用**确定性抽取**（时间词/地点词/数字+单位，参照 `SP/studio/pipeline.py` 的 `_extract_idea_core_kws`（数字抽取） 已有的 `_extract_idea_core_kws` 数字抽取思路）+ 可选一次小模型归纳，写回并保留 `source_chapter`；
  - 快照：`snapshot(no)` 把三个文件复制到 `data/canon/snapshots/ch{no:03d}/`，`restore(no)` 原子覆盖，`diff(no_a, no_b)` 返回行级差异；
  - 校验：`validate(project, chapter_no, text)` 只做确定性检查——时间词单调性、地点与 `current_state` 冲突、账本数字未解释跳变、人物"已死亡/已离场"却出场；输出 `{code:"STATE_CONTRADICTION"|"TIMELINE_PARADOX"|"NARRATIVE_GAP", severity, evidence:{quote, location}}`（证据带原文片段，对应 openwrite 的 evidence.quote 约定）。
- 接线：`pipeline.stage_write` 提交后 后调用 `canon.update_from_chapter` + `canon.snapshot(no+1)`；`pipeline.stage_polish` 前跑 `canon.validate` 把问题作为额外 issues 注入终审（复用 `extra_issues` 通道 `SP/studio/pipeline.py` 的 `stage_quality_gate`）。
- CLI：`studio canon show|check|snapshot|restore|diff`。

**验收**
1. 第 2 节把主角写到"城南"，第 3 节无过渡写到"城北" → `canon check` 报 `STATE_CONTRADICTION` 且带引用原文片段。
2. `canon restore 2` 后 `current_state.md` 与当时快照字节一致。
3. 校验全部是确定性代码：单测不需 LLM 即可覆盖 5 个分支。

---

### B10 修订提案与 hunk 级应用（revise-span）：改稿不覆盖整章

**优先级** P1 ｜ **工作量** M（新增约 300 行 + 复用现有 patch 通道）

**openwrite 做法**
- 提案是持久对象：`{proposal_id, chapter_id, source_revision(sha256), selection{start,end,original_text}, diff{hunks[{id,before,after}]}, status, created_at}`（`OWC/tools/revision_service.py:531-566`）。
- 冲突拒绝：`_check_conflict` `:683-714` 三重校验（当前 sha ≠ proposal.source_revision / 区间越界 / 原文片段不符）→ 把提案标 `stale` 并抛 `DOCUMENT_CONFLICT`，`details` 里带 `expected_revision/current_revision`（`:704-714`）。
- 应用支持 hunk 选择：`_accepted_replacement` `:462-512` 校验 `selected_hunk_ids` 必须存在、不重复、不越界，否则 `HUNK_SELECTION_MISMATCH`（`:502`）。
- 评审锚点解析：issue 的 `evidence.quote` 必须在正文中**唯一出现**才定位成功（`_resolve_issue_anchor` `:716-733`）。
- 应用后联动：旧评审立刻 `stale`（`_mark_review_stale` `:771-779`）；正文变化一律令评审失效，交付前必须复评（`DESIGN.md:265-269`）。
- 校验器只阻断**新引入**的错误：`_default_validator` `:751-768` 用 before/after 的错误集合差集决定 `blocking`，避免"历史问题反复挡住本次修改"。

**为什么好**
- 「改一段」而不是「重写一章」是作者的真实动作；hunk 级 + 唯一锚点让修改可审、可撤、可只接受一部分。
- 版本冲突显式失败（而不是覆盖），配合 `stale` 传播，保证"评审说的正文"就是"现在的正文"。
- 只阻断新增错误这个细节，解决了很多"越修越卡"的死锁。

**短篇现状**
- 已有类似能力但不可审：`SP/studio/pipeline.py` 的 `_sanitize_lead` 之后的 patch 通道（`_apply_patches` 被 polish 使用，`patch_fix` 配置见 `SP/config.yaml:76` 注释「search/replace 确定性应用」），但补丁是**过程中间产物**，没有提案对象、没有版本校验、没有 apply/reject 记录。

**短篇落地**
- 新增 `studio/revision.py`（约 300 行）：
  - `data/revisions/rev_*.json`，字段照 openwrite（含 `source_revision`、`selection`、`hunks[]`）；
  - `create_selection(project, no, start, end, intention)`、`create_from_review(project, no, issue)`、`apply(project, rev_id, selected_hunk_ids)`、`reject(rev_id)`、`regenerate(rev_id)`；
  - 复用 `pipeline._apply_patches`（先抽出为 `studio/patch.py` 的纯函数，`old` 必须 `count==1`，同 `revision_service._resolve_issue_anchor` 的唯一性要求）；
  - `apply` 前做三重冲突校验，成功后原子写正文 + `meta.reviews[no]["stale"]=true` + 写 `data/revisions/applied/{rev_id}.json` 审计；
  - 校验只阻断新错误：把 `langqa.scan` 的 before/after 错误集合做差集（同 openwrite `:751-768`）。
- CLI：`studio revise --chapter 5 --start 120 --end 200 --intent "压缩心理描写"`、`studio revise apply rev_xxx --hunks h1,h2`、`studio revise reject`；看板加"修订"面板（现有 workbench 的编辑器可直接用 `save_manuscript` 之前加一个 diff 确认弹窗，思路同 `OWC/tools/manuscript_editing.py:283` 的 `.tmp` 写）。

**验收**
1. 生成提案后手动改正文再 apply → 拒绝并给出 `DOCUMENT_CONFLICT` + 两个 revision。
2. 只选中 h1 → 正文只改 h1，报告记录 `selected_hunk_ids`。
3. apply 后 `reviews/chNN.json.stale=true`，`studio publish` 拒绝未复评稿。

---

### B11 接纳事务：accepted/pending revision + 五阶段不可跳步

**优先级** P1 ｜ **工作量** L（新增约 380 行 + 拦 3 个写入口）

**openwrite 做法**
- 协议写在模块 docstring（`OWC/tools/manuscript_acceptance.py:1-13`）：任何来源改正文（Studio、agent、修订恢复、导入、外部编辑器）都必须走同一条因果链——冻结已接受的字节 → 立刻让派生件失效 → 分析冻结版本 → 重建可重放事实 → 才允许继续生成。
- 五阶段：`STAGE_NAMES = capture/invalidate/analyze/rebuild/propagate`（`:34`），每阶段有 `status/error_code/error_message`（`:347-361`）。
- 状态机：每章 `head = {accepted_revision, pending_revision, facts_revision, status, operation_id}`（`:99-129`），状态含 `baseline_required/pending/processing/failed/stale/current/external_change`。
- 读侧容错：`inspect()` 每次拿磁盘内容算 sha 与三处记录比对；不一致 → `external_change`（`:110-120`）。docstring 明确「崩溃发生在 rename 与 journal 之间时，读侧 fail closed 成 external change，而不是让旧摘要进入下一次 prompt」（`:9-12`）。
- 恢复：`resume(operation_id)` 从失败阶段继续，已完成直接返回（`:325-369`）；`abandon` 保留审计（`:395-424`）；错误码 `STALE_ACCEPTED_REVISION`（`:313`）、`ACCEPTED_SOURCE_CHANGED`（`:305,338`）。

**为什么好**
- 它回答的是短篇最痛的问题：**「作者手动改了两行，系统还能不能信自己的摘要/伏笔/大纲」**。答案是显式状态 + 拒绝继续，而不是悄悄用旧摘要生成下一节。
- 五阶段把「写文件成功」和「派生件已同步」分开，中间崩溃也能定位到具体阶段并继续。
- `accepted` 与 `pending` 分离，允许"改稿已保存但事实还在重建"，人机可以并发。

**短篇落地（做精简版，5 阶段 → 3 阶段）**
- 新增 `studio/acceptance.py`（约 380 行）：
  - `data/acceptance/state.json`：`{chapters:{ch05:{accepted_revision, pending_revision, status, operation_id}}, revision}`；
  - `STAGE_NAMES = capture/invalidate/rebuild`（短篇没有独立 facts 存储，重建 = 更新 `meta.chapters_state.summaries/tails` + `canon` + `foreshadow` 结算）；
  - `start_acceptance(no, expected_previous_revision)`（正文提交时调）、`accept_external(no)`（看板/CLI 检测到手动改稿时调）、`resume(op_id)`、`abandon(op_id, confirm)`、`require_current(no)`（管线写章前调，不一致拒跑）；
  - 指纹用 `hashlib.sha256`（同 openwrite `:79-81`）。
- 拦截三个写入口：`SP/studio/workbench.py:262 save_manuscript`、`SP/studio/dashboard.py:949-958 /api/manuscript`、`SP/studio/pipeline.py` 的章节落盘 → 一律先 `start_acceptance`。
- `SP/studio/cli.py cmd_run/cmd_continue` 开头 `require_current` 全部已写章节，有 `external_change` 就要求 `studio accept chNN` 后再跑。
- 错误信息照抄那种"告诉你下一步"的风格：「第 5 节正文已被外部修改（accepted≠current），请先 `studio accept 5` 重建摘要与伏笔状态」。

**验收**
1. 手动编辑 `chapters/ch02.md` 后 `studio run` → 拒跑，输出 `EXTERNAL_CHANGE` + 具体章节与两个 revision。
2. `studio accept 2` 后 `meta.chapters_state.summaries["2"]` 被重算（旧摘要作废）。
3. 中断 `accept` 在 rebuild 阶段 → `state.json` 记录 `failed` + 阶段名，`studio accept --resume` 能继续。

---

### B12 任务存储：status × phase × retryable × 结果引用

**优先级** P1 ｜ **工作量** M（新增约 260 行 + `runlog` 桥接）

**openwrite 做法**
- `TaskStore` 一个任务一个 YAML 快照 + 追加式事件（`OWC/tools/task_store.py:155-306`）。
- 状态机 7 态：`pending/running/awaiting_confirmation/completed/failed/cancelled/interrupted`；阶段 7 段：`queued/reading/preparing/model/validating/committing/complete`（`:22-39`）。
- 读时归一化：旧记录（v0）不拒绝，缺字段用**诚实默认值**填（`retryable = status in {failed, interrupted}`，`phase` 只有在 `completed→complete` 时才推导，其余留 `None` 而不是编造）（`:57-120`，注释 `:48-50` 说明为何只推导这一个）。
- 结果引用：按 task 类型派生 `result_ref`（`{type, id, path}`），`chapter_write` 指向章节、`chapter_review` 指向评审、`model_benchmark` 指向 artifact（`:123-150`）。
- 长操作只暴露 `task_id` + `phase`，不做同步阻塞（`DESIGN.md:165-170` 解释了为什么同步端点必翻车）。

**为什么好**
- 「跑到哪一步了」是长任务的第一个问题；`phase` 比 `running` 精确得多，且能给 UI/日志一致的进度语义。
- `retryable/interrupted` 是崩溃恢复的判定依据：短篇现在杀进程后只能靠人看日志猜。
- 读时归一化让旧数据可用但**不伪造**，这是很好的数据演进策略。

**短篇落地**
- 新增 `studio/tasks.py`（约 260 行，JSON 文件 + 追加事件 `data/tasks/tsk_*.json`、`events.jsonl`）：
  - `TaskStore.create(type, chapter_id, input_summary)` / `transition(task_id, status, phase, event)` / `load` / `list(statuses)` / `cancel` / `retry`；
  - 复用短篇 7 态中的 6 个（去掉 `awaiting_confirmation` 或用它表示"等人确认淘汰"）；
  - `result_ref` 按类型派生：`run` → `projects/<name>`、`write` → `chapters/chNN.md`、`polish` → `final/成稿.md`、`deconstruct` → `kb` 单元数。
- 桥接：`SP/studio/runlog.py:126 stage()` 与 `:99 event()` 已经有阶段上下文 → `tasks.transition(phase=...)`；`cmd_run/cmd_continue/cmd_produce` 第一行 create，末行 complete/failed；`KeyboardInterrupt` → `interrupted` + `retryable=true`。
- CLI：`studio task list|get|retry|cancel`；看板"任务"区直接读 `data/tasks/`（现有 runlog 列表可保留为原始日志入口）。

**验收**
1. 模型调用中途 `taskkill /F` → `studio task list` 显示 `interrupted`、`phase=model`、`attempt=1`、`retryable=true`。
2. `studio task retry tsk_xxx` 从阶段跳过已完成 stage 续跑（复用 `pipeline` 现有 stage 跳过）。
3. 旧任务文件（手工删掉 `phase` 字段）仍可 `list`，`phase=null` 而不是猜一个。

---

### B13 工作流状态机持久化：阶段、时间戳、失败原因

**优先级** P1 ｜ **工作量** S（新增约 180 行 + 改 3 处）

**openwrite 做法**
- `WorkflowScheduler` 每章一个 `wf_<chapter>.yaml`，固定 6 阶段 `context_assembly/writing/review/user_confirm/styling/compression`（`OWC/tools/workflow_scheduler.py:27-34`）。
- 每阶段 `StageRecord{name, status(pending/running/completed/failed/skipped), started_at, completed_at, message, data}`（`:37-66`）；`start_stage/complete_stage/fail_stage/skip_stage` 四个转移都能持久化（`:196-274`）。
- 关键数据向顶层投影：`writing→draft_path`、`review→passed/errors/warnings`、`user_confirm→approved`、`styling→polished_path`（`:226-237`）。
- 可读摘要与"未完成任务枚举"：`get_status_summary` 用图标渲染（`:278-303`）、`list_active_workflows()` 扫 `wf_*.yaml` 过滤未完成（`:315-324`）；`is_waiting_for_user()` 判断卡在确认（`:311-313`）。

**为什么好**
- 重启后能回答三个问题：卡在哪个阶段、为什么失败（`message`）、上一步产物在哪（`data`）。
- 「未完成工作流枚举」是无人值守调度的入口条件。

**短篇现状**
- `SP/studio/store.py:10` 只有 10 个粗粒度 stage 字符串，`SP/studio/store.py:36-39 set_stage` 只写 `status[stage]="detail"`——没有时间戳、没有失败原因、没有阶段内数据。

**短篇落地**
- 新增 `studio/workflow.py`（约 180 行）或扩展 `Project`：
  - `data/workflows/wf_run_{request_id}.json`，`StageRecord` 字段照抄（JSON 化）；
  - `SP/studio/pipeline.py` 每个 `stage_*` 函数开头/结尾 `start_stage/complete_stage`，异常 `fail_stage(e)`；`stage_quality_gate` 与 `word_gate` 这类循环用 `complete_stage(data={rounds, scores})` 记账；
  - `SP/studio/cli.py cmd_continue:352` 用 `list_active()` 判断"续跑哪个阶段"（现在靠 `project.has()` 猜）；
  - `studio status --project X` 打印 `get_status_summary` 风格表格。
- 与 B12 分工：B13 记「流程走到哪」，B12 记「这次进程怎么结束」；两者都用 `request_id` 关联。

**验收**
1. `studio status` 输出每阶段图标 + 耗时 + 失败原因。
2. 人为让 `stage_review` 抛错 → `wf.json` 里该 stage `failed` 且 `message` 含 error code。
3. `cmd_continue` 在 `workflow` 显示 `review` 未完成时不重跑 `write`。

---

### B14 门控纪律写进提示词 + CLI 提示（preview→confirm→apply）

**优先级** P1 ｜ **工作量** S（改 3 个 prompt + 1 个 CLI 提示）

**openwrite 做法**
- 人格里写死三类写入门控（`OWR/presets/openwrite/agent.cordis.yml:63-74`）：每个独立修改先 `document_change_plan(action="preview")` / `structured_change_plan(action="preview")`，展示 before/after 与来源版本，用户明确确认后**把原 preview_token 原样回传**再 `apply, confirm=true`；拒绝走 `reject`，源冲突走 `retry`，撤销走 `undo_preview_token`。
- 明确禁止绕过：「`novel_doc_write` 是已获明确授权的完整文档写入兼容入口，不得用它绕过逐项预览」（`:74`）；「普通讨论、分析和未确认建议不得写入资产」（`:77`）。
- 这些纪律还写进技能，形成"提示词 + 工具 + 技能"三重约束（如 `canon/SKILL.md:17`、`learn/SKILL.md` 的「普通讨论不得写入」）。

**为什么好**
- 纯提示词约束不可靠，但**没有提示词约束**时 agent 一定会顺手改文件。openwrite 的做法是：提示词说明流程 + 工具层用 token/confirm 强制 + 技能层重复提醒——三层冗余，成本低。
- 短篇是 CLI + 看板双入口，作者手动改稿很容易与自动流水线打架；需要一句清晰的"先预览再确认"。

**短篇落地**
- `SP/prompts/draft.md`、`SP/prompts/review.md`、`SP/prompts/improve.md` 各加一段固定「写入纪律」：
  1. 已有正文不得覆盖；改稿只返回 hunk（B10）；
  2. 未确认的讨论、建议不进 `project.json`/`bible.json`；
  3. 写章前必须先读 `outline` + `bible` + `canon`，缺项先补（B05）。
- CLI 侧给同等强度的提示（因为 CLI 才是真实入口）：`cmd_produce`/看板的删除、淘汰、覆盖动作要求 `--confirm` 参数并在输出里回显将改动的文件清单与 sha256 前后值（对应 openwrite 的 `CONFIRMATION_REQUIRED`）。
- 注意：**提示词不是强制手段**，真正兜底是 B05/B10/B11 的代码检查；这条只降低"agent 乱写"的概率。

**验收**
1. 三个 prompt 各含「写入纪律」小节（diff 可查）。
2. `studio produce --force` 无 `--confirm` 时拒绝，并打印将改动的文件+revision。

---

### B15 契约校验与「一条命令跑完所有检查」

**优先级** P1 ｜ **工作量** M（新增约 420 行 + `contracts/` 目录）

**openwrite 做法**
- 契约层：手写校验器 `OWC/tools/canonical_contracts.py:59-197`（`validate_review_v2` / `validate_delivery_v2` / `validate_model_profile_surface` / `validate_benchmark_v1` / `validate_review_manifest_v2` / `validate_canonical_artifacts`）。
- 机器可读 schema + 双向 linter：`contracts/*.schema.json` + `OWC/tools/schema_lint.py` + `OWR/packages/openwrite-bridge/scripts/schema-lint.mjs`，同一 fixture 矩阵在 Python 与 TS 两侧跑（`OWR/GOAL.md:643` 记录了这条演进的动机与结果：valid fixture + unknown-version/bad-enum/bad-range/bad-type/credential-leak 五类负例）。
- codegen 防漂移：`tools/schema_codegen.py` 从六份 schema 生成 Python+TS 类型与校验器，`--check` 报告产物是否 byte-identical（`OWR/GOAL.md:652`）。
- 一键门禁：`npm run check` = `check:plugin` + `test:contracts`（`OWR/package.json:18`），而 `check:plugin` 又串起 build/doctor/维护测试/预设测试/双 smoke/epochs/组件测试（`:21`）；契约侧 `test:contracts` = `contract-smoke.mjs && dog-canonical.mjs`（`OWR/packages/openwrite-bridge/package.json:24`）。
- **诚实说明**：任务书提到的 `check-current-skill-contracts` 脚本名在本地源码树中**未找到**；openwrite 实际的等价物是上面的 `npm run check` 组合与 `schema-lint`/`contract-smoke`。

**为什么好**
- 短篇的核心资产是 `project.json / outline.json / bible.json / reviews/chNN.json / report.json` 这一堆 JSON；没有契约，改一个字段就会让看板/复盘/发布某处静默失效（历史缺陷里就有这类：`SP/测试与优化报告_20261018.md` 记录过 `rep_core` 的 `maxed_langqa` 被合并覆盖导致漏报）。
- 「一条命令跑完」把质量门槛从"记得跑"变成"CI/提交前必然跑"。

**短篇落地**
- 新增 `contracts/`（5 份手写 JSON Schema 子集）：`report.schema.json`、`review.schema.json`（B07 的五轴）、`bible.schema.json`、`outline.schema.json`、`project.schema.json`。
- 新增 `scripts/check_contracts.py`（约 220 行，纯 stdlib 的 JSON Schema 子集：`type/required/enum/pattern/minItems/properties/additionalProperties`；不引 jsonschema）：
  - 用 `workspace/projects/*/` 的历史产物做 fixture 矩阵（正例 + 5 类负例：缺字段/错类型/坏枚举/坏 pattern/凭据泄漏 —— 凭据检查尤其重要，短篇 `config.yaml` 里就有真实 api_key，产物绝不能带出来）；
  - 从 Python 类型注释**不做** codegen（收益不抵成本），改为在 `studio/typeset.py`/`analytics.py` 读取处调用同一组 `validate_*` 函数，保证运行时与检查器同源。
- 新增 `studio/verify.py` + `python -m studio verify`（一条命令）：`pytest -q` → `check_contracts.py` → `studio check`（LLM 连通性，可选 `--offline` 跳过）→ 冒烟（MockLLM 跑通 `run` 到 `report.json`）→ 汇总表格与退出码。

**验收**
1. 手工把 `report.json` 的 `final_words` 改成字符串 → `verify` 失败并指出字段与期望类型。
2. 在产物里塞一个 `api_key` 字段 → 凭据检查失败。
3. `python -m studio verify --offline` 在无外网环境 60 秒内跑完并给出通过/失败汇总。

---

### B16 对话质量：确定性对白指纹 + 与"语气档案"对账

**优先级** P1 ｜ **工作量** M（新增约 260 行 + 1 处注入 + 报告字段）

**openwrite 做法**
- `DialogueFingerprintExtractor`（`OWC/tools/dialogue_fingerprint.py:37-227`）：按角色抽取对白 → 计算 `avg_sentence_length`（`:142-144`）、`question_ratio`（`:146-148`）、`common_bigrams`（中文二元组 Top10，`:164-175`）、`speech_patterns`（开头口头禅正则，`:177-197`），并 `to_prompt_text()` 供 prompt 使用（`:24`）。
- `dialoguequality` 技能把"AI 味对白"拆成可检查清单：过度礼貌、解释性对话、完美逻辑（每句都有正面回应、没有打断/沉默/敷衍）、套路表达（"受教了/所言极是""嘴角微微上扬"）、角色同声化（句长接近、用词层次相同）（`OWR/presets/openwrite/skills/dialoguequality/SKILL.md:71-99`），并给出判定顺序：先看指纹，再对照角色档案，最后放到整章上下文里（`:61-67, 164-168`）。

**为什么好**
- 「角色说话都一样」是最常见的 AI 味，也是最容易**量化**的：句长/问句率/口头禅/二元组这四个数就能拉开角色距离，不需要模型判断。
- 短篇的 `prompts/bible.md` 已经要求每个角色填 `speech_style`（句长习惯+口头禅+语言特征），且要求"遮住名字光看台词能认出是谁"——但这只是**声明**，从来没有验证过。

**短篇落地**
- 新增 `studio/dialogueqa.py`（约 260 行，纯 stdlib `re/statistics/collections`）：
  - `extract(lines, character)`：支持 `「…」`、`“…”`、`名字：`、`名字说：` 四种模式（参考 `_extract_character_dialogues` `:113-127`）；
  - `fingerprint(dialogues)`：四项指标（同 openwrite 算法，含中文二元组与开头口头禅）；
  - `check_voice_collision(fps)`：两角色句长差 <15%、问句率差 <0.1、二元组交集 >50% → `DIALOGUE_VOICE_COLLISION`；
  - `check_declared(project, fps)`：把实测指纹与 `bible.characters[].speech_style` 的声明值对比（从声明里确定性解析"句长/口头禅"关键字，解析不出就跳过），不一致 → `DIALOGUE_DECLARATION_DRIFT`；
  - `check_ai_tells(text)`：确定性项——礼貌词密度（"请/劳烦/阁下"在熟人对话中的出现次数）、套路动作标签（维护一个小词表，来自 `dialoguequality` 清单）、连续解释句（相邻两句都含专名 + 因果连词）。
- 接线：`SP/studio/langqa.py` 同级，`SP/studio/agents.py:_chapter_core`（`:321-345` 语言质检处）后增加一道**零 token** 对话门；报告写进 `reviews/chNN.json.dialogue`（fps + 告警）并注入主编 prompt（`to_prompt_text` 风格，几十 token）。

**验收**
1. 构造两个角色对白句长/问句率接近 → 报告出现 `DIALOGUE_VOICE_COLLISION` 并列出二人指纹。
2. `bible` 声明"短句、爱反问"，实测句长 40 字且问句率 0 → `DIALOGUE_DECLARATION_DRIFT`。
3. 该门不调用 LLM（用 MockLLM 跑一遍成本为 0）。

---

### B17 风格系统与写法记忆：可迁移/不可迁移分离 + 数值指纹

**优先级** P1 ｜ **工作量** S（新增约 150 行 + 1 个数据文件）

**openwrite 做法**
- 风格分三层且**不内置参考库**：`craft/*`（通用技法）→ `data/sources/{sid}/style/*`（从用户提供文本提取的 source pack）→ `data/novels/{id}/src/**`（本书确认版），合成到 `data/style/composed.md`（`OWR/presets/openwrite/skills/style-system/SKILL.md:13-26`）。
- 提取时强制二分：`reusable`（可迁移的节奏/叙述距离/对话密度/结构习惯）vs `source_bound`（专名、角色口癖、专属组织、签名梗、作品特定规则），**合成层只吸收 reusable**（`:89-99`）。
- 指纹模板给出可量化字段：段落长度分布（短/中/长占比）、对话占比、基调、人称、句法偏好、禁用表达（`OWR/presets/openwrite/skills/style-system/templates/fingerprint.yaml:10-61`）。
- 写法记忆（`learn`）的纪律：风格、成功钩子、作者偏好都是**作品资产**，不是会话本地文件，不是第二套 RAG（`OWR/presets/openwrite/skills/learn/SKILL.md`）。

**为什么好**
- 「风格」如果只写在 40KB 的 `style_guide.md` 里，模型每次只能模糊遵守，也无法校验；数值指纹让"像不像"变成可测量、可回归。
- reusable/source_bound 二分解决了拆书/借鉴场景的抄袭风险：只迁移句法节奏，不迁移专名与设定。

**短篇现状**
- `SP/data/style_guide.md` 40KB 文本 + `SP/studio/pipeline.py` 的 `load_style_guide` 整段注入；`SP/studio/pipeline.py` 的 `lessons_digest` 已经有写法记忆（`workspace/feedback_lessons.json`），但没有"可迁移/不可迁移"分类。

**短篇落地**
- 新增 `data/style_fingerprint.json`（工作区级或项目级）：
  ```json
  {"version":1,"target":{"para_ratio":{"short":0.35,"medium":0.45,"long":0.20},
   "dialogue_ratio":0.42,"avg_sentence_len":18,"question_ratio":0.12,
   "banned_phrases":[...],"required_elements":["每章至少一个反转"]},
   "measured":{"at":"2026-10-25","source":"final/成稿.md","para_ratio":{...}}}
  ```
- 新增 `studio/style.py`（约 150 行）：`measure(text)` 计算上述指标（纯 stdlib），`check(text, target, tol)` 返回偏差；`pipeline.stage_polish` 结束前把 `measured` 写回，偏差超阈值进 `report.json.style_drift`。
- `load_style_guide()` 注入时**追加一行紧凑指纹**（"短段 35% / 对话 42% / 平均句长 18 字"），比 40KB 全文更可执行。
- `lessons_digest`（`SP/studio/pipeline.py`）与 `stage_improve`（`SP/studio/pipeline.py`）写入的教训加 `kind: reusable|source_bound` 字段；`lessons_digest` 只注入 `reusable`（source_bound 留在复盘文档里，防污染下一次选题）。

**验收**
1. 生成一篇后 `data/style_fingerprint.json.measured` 有值，且 `report.json.style_drift` 指出偏差项。
2. 拆书/借鉴产出的 lessons 里 `source_bound` 条目不会出现在下一次 `run` 的 prompt 中（grep 关键专名验证）。
3. 注入 prompt 的 `STYLE_GUIDE` 长度下降 ≥80%，同时保留指纹行。

---

### B18 章组审计与漂移账本（可数证据 + 冻结裁决）

**优先级** P2 ｜ **工作量** L（新增约 420 行 + `improve` 接线）

**openwrite 做法**
- `ArcAuditService.audit(group)` 输出**全部可数**的章节组报表（`OWC/tools/arc_audit.py:111-270`）：逐章 `{promise, declared_state_change, promise_verified_in_prose, function, payoff_of, word_budget, actual_words, deviation, missing_fields, manuscript_status}`；聚合出字数计划/实际/偏差（`:230-239`）、完整性（`:240-243`）、失去推进的"连续 3 章"区段 `streaks`（`:204-215, 244-247`）、功能分布与连续段（`:248-254`）、伏笔 `net_new_debt`（`:255-262`）、漂移（`:263-269`）。
- 漂移账本是**作者裁决制**：`change()` 支持 `drift_entry`（需 chapter/kind/impact/note）、`reconcile`（absorb/recover/freeze，需 `confirm=true`）、`replan`（`OWC/tools/arc_audit.py:287-435`）；`DRIFT_KINDS = growth/escape/dilution/snowball`（`:23`）。
- 冻结用可数预算触发：超 `rewrite_budget` 自动 `frozen` + `reason="rewrite_budget_exceeded"`（`:344-349`）；冻结后写章被 `require_unfrozen` 拒绝（`:436-441`），并把账本内容作为写章指导 `chapter_guidance`（`:443+`）。
- 事务化落盘：先写事务文件再 `_commit`，中断时 `_recover_transaction` 能继续（`:54-110`，注释「Drift reconciliation was interrupted」）。

**为什么好**
- 它把"这一卷写得偏了"从主观感受变成 6 张表；`streaks`（连续 3 章没有状态变化）这种模式特别适合短篇的 10–14 节结构——短篇最常见的失败就是中段连续几节原地打转。
- 冻结/重写预算把"再改一次"的成本显式化，避免无限回炉烧钱。

**短篇落地（按短篇形态改造）**
- 新增 `studio/arc_audit.py`（约 380 行）：短篇没有"篇/节/章"三级，按**小节组**（起/承/转/合 四段，`SP/studio/pipeline.py` 的 `_chapter_ctx` 中 `arc_of_ch` 已有弧位推导）审计：
  - 逐节：`{no, title, goal, beats_count, word_target, actual_words, deviation, function(来自 genre_chain 的 beat 功能), foreshadow_obligations, resolved_foreshadows}`；
  - 聚合：字数偏差、`streaks`（连续 3 节无状态变化 / 连续 3 节同功能 / 连续虐章，复用 `SP/studio/pipeline.py` 的 `_chapter_ctx` 中连续虐章统计 已有的连续虐章统计）、伏笔净新增债务、承诺兑现（`outline.chapters[].goal` → 实测摘要覆盖率）；
  - `data/drift.json` + `studio drift entry|reconcile|freeze`（短篇的 freeze 语义 = "本篇不再自动重写，交人工"，比 openwrite 的预算冻结更简单）；
  - 审计只在 `stage_polish` 之后跑一次，输出进 `report.json.audit`，并作为 `stage_improve`（`SP/studio/pipeline.py` 的 `stage_improve`）的输入证据。
- CLI：`studio audit --project X`（表格输出，参照 `SP/studio/dashboard.py` 现有纯 CSS 表格能力）。

**验收**
1. 用历史 3 篇产物跑 `studio audit`，能报出至少 2 条真实问题（连续无状态变化 / 字数偏差 >25%）。
2. `drift entry` 后 `reconcile --decision freeze` 生效，`studio run` 对该篇拒绝自动重写。
3. 报告字段与 `report.json` schema（B15）一致。

---

### B19 评审 DAG 蓝图：一次定义、逐章实例化、版本化可追溯

**优先级** P2 ｜ **工作量** L（新增约 320 行 + 看板树渲染）

**openwrite 做法**
- 蓝图与实例分离：`review_dag_framework._build_framework()` 构造 47 节点固定拓扑（1 根 + 1 上下文 + 6 域 + 36 叶子 + 1 门禁 + 1 聚合），带 `topology_locked: true`、`selection_policy.mode="retain-topology"`（局部审稿也保留全部节点，未请求写 `inconclusive`）（`OWC/tools/review_dag_framework.py:54-211`）。
- 内容寻址：`revision = "sha256:" + sha256(canonical_json(REVISION_FIELDS))`（`:32-51`），每份 review 记录 `frameworkId/frameworkVersion/frameworkRevision`（`OWR/docs/REVIEW_DAG_FRAMEWORK.md:13`）。
- 强校验（`validate_review_dag_framework` `:224-332`）：schema 版本、rubric 与蓝图一致、单根树、无环、全部编号恰好映射一次、域名归属不漂移、invariants 与拓扑一致、revision 与内容一致。
- 实例化只绑定 `chapter_id` + artifact 目录（`instantiate_review_dag` `:335-369`），并校验 chapter 格式与相对路径安全。
- 四个扩展点（context-evidence / domain-evaluator / gate-evaluator / post-review-policy）规定"能扩展什么、不能改图"（`OWR/docs/REVIEW_DAG_FRAMEWORK.md:58-67`）。

**为什么好**
- 版本化蓝图让**历史评审可比**：两篇稿子评分不同时，能排除"评审规则本身变了"。
- `topology_locked` + 扩展点是最省事的多方协作设计：想换模型/提示词走扩展点，想改结构必须升版本。

**短篇落地（缩到 15 节点左右）**
- 新增 `studio/blueprint.py`（约 200 行）：从 B07 的 `studio/review.py` 的域/准则生成固定拓扑：
  `root → context（上下文完整性：字数/正典版本/伏笔义务是否注入） → 6 域 composite → 每域 2–3 准则 leaf → gate（敏感词/禁用道具） → aggregate`，共 ~15–18 节点；
- 新增 `studio/dag.py`（约 120 行）：`materialize(project, chapter_no, report)` 写 `data/reviews/dag/ch{no:03d}.json`（节点带 `target` 指向 `reviews/chNN.json` 的路径 + `status`），`validate_blueprint()` 直接照 `validate_review_dag_framework` 的 6 类断言移植；
- 看板：`SP/studio/dashboard.py` 用纯 CSS 渲染三层树（现有 dashboard 已有 conic-gradient/条形图经验，`SP/studio/dashboard.py:4` 自述），显示节点状态与证据摘要。
- **不要抄 47 节点/37 编号**（理由见第 3 节）；节点数按短篇准则量决定。

**验收**
1. `validate_blueprint()` 对构造的环/双父/漏映射 fixture 各报错。
2. 两次生产同一框架版本 → 两份 `ch*.json` 的 `framework_revision` 相同；改一个准则后 revision 变化。
3. 看板能展开到准则级并显示 evidence 引用。

---

### B20 操作 trace：只存摘要，不存正文与 prompt

**优先级** P2 ｜ **工作量** M（新增约 260 行 + 2 处接线）

**openwrite 做法**
- 隐私边界写在模块 docstring：trace 只存 digest 与 provenance，不存原始 prompt、模型输出、正文、凭据或思维链（`OWC/tools/operation_trace.py:1-6`）。
- 字段分类三个正则：敏感键（`api_key/authorization/bearer/credential/password/secret/token` → 记 `omitted` + `present`）、内容键（`content/text/prompt/guidance/replacement/before/after/excerpt/quote` 与任何 dict/list → 记 `sha256/utf8_bytes/rendered_units/raw_stored:false`）、安全键白名单（`action/chapter_id/id/kind/path/revision/target_words` 等 → 记 exact）（`:26-48, 79-102`）。
- 记录结构：`{trace_id, created_at, status, error_code, request{request_id, route, workspace/session/tool_call_id, arguments摘要}, context{packet_revision, source_revision, budget, compression}, model_calls[{prompt digest, usage, model, finish_reason, tool_calls摘要}], domain_change{...}, privacy{6 个 raw_stored:false}, retention{max_age_days:30, max_records:100}}`（`:276-342`）。
- 保留策略在写入时执行：`prune()` 按 30 天与 100 条双限删除（`:356-375`）；写文件用 mkstemp + fsync + replace（`:321-332`）。

**为什么好**
- 短篇现在的 `runlog` 是人读日志（`SP/studio/runlog.py`），缺少机器可查询的"这次到底调了什么/花了多少/改了哪些文件"的证据，复盘成本高。
- digest-only 是**可以带到生产**的做法：既能证明"跑过了"，又不至于把作者正文写进诊断文件。

**短篇落地**
- 新增 `studio/trace.py`（约 260 行）：`data/traces/trace_*.json`（放项目内，随项目移动），字段照 openwrite 精简：
  `{trace_id, request_id, created_at, status, error_code, run{cmd, args摘要}, model_calls[{role, model, prompt_sha256, completion_sha256, usage, elapsed_ms, attempt}], changes[{path, before_sha256, after_sha256, units}], privacy{...}, retention}`；
- 接线：`SP/studio/llm.py` 的 `chat/chat_messages`（`:169,178`）调用处记一次 model_call；`SP/studio/store.py`/`workbench.py` 的写点记 changes；
- 保留：30 天 / 200 条双限；CLI `studio trace list|show <id>`。

**验收**
1. 任何 trace 文件里不存在长度 >200 的连续正文字符串（写一个断言扫全部 trace）。
2. `trace.model_calls` 条数与 `llm.usage_summary()` 的 calls 总数一致。
3. 写 201 条 trace → 最老的被 `prune` 删除。

---

### B21 多模型横评 + 本地价目表自动选模（auto:cheapest）

**优先级** P2 ｜ **工作量** M（新增约 320 行 + 价目表）

**openwrite 做法**
- 自动选模是**显式前缀 + 策略词**：`auto:cheapest` / `auto:popular`（`OWC/tools/model_profiles.py:47-48`），未知策略抛 `INVALID_AUTO_SELECT_STRATEGY`，`auto:popular` 仅支持 OpenRouter 否则 `AUTO_SELECT_UNSUPPORTED`（`:730-756`）。
- `cheapest` 的选择是**带硬约束的比价**（`:758-801`）：排除非文本/非 `->text`、上下文长度不足、`max_completion` 不足、负价（动态定价）、零价（免费模型不稳定）的候选，按 `prompt_price+completion_price` 取最小；无候选 → `MODEL_CATALOG_EMPTY`。
- 目录有 1 小时磁盘缓存（`:803-829`），避免每次选模都打 provider。
- 横评的关键设计是**隔离**：每个候选与重复轮次有独立的完整工作区副本，正文只提交到候选沙箱，不覆盖正典；artifact 记录 execution_mode/上下文 hash/prompt 版本/字数/finish_reason/token/延迟/成本/错误；provider 失败算 reliability failure，不产生伪造的低质量分（`DESIGN.md:280-301`）。

**为什么好**
- 短篇有 5 个角色模型路由（`SP/studio/llm.py` + `SP/config.yaml:137-189`），但换模型只能靠人肉试；横评把"哪个模型写得好/便宜/稳"变成可重复实验。
- 成本字段与正确性分离（失败不算低分）是评测系统最容易做错的地方，openwrite 写对了。

**短篇落地（无外网版）**
- 新增 `data/model_prices.json`（**手工维护**，不联网抓）：`{"models": {"qwen3.8-flash": {"prompt_per_1m": 0.3, "completion_per_1m": 1.2, "context": 128000, "max_completion": 16384, "text": true}, ...}}`；
- `studio/modelroute.py`（约 120 行）：`resolve("auto:cheapest", task="writer")` 按上面 openwrite 的 5 条硬约束过滤后取最小价，未知策略/空目录给稳定错误码；`config.yaml` 的 `llm_roles.writer.model` 可写 `auto:cheapest`。
- `studio/bench.py`（约 200 行）：`run(pitch, models, chapters=1, repeats=1)` → 每个候选复制项目到 `workspace/benchmarks/{run_id}/cand_{i}/`（短篇项目只有几百 KB，复制成本可接受），跑 `stage_write` 单章，收集 `{model, words, gate四轴, elapsed_ms, tokens, cost_usd, errors}` 写 `bench_{run_id}.json`；**绝不写 `workspace/projects/`**。
- CLI：`studio bench --models a,b,c --project X`。

**验收**
1. `auto:cheapest` 在价目表里选出最便宜的合格模型；把该模型 `context` 改小到不够 → 自动跳到次便宜。
2. 未知策略 `auto:fastest` → `INVALID_AUTO_SELECT_STRATEGY`。
3. 横评前后 `projects/X/` 的文件 sha256 全部不变。

---

### B22 上下文预算与受保护项（防止压缩吃掉关键指令）

**优先级** P2 ｜ **工作量** M（新增约 220 行 + `_chapter_ctx` 改造）

**openwrite 做法**
- 受保护项是**显式清单**：`generation_protected_items` / `packet_protected_items` 从上下文里挑出不可静默压缩的部分（章节奏约束、写作目标、必守规则等），`protected_token_count` 计量、`ensure_protected_fits(items, budget)` 检查放得下、`assert_protected_unchanged(before, after)` 防压缩改动它们（`OWC/tools/context_protection.py:105-199`），放不下抛 `ContextBudgetError`（`:44-...`）。
- 预算计划：`ContextBudgetPolicy.plan(used_tokens)` 产出 `ContextBudgetPlan`，与 `input_budget_tokens()` 一起决定压缩策略（`OWC/tools/llm/context.py:99-...`）。
- 清单可核对：`build_context_manifest` 为每份来源记录 `{path, exists, revision, snippet}`，并做 `check_context_manifest_freshness`（文件变了 → 清单过期）（`OWC/tools/context_manifest.py:79-244`），预算/压缩/新鲜度都进 manifest 供 trace 引用（`operation_trace.summarize_context_packet` `:115-147`）。

**为什么好**
- 长上下文系统最常见的隐性事故：压缩把"必须写 3000 字""不许出现某词"这类硬指令删了，模型于是自由发挥，评审还判过。
- manifest 让"这次用了哪些来源、各自什么版本"可审计——配合 B20 的 trace 就是完整的可复现上下文快照。

**短篇落地**
- 新增 `studio/ctx.py`（约 220 行）：
  - 估算：中文 ~1 token / 1.6 字符（与 `SP/studio/llm.py` 现有 usage 估算口径一致），英文按空白词；
  - 分区预算：`BIBLE/OUTLINE/RAG/STYLE/PREV_SUMMARY/PREV_TAIL/KNOWLEDGE/GENRE_GUIDE` 各有上限，`PROTECTED = {"WRITE_SPEC","BAN_PROPS","GOAL","CH_TITLE","LANGUAGE_RULES"}`；
  - `assemble(parts, budget)` → 按优先级裁剪**非受保护**区，受保护项超预算直接抛 `CONTEXT_BUDGET_EXCEEDED`（对标 `ContextBudgetError`）；
  - `manifest(parts)` → `{section, path/source, sha256, bytes, est_tokens, dropped:bool}`，写进 `reports/context/chNN.json` 与 trace。
- 接线：`SP/studio/pipeline.py` 的 `_chapter_ctx` 末尾调用 `ctx.assemble`；`_chapter_ctx` 内 `kb`/`memory`/`rag` 逐级兜底 的 kb/memory/rag 逐级兜底保留，但每级产出进 manifest。

**验收**
1. 把 `max_tokens` 调小触发裁剪 → 只裁 RAG/KNOWLEDGE，受保护项完整，报告 `dropped` 列表正确。
2. 修改 `bible.json` 后旧 manifest 被标 `stale`（`check_context_manifest_freshness` 同款）。
3. 单测：受保护项超预算 → 抛 `CONTEXT_BUDGET_EXCEEDED` 而非静默截断。

---

### B23 i18n：只借"键位表 + 缺键测试"的约束，不借前端词典体系

**优先级** P2 ｜ **工作量** S（约 80 行，**建议暂缓**）

**openwrite 做法**
- 前端词典是**单一键位表**：`locales.ts` 定义 `NS = 'studio-panel'` 与 `StudioPanelKey` 联合类型（151KB 文件，键位是类型的一部分），两种语言的字典都必须覆盖同一键集合（`OWR/packages/studio-panel/src/client/locales.ts:1-7`）。
- 语言契约写在人格里：跟随作者当前对话语言回复，但保留既有稿件/大纲/资产语言；中文技能文档不代表要中文输出（`OWR/presets/openwrite/agent.cordis.yml:17-25`；每个 SKILL.md 开头都有同一句 `Language:` 声明）。

**为什么好**
- 键位类型化 = 缺翻译在编译期就暴露，不会出现"某个菜单项英文界面里显示中文"。
- 语言契约放在人格而不是散落在每个提示里，减少重复与漂移。

**短篇落地（不建议现在做）**
- 短篇是**中文单语**产品（读者、平台、prompt 全部中文），双语的收益接近零，成本却要动 `dashboard.py` 的内联 HTML/JS、CLI 文案、prompt 输出。建议：**暂不落地**。
- 如果将来要开源：新增 `studio/i18n.py`（约 80 行）——`TEXT = {"run.busy": ("项目正在运行", "Project is busy"), ...}` + `t(key)` + 单测断言两种语言键集合相等；只在 CLI 输出与看板文案层使用，prompt 保持中文（模型输出语言由 `LANGUAGE_RULES` 控制）。

**验收**（若落地）
1. 单测断言两语言键集合完全一致。
2. 缺键回退返回键名并打 warning（不崩）。

---

### B24 流式长文本、取消传播与 429 分级退避

**优先级** P2 ｜ **工作量** S（约 120 行增量）

**openwrite 做法**
- 流式开关统一成环境变量 `OW_AGENT_STREAM`（默认开），长正文与结构化生成共用（`OWC/tools/llm/__init__.py:25-27`），流式失败可降级同步（模块 docstring 第 2 条）。
- 取消是**请求级 ContextVar**：`model_cancellation(check)` 绑定一个 `() -> bool`，`check_model_cancellation()` 在每次调度模型请求前调用，取消则抛 `RuntimeError("MODEL_CANCELLED: ...")`（`OWC/tools/llm/cancellation.py`）。这样取消能"尽快但不粗暴"地停在下一次调用前，而不是杀线程。
- 错误分类型：`RateLimitError(PROVIDER_RATE_LIMIT)` / `LLMTimeoutError(PROVIDER_TIMEOUT)` / `NetworkError` / `ContextLengthError` / `StreamError`（`OWC/tools/llm/errors.py:37-...`），上层按类型决定重试或降级。
- 客户端流式解析带超时与进度回调：`_stream_timeout()` / `_stream_response(stream, on_progress)`（`OWC/tools/llm/client.py:532, 937`）。

**为什么好**
- 流式 + 取消 = 长任务可中断且可保稿（配合 B03），也是看板"停止生成"按钮能真实生效的前提。
- 把 429/超时/网络分开，才能实现"429 多等一会、超时换模型、网络重连"的差异化策略。

**短篇现状**
- 已有不错的基础：`SP/studio/llm.py:255-266` 流式与降级、`:318-347` 429/连接错误分级退避并读 `Retry-After`（`SP/studio/llm.py:327`）。缺的是：错误码（B04）、取消机制、取消后的保稿（B03）。

**短篇落地**
- `studio/llm.py`：加 `cancel_event`（`threading.Event` 或检查 `data/cancel/{run_id}` 文件存在）；在 `chat/chat_messages` 入口检查并抛 `StudioError(code="CANCELLED")`；流式循环里每累积 N 字符检查一次。
- 看板「停止」按钮：写 `data/cancel/<request_id>` 文件，`SP/studio/llm.py` 轮询到即取消；`pipeline` 捕获 `CANCELLED` 后调 `drafts.save_draft`（B03）再退出，任务状态置 `cancelled`（B12）。
- 把 `stream` 的自动判定（`SP/studio/llm.py:169-175`）抽成 `LLM_STREAM` 环境变量 + 配置双开关，便于故障时一键降级。

**验收**
1. 生成中点「停止」→ 2 秒内停止发起新请求，草稿可恢复，任务 `cancelled`。
2. 429 响应带 `Retry-After: 30` → 日志记录等待秒数并遵守。
3. 流式中途断连 → 自动降级一次非流式重试（现有行为）并记录 `PROVIDER_STREAM_ERROR`。

---

## 3. 明确不要借鉴的部分

> 判断标准：① 依赖 dsh 宿主或 Node 工具链；② 只服务 OpenWrite 自己的产品形态；③ 成本（磁盘/时间/维护面）远大于短篇单篇 9k 字的需求；④ 会破坏短篇"确定性流水线"的核心优势。

| # | 不抄的东西 | 证据 | 理由 |
|---|---|---|---|
| N1 | **studio-panel 前端整套**（React/Vditor/React Flow/ELK，~40 tsx + 150 个组件测试 + Playwright） | `OWR/packages/studio-panel/src/client/*`、`OWR/packages/studio-panel/scripts/components/*` | 短篇看板是纯标准库 `ThreadingHTTPServer` + 内联 HTML/CSS（`SP/studio/dashboard.py:4,18-20`）。引入前端＝引入 node/pnpm/构建链，直接违背"零依赖纯标准库"约束。**只借视图契约与轮询思路**，不借实现。 |
| N2 | **90 个 `novel_*` 工具 + HTTP 动作面 + managed runtime 安装**（cordis patch/whl/profile bundle/版本兼容矩阵） | `DESIGN.md:93-114`、`OWR/package.json:50-71`、`OWR/plugin.mjs` | 工具面存在的理由是"给 dsh agent 用"；短篇是确定性 CLI（`studio run/continue/produce`），已经有清晰的子命令分层，强行 action 分发器（`novel_source_action(action=...)`）只会让命令行变难读。发布体系（release/*.whl、safe-upgrade、plugin-doctor）短篇不需要。 |
| N3 | **dsh-dog（DoG 图/verifier/SDK 编排）** | `OWR/vendor/dsh-dog/`（SPEC 48KB、core 49KB、schema-0.2） | 需要第二个轮询进程 + Node 运行时 + 独立版本节奏。短篇要的是"证据可查"，用 JSON 报表 + 纯 Python 校验（B15/B19）即可达到同等可审计性，成本差一个数量级。 |
| N4 | **Chapter Run V2 全量阶段机 + benchmark framework 沙箱**（每候选复制整套作品工作区、run-scoped profile activation、LiteLLM 网关） | `DESIGN.md:280-301`、`OWC/tools/chapter_run_v2.py` | 这是为"长篇、多卷、多模型大规模横评"设计的。短篇 9k 字、单模型默认、横评只跑 1–3 个候选（B21 的精简版足够），全量阶段机的状态组合与调试成本远超收益。 |
| N5 | **内置 ReAct / orchestrator / tool_runtime 自研 agent 层** | `OWC/tools/agent/react.py`（92KB）、`orchestrator.py`（64KB）、`tool_runtime.py`（71KB） | 短篇的竞争力恰恰是"固定管线 + 确定性门禁 + 可复现成本"（`SP/studio/pipeline.py` 的 `stage_*` 线性结构）。引入自由 ReAct 会带来不可复现的输出、不可预测的 token 成本，且要维护工具 schema 演化——openwrite 自己在 dsh 集成里也**刻意不迁移**这一层（`DESIGN.md:34-36,66-67`）。 |
| N6 | **六域 37 项 + 47 节点固定拓扑原样照搬** | `OWC/tools/review_rubric.py:22-148`、`OWC/tools/review_dag_framework.py:54-211` | 37 项×每章一次 LLM 评审，对 9k 字短篇是 3–5 倍评审成本，且长篇小说专用的检查（战力崩坏、跨书规则一致性、番外伏笔隔离）在短篇里是空转。**借结构与语义（证据门槛/四轴/版本化拓扑），不借数量**。短篇应有自己的 5–6 域、~15 节点（B07/B19）。 |
| N7 | **在线模型目录自动比价**（`_model_catalog` 打 provider `/models`、`auto:popular` 走 OpenRouter 路由） | `OWC/tools/model_profiles.py:758-829` | 需要外网 + provider 特定语义（pricing 字段、`top_provider.max_completion_tokens`）；当前调研环境明确无外网，且短篇模型是 5 个固定角色配置。用**本地手工价目表**（B21）达到同样决策效果。 |
| N8 | **Workspace 多租户上下文**（6 个 `X-OpenWrite-Workspace-*` header、context epoch、会话绑定、跨请求 workspace 校验） | `OWR/packages/openwrite-bridge/src/client.ts:36-60, 163-181, 251-258`、`OWR/docs/WORKSPACE_CONTEXT_CONTRACT.md` | 单进程单工作区的短篇不需要多租户隔离；它真正的问题是**同一项目被两个本机进程同时写**，B01 的进程锁正好覆盖，不需要引入 ID/epoch/路由层。 |
| N9 | **双语 i18n 体系**（151KB `locales.ts` + 每个 SKILL 的语言契约 + 翻译指令） | `OWR/packages/studio-panel/src/client/locales.ts`、`OWR/presets/openwrite/agent.cordis.yml:17-25` | 中文单语产品，收益≈0。只借"键位表 + 缺键测试"（B23），且建议暂缓。 |
| N10 | **发布/升级/CI 全套**（wheels + constraints.lock + safe-upgrade + maintenance + release-provenance + Playwright e2e + dsh 兼容矩阵） | `OWR/scripts/safe-upgrade.mjs`、`release/`、`.github/workflows/*`、`OWR/package.json:21` | 短篇不是分发包，没有 dsh 版本兼容矩阵要维护。**只借"一条命令跑完所有检查"这个形式**（B15 的 `studio verify`），不借发布体系。 |
| N11 | **工具效应三档 + plan-mode/沙箱策略集成** | `OWR/packages/openwrite-bridge/src/tool-policy.ts` | 这是给"LLM 自主调用工具"做权限收口用的；短篇是程序调用程序（CLI 参数即意图），没有需要拦截的自主工具调用。B14 的 CLI `--confirm` 语义更贴合。 |

**一句话总结**：openwrite 的领域深度（评审/伏笔/正典/接纳）值得抄；它的**运行时形态**（dsh 插件 + 前端工作台 + 自研 agent + 发布体系）不值得抄，因为它解决的是"给 Agent 一个小说后端"，而短篇要解决的是"给作者一条可靠的短篇生产线"。

---

## 4. 落地路线图（三批）

**约束**：纯标准库（`requests`/`pyyaml` 已存在，`numpy` 仅 kb 已有）；不新增第三方依赖；每个新模块单文件可独立测试；所有新数据文件用 JSON（短篇现有资产是 `data/*.json`，不引 YAML）。

### 批次 1（P0，约 1.5–2 天，新增 4 个模块 / 改 8 个文件）

| 顺序 | 任务 | 文件 | 改动量 | 依赖 |
|---|---|---|---|---|
| 1.1 | 原子写工具 | `studio/util.py`（+25） | S | — |
| 1.2 | 错误封套与码表 | 新增 `studio/errors.py`（~90） | S | — |
| 1.3 | `store.py` 全部写点原子化 + `write_chapter` 封装 | `studio/store.py`（改 ~30） | S | 1.1 |
| 1.4 | 跨进程项目锁 | 新增 `studio/lock.py`（~130），`cli.py`/`dashboard.py`/`scheduler.py`/`workbench.py` 接线（~40） | M | 1.2 |
| 1.5 | 断点保稿与草稿发现 | 新增 `studio/drafts.py`（~180），`agents.py`（~25）/`pipeline.py`（~15）/`cli.py`（~40） | M | 1.1,1.4 |
| 1.6 | 写入前就绪预检 | 新增 `studio/precheck.py`（~110），CLI/看板各 1 处 | S | 1.2 |
| 1.7 | 门控纪律入 prompt + `--confirm` | `prompts/draft.md`、`review.md`、`improve.md`；`cli.py cmd_produce` | S | — |
| 1.8 | P0 测试 | 新增 `tests/test_lock_atomic_drafts.py`（~220） | S | 全部 |

**批次 1 验收门槛**（必须全绿才进批次 2）
1. 并发双跑 → 第二个 `PROJECT_BUSY`；杀进程后锁自动回收。
2. 强杀在任何时刻 → `project.json` 可解析、无半截章节文件、`.drafts/` 有可恢复稿。
3. `python -m studio run` 的失败输出都带 `code` + `request_id`。
4. `python -m pytest -q tests/test_lock_atomic_drafts.py` 全绿。

### 批次 2（P1，约 4–6 天，新增 11 个模块 / 改 6 个文件）

| 顺序 | 任务 | 文件 | 改动量 |
|---|---|---|---|
| 2.1 | 评审证据结构与五轴聚合 | 新增 `studio/review.py`（~350）；改 `agents.py`（~40）、`pipeline.py` 的 `stage_write`（`reviews/` 落盘）、`prompts/review.md` | M |
| 2.2 | 字数门保稿 + 定点重试 | 改 `agents.py:276-294`（~80）、`cli.py:707-726`（~20）、`reviews/chNN.json` 字段 | M |
| 2.3 | 伏笔 DAG + 章节义务 | 新增 `studio/foreshadow.py`（~300）；改 `pipeline._chapter_ctx`（~20）、`agents.py`（~10）；迁移脚本 | M |
| 2.4 | 正典/真值与快照 | 新增 `studio/canon.py`（~240）；`pipeline.stage_write`/`stage_polish` 各 1 处 | M |
| 2.5 | 修订提案与 hunk 应用 | 新增 `studio/revision.py`（~300）+ 抽 `studio/patch.py`（~60）；CLI 3 个新子命令 | M |
| 2.6 | 接纳事务（精简 3 阶段） | 新增 `studio/acceptance.py`（~380）；拦 `workbench.py`/`dashboard.py`/`pipeline.py` 三个写点 | L |
| 2.7 | 任务存储 + 工作流阶段机 | 新增 `studio/tasks.py`（~260）、`studio/workflow.py`（~180）；桥接 `runlog.py` | M |
| 2.8 | 对话质量指纹 | 新增 `studio/dialogueqa.py`（~260）；`agents._chapter_core` 插一道零 token 门 | M |
| 2.9 | 风格指纹 + lessons 分类 | 新增 `data/style_fingerprint.json`、`studio/style.py`（~150）；改 `lessons_digest`/`stage_improve` | S |
| 2.10 | 契约校验 + `studio verify` | 新增 `contracts/*.schema.json`（5 份）、`scripts/check_contracts.py`（~220）、`studio/verify.py`（~120） | M |
| 2.11 | P1 测试 | `tests/test_review_v2.py`、`test_foreshadow.py`、`test_revision.py`、`test_acceptance.py`、`test_contracts.py`（~600） | M |

**批次 2 验收门槛**
1. `python -m studio verify --offline` 一条命令全绿（含契约检查与 MockLLM 冒烟）。
2. 用 3 篇历史素材回放：评审报告有 `quality_score/coverage/gate_status/delivery_status`，无证据条目不得分。
3. 手动改稿后 `run` 拒跑（`EXTERNAL_CHANGE`），`accept` 后继续。
4. 伏笔超期、对话同声化、字数保稿三类问题各能复现一次。

### 批次 3（P2，约 5–8 天，新增 7 个模块）

| 顺序 | 任务 | 文件 | 改动量 |
|---|---|---|---|
| 3.1 | 评审 DAG 蓝图与物化 | 新增 `studio/blueprint.py`（~200）、`studio/dag.py`（~120）；看板树渲染（~150） | L |
| 3.2 | 章组审计与漂移账本 | 新增 `studio/arc_audit.py`（~380）+ `studio drift` 子命令；接 `stage_improve` | L |
| 3.3 | 操作 trace | 新增 `studio/trace.py`（~260）；`llm.py`/`store.py` 各 2 处 | M |
| 3.4 | 多模型横评 + 本地价目选模 | 新增 `data/model_prices.json`、`studio/modelroute.py`（~120）、`studio/bench.py`（~200） | M |
| 3.5 | 上下文预算与 manifest | 新增 `studio/ctx.py`（~220）；改 `pipeline._chapter_ctx`（~30） | M |
| 3.6 | 流式/取消/限流增量 | 改 `studio/llm.py`（~120）、看板停止按钮（~30） | S |
| 3.7 | i18n（可选，建议不做） | `studio/i18n.py`（~80） | S |

**批次 3 验收门槛**
1. 两份评审的 `framework_revision` 可比；改一处准则后 revision 变化。
2. `studio audit` 对 3 篇历史素材各报出 ≥1 条真实问题。
3. 横评不污染 `workspace/projects/`；trace 内无原文。
4. 上下文超预算时受保护项完整、`dropped` 可查。

### 排期建议与风险

- **不要在批次 1 之前做任何"大重构"**：锁/原子写/保稿是所有后续改造的地基，先做能立刻减少"跑一半崩了白跑"的损失。
- **批次 2 建议先做 2.1+2.2（评审与字数门）再做 2.3–2.6**：这两项直接决定"每篇稿子的质量下限与可救性"，收益立刻可见。
- **2.6 接纳事务是批次 2 里唯一可能超期的**：如果时间紧，先做「磁盘 SHA vs 记录 SHA 的只读检查 + 拒跑」，把三阶段重建留到下一轮。
- **不要引入 YAML/第三方向量库**：openwrite 用 YAML 是历史（`workflow_scheduler`、`foreshadowing_manager`），短篇已统一 JSON，混用只会增加解析面。
- **注意 `dashboard.py` 128KB 是单文件**：所有看板接线都在这一个文件里，改动易冲突，建议批次 1 就把它拆出 `studio/dashboard_views.py`（可选，避免多人并行时冲突）。

---

## 5. 附录

### A. 证据索引（按主题）

| 主题 | 主要证据 |
|---|---|
| 架构分层 | `OWR/DESIGN.md:29-38`、`:93-114`、`:116-128`、`:130-145`、`:158-178`；`OWR/docs/WORKFLOWS.md:17-74` |
| 锁与原子写 | `OWC/tools/project_lock.py`、`OWC/tools/chapter_pipeline.py:168-181`、`OWC/tools/file_ops.py:12-23`、`OWC/tools/operation_trace.py:322-332` |
| 保稿/恢复/取消 | `OWC/tools/chapter_pipeline.py:678-750`、`OWC/tools/chapter_lifecycle.py`、`OWC/tools/agent/writer.py:226-333` |
| 评审与证据 | `OWC/tools/review_rubric.py`、`OWC/tools/review_dag_framework.py`、`OWR/docs/REVIEW_DAG_FRAMEWORK.md`、`OWC/tools/canonical_contracts.py` |
| 伏笔/正典/真值 | `OWC/tools/foreshadowing_manager.py`、`OWC/tools/chapter_work_brief.py:462-595`、`OWC/tools/truth_manager.py`、`OWC/tools/arc_audit.py` |
| 修订/接纳 | `OWC/tools/revision_service.py`、`OWC/tools/manuscript_acceptance.py`、`OWR/presets/openwrite/skills/revise-span/SKILL.md` |
| 任务/工作流 | `OWC/tools/task_store.py`、`OWC/tools/workflow_scheduler.py`、`OWR/presets/openwrite/skills/workflow-manager/SKILL.md` |
| 错误码/trace/隐私 | `OWR/packages/openwrite-bridge/src/client.ts:17-22,286-313`、`OWC/tools/operation_trace.py`、`OWC/tools/studio_application.py:298-348` |
| 选模/横评 | `OWC/tools/model_profiles.py:700-829`、`DESIGN.md:280-301` |
| 上下文治理 | `OWC/tools/context_protection.py`、`OWC/tools/context_manifest.py`、`OWC/tools/progressive_compressor.py` |
| 风格/对话 | `OWR/presets/openwrite/skills/style-system/`（SKILL + templates/fingerprint.yaml）、`OWC/tools/dialogue_fingerprint.py`、`OWR/presets/openwrite/skills/dialoguequality/SKILL.md` |
| 门控纪律 | `OWR/presets/openwrite/agent.cordis.yml:61-91`、各 `SKILL.md` 的「不要另建存储」条款 |
| 验收体系 | `OWR/package.json:18,21`、`OWR/packages/openwrite-bridge/package.json:24`、`OWR/GOAL.md:643-652`、`OWC/tools/schema_lint.py` |

### B. 短篇侧对照证据（现状）

| 现状 | 证据 |
|---|---|
| 无跨进程锁；看板可 `Popen` 起第二个进程 | `SP/studio/dashboard.py:38`（`GEN_RUNNING` 仅进程内集合）、`:933-948` |
| `project.json`/产物非原子写 | `SP/studio/store.py:32-34,45-48,56-59`；`SP/studio/workbench.py:266-270` |
| 中断丢稿（只有全部门禁过了才落盘） | `SP/studio/pipeline.py` 的 `stage_write` 提交处／无门禁分支 |
| 字数门打满整篇淘汰 | `SP/studio/pipeline.py` 的 `word_gate`、`SP/studio/cli.py:707-730` |
| 评审 fail-open | `SP/studio/agents.py:92-96,122-128,198-204` |
| 伏笔只是一行行文本 | `SP/studio/pipeline.py` 的 `_chapter_ctx` 中 `foreshadow_brief`；`SP/prompts/bible.md` 的 `foreshadows` 结构 |
| 无正典快照 | `SP/studio/pipeline.py` 的 `stage_write`／`_stage_write_hybrid` 中 `chapters_state.summaries`（只有 summaries/tails） |
| 阶段状态粗粒度 | `SP/studio/store.py:10,36-39` |
| 已有基础（不要重造） | `SP/studio/langqa.py`（确定性语言质检）、`SP/studio/llm.py:318-347`（429/连接退避）、`SP/studio/runlog.py`（事件与 stage）、`SP/studio/pipeline.py` 的 `_rewrite_section`（定点扩写）、`SP/studio/util.py:95-104`（RateLimiter） |

### C. 调研方法与边界（诚实声明）

- **方法**：先 `glob`/`grep` 摸目录 → 按价值排序精读：`DESIGN.md`（全读）→ `OWC/tools/` 中的锁/流水线/评分/DAG/伏笔/任务/工作流/接纳/修订/trace/选模模块（精读关键函数）→ `presets/openwrite/skills/`（全部 24 个 SKILL 抽读，重点 9 个全读）→ bridge 的 `client.ts`/`tool-policy.ts`（全读）→ `docs/REVIEW_DAG_FRAMEWORK.md`/`WORKFLOWS.md`/`FRAMEWORK_ADVANTAGES.md`（抽读）→ 短篇侧对照精读 `agents.py`（全读）、`store.py`（全读）、`workbench.py`（抽读）、`pipeline.py`（按 grep 定位读）、`config.yaml`（全读）。
- **未通读**（结论不依赖它们）：`OWR/GOAL.md`（125KB，只抽读验证记录）、`OWR/CHANGELOG.md`（24KB）、`OWR/packages/openwrite-bridge/src/tools.ts`（156KB，只 grep 结构）、`OWR/packages/studio-panel/src/client/*`（只读 `locales.ts` 头部与若干测试名）、`OWR/docs/RELEASE_ACCEPTANCE.md`（43KB）、短篇 `dashboard.py`（128KB，只读关键 40 行与自述注释）。
- **未找到**：任务书提到的 `check-current-skill-contracts` 脚本在本地源码树中不存在（`grep` 无命中）；openwrite 的等价实践是 `npm run check` / `test:contracts`（见 B15 与 `OWR/package.json:18,21`）。
- **未验证**：所有结论均来自源码阅读，**没有运行 openwrite**（环境无外网、且它依赖 dsh 宿主与 Studio 进程）。因此文中"为什么好"是基于代码结构与注释的推理，不是实测性能/质量结论；每条落地项的"验收"给了可执行的最小验证方式。
- **版本**：`OWC/` 为 OpenWrite Core `5.8.12`（`site-packages/openwrite-5.8.12.dist-info`）；`OWR/` 为 `dsh-openwrite 0.2.25`（`OWR/package.json:3`）。
