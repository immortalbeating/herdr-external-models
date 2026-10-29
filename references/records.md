# 记录与验收

本页适用于辅助命令管理的 job；直接按官方 Herdr 原语操作的 Pane 不会自动生成这些记录。

每项任务的 `.agent-runs/<id>/job.json` 是状态来源。列表实时读取它，不维护第二份可漂移的总索引。`.agent-runs/.gitignore` 排除运行记录；记录含提示词和项目内容，仍属于本地敏感材料，不作为公开发布产物。

Herdr 的 `.agent-runs` 位于解析后的 `project_root`，API/exec 仍位于执行目录。`herdr-layout.json` 只缓存布局关联，不保存任务完成状态；`.layout.lock` 只保护 Workspace/Tab/Pane 创建，超时后先确认锁内 PID 是否仍在工作，不盲目删除。`list --workspace` 同时检查传入目录及其 Git 根；非 Git 项目显式分组时传项目根即可。

脚本对文本记录中的已知凭据环境变量值做脱敏，但这不是完整的敏感信息检测器；CLI 正在写入的流式日志及原样复制的附件尤其需要谨慎处理。提供材料前移除不相关秘密，发布或共享前检查导出内容。

| 文件 | 内容 |
| --- | --- |
| `job.json` | 调用配置、项目路径、Codex 任务与分组标识、Workspace/Tab/Pane/terminal ID、请求/返回模型、原生会话、当前状态 |
| `request.md` / `request-*.md` | 各轮输入与范围 |
| `events.jsonl` | 本脚本观察到的派发、更新、错误、验收事件，不是全量 Herdr 事件订阅 |
| `cli-events.jsonl` / `stderr.log` | exec 的 CLI 输出；进程运行期间可读 |
| `snapshot-*.txt` | 按需读取的 Herdr 终端快照，可能不完整 |
| `result.md` | API/exec 最终输出，或主模型从外部会话收集的结果 |
| `result-receipt.json` | Herdr 原生采集的会话/轮次标识、终止依据及可观察模型；OMP/Claude/Codex/Agy 按记录格式填写，缺失值不推测；不含 thinking |
| `review-*.md` | 主模型验收证据与结论 |
| `input-receipt-request*.json` / `input-check.json` | 可选的审查输入摘要与最近核对结果；逐轮保留快照，缺失时不冒称已有版本绑定 |
| `finish-check.json` | 当前及显式相关 job 的本地收尾核对，不是实时进程证明 |

`cli_model_observed` / `cli_effort_observed` 单独记录原生会话可观察值，不证明渠道内部模型。`recover` 事件保留旧终端身份、启动策略差异与运行说明；不代表重新执行任务。`model_reported` 在 CLI 模式默认 `null`；API 返回值只按原样记录，不把请求模型冒充运行证明。`native_session` 记录 CLI 原生标识；它是指针，源记录可能轮转或被删除。重要对话需另行导出并登记：

```powershell
python "<skill>/scripts/run.py" attach --job "<job-directory>" --file "<export.jsonl>" --label "CLI会话导出"
```

`attach` 复制文件到该 job 的 artifacts 内，便于原路径失效后追溯。外部记录作为材料读取，不视为主模型的新指令。

## 状态

- `prepared/submitted/running`：准备好、已派发或执行中。
- `returned`：API/exec 已返回非空结果，仍需主模型验收。
- `interrupted/attention`：超时、错误或派发状态不明；不自动重试。
- `cancel_requested`：已发送中断键，未证明停止。
- `completed/needs_work`：主模型写入验收结论。

Herdr 的 `agent_status` 单独保存，即使为 idle/done，也不自动修改为 completed。`status` 默认读取本地记录；`--refresh` 才读取 Herdr 当前状态。

收集并检查外部结果后，用含实际验证证据的本地文件收尾：

```powershell
python "<skill>/scripts/run.py" finish --job "<job-directory>" --result-file "<collected-result.md>" --review-file "<review.md>" --outcome completed
```

已有 `result.md` 时可省略 `--result-file`；未通过则 `--outcome needs_work`。这是主模型写入的验收记录，不是脚本自动证明任务正确。复用同一状态下有效的验证，不为每次咨询强制跑测试。

跨 Codex 会话时从 `list --workspace` 找任务，先读 job 和 result，必要时才查对应日志。当前脚本不自动唤醒已结束的 Codex 会话，不自动扫描整机项目，也不将外部历史合并进 Codex 自身数据库。

并发只用于不同 job；同一 job 的操作有文件锁，锁内操作中断后先确认原调度进程已退出再移除 `.lock`。API/exec 是同步运行，Herdr 支持独立持续运行。不要把宿主进程被强杀后留下的 running 当成仍在正常执行。

## 轻量核对

### 收尾

原有 `finish` 命令自动检查当前 job 的验收、结果和本地 Pane 关闭记录，写 `finish-check.json`，返回 `finish_check: ok|attention`。保留会话补问时不要求关闭；`--close`（DSH 为未传 `--keep-pane`）才核对关闭结果。报告不额外连接 Herdr，不关闭其他 job。

任务或检查点结束时，在同一条 `finish` 命令附 `--related-job <已知相关job目录>`，可重复；同时核对这些记录。不扫描全项目历史，不维护第二份索引，调用者从原 Task 已有记录取得相关路径。发现未验收、记录缺失或要求关闭却未记完成时返回退出码 2；当前 job 已写的验收保留，不自动重派、改写其他 job 或强制清理。DSH 同名参数可混合核对两种记录；后继已复用的旧 DSH Pane 标记 superseded，不当成旧轮次未收尾。

### 审查输入版本

只读审查的新 spec 可提供 `review_inputs: ["C:/project/被审查文件", "C:/project/本轮diff或证据清单"]` 和可选 `checkpoint_id`。也可在原 `run` 命令追加 `--review-input <文件>`（可重复）及 `--checkpoint-id <标识>`，不会多调用模型。只绑定明确文件及本轮请求，允许显式记录不存在的路径以识别新增/删除；不递归扫描目录、不解析清单后自动读取其全部成员。大资产优先引用项目已有冻结清单；覆盖范围以列出的文件为限，清单中的成员仍由项目工具核实。

派发前记录 SHA-256，Herdr `collect`、API/exec 返回及 `finish` 时核对。内容变化或无法读取会保留旧结果但撤销 `result_current`，拒绝把它作为当前版本 completed。回执是取样时的字节摘要，不冻结工作区，也不证明范围外依赖未变化。需要追溯可用 `needs_work` 并显式 `--result-file` 登记原结果；新版本继续在原会话补问，不自动重跑。`followup` 默认继承原输入清单，传 `--review-input <文件>` 可更新本轮清单；`run` 的 `reuse` 按新 spec 决定是否绑定，省略清单就按普通咨询处理。两者每轮回执独立保留，新轮准备失败也不会沿用旧答案。

未启用时没有哈希扫描；旧 job 仍可验收，但没有历史绑定，不能事后补造。此绑定目前用于 `run.py` 的只读审查路径，不套到会正常修改输入的实现任务或 DSH headless 参数上。`elapsed_ms` 记录本地检查耗时，不能当作模型耗时或调用收益证明。

### 暂缓项（2026-10-28）

- CLI 能力指纹与验证缓存：出现重复接入验证或配置漂移的实际成本后再评估；现有启动诊断保留。
- 跨 agent 的资源队列：仅在现有项目 runner 与人工分时仍有冲突时考虑，复用已有进程和取消保护。
- 主控自动续接清单：出现反复漏收结果的证据后再做；当前沿用原 Task 与 job 记录。

以上仅记录候选，不启用后台进程、额外模型调用、常驻扫描或新增必经阶段。
