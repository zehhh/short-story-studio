# 番茄小说草稿箱自动发布 + 自动排版方案

> 状态：已实现（排版引擎 + 草稿箱适配器）。真机联调需一次性安装 Playwright 并人工登录。

## 一、为什么是"草稿箱"而不是"全自动发布"

番茄作家助手（writer.changdunovel.com）**没有公开 API**。社区流传的接口方案靠登录 cookie
+ 未公开协议，随版本更新随时失效，且触发风控可能封号。因此本方案坚持两条原则：

1. **只自动化可逆动作**：建作品、填章节、存草稿——全部可以人工撤回；
   **「提交审核」永远留给作者本人点击**（不可逆动作 + 平台规则确认）。
2. **走官方 UI 而非逆向接口**：用 Playwright 驱动真实浏览器按人会点的方式操作，
   平台改版时只需修选择器，不影响其他模块。

## 二、自动排版引擎（studio/typeset.py）

成稿 `final/成稿.txt` → 番茄草稿箱规范稿，规则如下：

| 规则 | 处理 |
|---|---|
| 章节标题 | 统一「第01章 标题」；无标记成稿按 ≤2000 字自动切章 |
| 段落 | 段首去全角空格（移动端不显示缩进），段间空一行 |
| 标点 | 半角 `, . ! ? : ;` → 全角；省略号统一 `……`；破折号统一 `——` |
| 痕迹 | 清除 markdown（#/…/\`\`/链接语法）、行尾空白、连续空行 |
| 自检 | 超长段（>220 字）报告不自动切（保节奏）；单章 <300 字告警；敏感表述预检 |

产物：
- `submission/03_番茄排版稿.txt` —— 可整篇或逐章直接粘贴；
- `submission/04_排版自检.txt` —— 需人工决断的问题清单。

manual 投稿包模式也会自动生成这两份文件。

## 三、草稿箱适配器（studio/fanqie_draft.py）

### 前置（一次性）

```bash
pip install playwright
playwright install chromium
python -m studio.fanqie_draft login     # 弹出真实浏览器，人工登录后回车
```

登录态保存在 `workspace/fanqie_state.json`（约 30 天有效，脚本每次运行自动回写续期）。

### 使用

```bash
# CLI
studio publish --project <名> --mode fanqie_draft
# 或
python -m studio.fanqie_draft push <project_name>
```

流程：排版 → 打开作家后台 → 新建作品（书名/简介）→ 逐章「新建章节 → 粘贴 → 存草稿」
→ 完成后人工到后台检查并提交审核。

### 风控与合规设计

- **人工节奏**：章节间隔随机 3–7 秒；单次会话章节数上限可配
  （config.yaml → `publish.fanqie_draft.max_chapters_per_run`，默认 50）；
- **不碰发布按钮**：代码中不存在任何「提交审核」的点击路径；
- **登录态本地保存**：cookie/storage_state 只存 workspace，不上传、不入 git；
- **失败回落**：未安装 Playwright 时给出一键安装指引并回落 manual 投稿包，流程不中断。

## 四、流水线接入

- `studio publish` / `produce --publish-mode fanqie_draft` / 看板「生成小说」表单
  的发布模式下拉均已加入该选项；
- 发布记录写 `publish.json`（status=published, note=草稿箱结果），后续
  `analytics fetch/import` 照常关联阅读数据。

## 五、后续可选增强（未实现，预留）

1. 封面图自动化：接图册模板（Pillow 生成黑白线稿封面）后由适配器一并上传；
2. 多平台适配器：七猫作家助手（同样 UI 自动化思路）复用 typeset 输出；
3. 数据回流：草稿箱章节列表页定时截图比对，发现「审核失败」自动生成改进任务。
