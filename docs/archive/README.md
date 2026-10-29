# 归档：已废弃的一次性脚本

这里存放**不再被任何代码引用**的历史脚本，只为可追溯性保留，请勿修改（改了也不会生效）。

| 文件 | 原位置 | 归档原因 |
| --- | --- | --- |
| `_produce_impl_2026-10-29.py` | 仓库根 | `cli.cmd_produce` 的早期实现草稿，与 `studio/pipeline.py` 的真实实现已经分叉，全仓零引用。保留根目录会让人误以为"要改这个文件"。 |
| `_rebuild_vectors_2026-10-29.py` | 仓库根 | 分片重建向量索引的运维脚本（每 10 批落盘、可中断续跑）。该能力已并入 `studio/kb.py:index_pending`（每 10 批自动落盘）并暴露为 `python -m studio kb rebuild --resume`，脚本本身不再需要。 |

## 相关运维命令（替代根目录脚本）

```bash
# 全量重建向量索引（清空后重建）
python -m studio kb rebuild

# 断点续跑（大库编码中断后接着跑，只补未编码单元）
python -m studio kb rebuild --resume

# 修复索引（去重重复行、按现存单元对齐 rows，不重新编码）
python -m studio kb repair
```
