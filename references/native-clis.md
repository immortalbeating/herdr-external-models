# 原生 CLI 参数与结果回收

调用 OMP、Claude、Codex 或 Agy 时按本页对应部分读取；先完成 [Herdr 会话](herdr.md) 的官方指引加载与目标确认。下述 `collect/recover` 适用于 `run.py` 建档的 job；普通官方 Pane 不自动具备这些关联。共用布局、补问与释放使用该参考，不重复初始化。Claude 可以协调任务，也可以被调用；向正在运行的其他任务发消息需有用户授权。

比较 CLI 的非交互能力、已实现 exec 路径或直接 API 时，读取 [能力与适配边界](direct-exec.md#能力与适配边界)。本页参数用于 Herdr 交互会话；`-p/--print` 等非交互参数应放入相应独立后端，不追加到本页的交互 spec。

## OMP / Claude 参数与权限

两者均使用 `mode: herdr`，提供明确的 `cli_args`、`permission_note` 和稳定任务 ID。`model` 记录调用意图，实际选择由 CLI 参数或配置决定。指定模型时保持字段与参数一致；明确沿用 CLI 默认时可用 `model: "cli-default"` 记录这一意图，并省略原生 `--model`。不能从旧用量推断默认模型。CLI 观察模型另记为 `cli_model_observed`。

OMP spec 示例（替换为已配置的 provider/model；写入任务另列 `write_scope`）：

```json
{
  "mode": "herdr",
  "workspace": "C:/path/project",
  "task_id": "task-identifier",
  "kind": "omp",
  "provider": "YourProvider",
  "model": "your-model-id",
  "cli_args": ["--model", "YourProvider/your-model-id", "--thinking", "high", "--tools", "read", "--approval-mode", "yolo"],
  "access": "read-only",
  "permission_note": "内置工具限 read，自动批准这项只读任务；另行检查 MCP/扩展。OMP 不提供操作系统文件沙箱。",
  "visible": false
}
```

`--tools` 限制内置工具，不代表关闭 MCP/扩展。Bash/写文件任务按范围选择工具与批准模式，记录真实措施。列表里有本次会话不可用的内置工具时，OMP 启动即退出（`Built-in tool unavailable in this session`）；Herdr 只报 `timed out waiting for agent startup`，`run` 会把可读取的面板末尾附在错误里并记 `agent_start_failed`。

部分内置工具还依赖模型或 judge 配置。遇到 find 等工具不可用时，先核对当前版本与配置；路径匹配可选择已确认可用的 glob，内容搜索可选择 grep。升级或更换渠道后重新核对工具资格。

spec 的 `--tools` 含 `find` 时，run/dry-run 提供配置依赖警告，不对所有模型或未来版本一律拒绝。警告不替代真实能力验证，也不自动更换工具或渠道。

按入口的能力验证规则检查本次实际使用的工具：只读任务覆盖读取与检索；执行任务增加创建、编辑和命令运行。工具名可用不等于本次确已调用。

测试会写用户数据时，隔离 APPDATA 并检查实际用户目录；`--add-dir` 不是 Windows ACL 授权。保持 Herdr integration 加载；用 `--no-extensions` 时通过 `-e` 显式加载 Herdr extension。

Claude 只读审阅 spec 示例：

```json
{
  "mode": "herdr",
  "workspace": "C:/path/project",
  "task_id": "task-identifier",
  "kind": "claude",
  "model": "用户指定的Claude模型ID",
  "cli_args": ["--model", "用户指定的Claude模型ID", "--effort", "high", "--permission-mode", "dontAsk", "--tools", "Read", "--strict-mcp-config", "--mcp-config", "{\"mcpServers\":{}}"],
  "access": "read-only",
  "permission_note": "内置工具限 Read，显式 MCP 配置为空并启用 strict；仍核对插件实际暴露工具。dontAsk 拒绝额外批准的操作，不提供操作系统文件隔离。",
  "visible": false
}
```

调用前核对安装版 `claude --help`，模型和 effort/权限选项以当前渠道与版本为准。沿用用户 Claude 配置时不自动追加覆盖参数，先核对实际权限再填写说明。`access` 本身不创建沙箱。认证与地址沿用 Claude 当前渠道，不把 OMP provider 配置传给 Claude。需要续接的会话保留原生记录；`--no-session-persistence` 不适合这一流程，`--fork-session` 会改变身份。恢复使用精确 ID/路径。

首次目录信任属于 CLI 初始化；遇到 blocked 先用被动 `read` 核对屏幕。处理已授权目录的初始化后，检查任务是否仍在输入草稿或已经进入原生记录；提交现有草稿或继续已有任务，避免再次 `run` 派发同一份工作。

Claude 首次派发还需同时看到明确的 `interactive_ready: true` 和有效的原生会话身份；TUI 的 idle 可能早于初始化完成。`run` 在同一 name/terminal/pane 上有界等待这两个信号，失败时留档且不提交任务，不自动重发；首轮前 JSONL 可能尚不存在，不以文件存在作为启动条件。

## OMP / Claude 按轮收集

```powershell
python "<skill>/scripts/run.py" status --job "<job>" --refresh
python "<skill>/scripts/run.py" collect --job "<job>"
python "<skill>/scripts/run.py" followup --job "<job>" --prompt-file "<followup.md>"
```

`collect` 定位 Herdr 上报的精确 JSONL：OMP 使用 session 路径；Claude 使用路径或在 `CLAUDE_CONFIG_DIR/projects`（默认 `~/.claude/projects`）按 UUID 查找。校验工作目录、原生身份与唯一性，不猜 latest。自定义配置根在 spec `env` 中保持一致；凭据沿用现有登录或环境。

收集要求 idle/done，用 `last_request` 文件名与 job ID 定位当前有效分支的本轮。只采用最终 assistant 的可见 text；thinking、工具输出、中间工具调用说明、错误/截断/拒绝及半写入尾行不作为结果。Claude 缺失 stop 标记的尾部文本，必须实时核实 idle/done；agent 退出不能代替这一条件。格式不明确时保留记录并报告无法收集。

OMP/Claude 默认最短长度为 1，`--min-chars` 可提高阈值。旧 job 缺少 `last_request` 且有补问时，用 `--request <本轮请求文件名>` 明确选择。`collect: none` 返回退出码 2，保留旧结果，不重发。成功写入 `result.md`、`result-receipt.json`，记录原生/message ID、用量、时长与 CLI 观察模型；不把它冒充 API 返回的 `model_reported` 或服务内部模型证明。检查产物与证据后再 `finish` 验收。

再次收集会先撤销旧 `result_current`，成功后才恢复；失败仍保留文件供追溯。Claude 的同 ID 记录若是直接父子相连的非可见思考块与唯一正文，且身份一致、明确正常终止并无错误，只取唯一正文。其他重复正文快照/不明确分块仍拒绝拼接；必要时核对原生记录，手工导出可见最终答案并用 `finish --result-file` 登记。

## OMP / Claude 重启后关联

前台断开通常保留进程；server 重启可能重建终端并用原生 `--resume` 恢复。旧 job 遇到 identity changed 时：

```powershell
python "<skill>/scripts/run.py" recover --job "<job>"
python "<skill>/scripts/run.py" recover --job "<job>" --apply
```

默认只预览。关联核对同一 CLI、工作目录、精确原生记录、唯一空闲候选和其他 job 权属，保留旧身份事件后更新 job/布局。它不启动 CLI、不重发任务。缺失、多解、忙碌或被其他 job 占用时停止；`--pane` 不能绕过这些检查。

Herdr 可能只重放 `--resume`。预览比较原 `cli_args` 与实际启动参数，并指出环境是否需复核。重要模型、思考与权限应持久配置，或用原参数明确重启同一原生会话。经检查确认当前运行配置符合已授权范围时，可记录：

```powershell
python "<skill>/scripts/run.py" recover --job "<job>" --apply --runtime-note "已核对启动参数、持久配置与任务环境，说明差异及范围仍适用的依据"
```

说明不授予新权限，也不恢复丢失参数。存在差异的进程不被 `reuse: true` 自动采用；显式关联后才继续该 job 的 `collect`/`followup`。不能确认权限或隔离路径时，按已有授权修正启动方式，不能只写一句确认来掩盖差异。

## Codex

Herdr 与 exec 都需明确模型、provider、effort 和权限；换成 TUI 不改变沙箱。生成参数、额外写目录及 Windows tempfile 权限问题见 [API 与一次性 CLI](direct-exec.md)。多个安装并存时分别记录实际可执行路径与版本，不用 Desktop 内置版本代替 npm CLI 版本。

Herdr 的 `collect` 默认最短 1 字。通过 hook 的精确 UUID 在当前 CODEX_HOME 下定位唯一 rollout JSONL，核对 session/cwd、最新原生 turn、job ID 与请求文件名。只采用显式 final 答案及同轮 task_complete 一致的正文；消息或工具尾流、错误、旧轮和不完整记录拒绝。回执记录原生 ID、turn/message 标识、模型与 effort。exec 使用 `--json` 事件和 `-o` 最终输出，不采集 Herdr 面板。

### 关闭后归档

Herdr Codex job 经辅助命令 `finish --close` 或 `close` 成功释放后，自动执行原生 `codex archive <绑定UUID>`；沿用该 job 的环境，核对会话目录、最新任务标记与归档后的日志。要求安装版支持 `codex archive`（本机 0.165.0 已验证）。原文保留在原生归档中，`job.json` 的 `codex_archive` 记录结果；采集可查活动及归档目录。多轮复审期间保留 Pane，`finish` 不带 `--close` 时不归档。其他 CLI、API/exec 不走此步骤。

归档失败时保留已经成功的面板关闭与验收，返回退出码 2；检查 `codex_archive.error` 后重试 `close --job <job>`，不会重关面板。尚未生成原生身份的启动失败记录标为 skipped。直接在 Herdr 界面手动关闭 Pane 不触发本脚本，需用同一 `close --job` 核实已释放并补归档；不扫描或批量改写其他会话。

## Agy

用安装版帮助核对模型、effort、mode 和权限参数；连接测试不覆盖用户默认模型或思考强度。模型列表失败不单独证明实际调用不可用，应检查真实输出。

Herdr 直接启动可执行文件，可能不执行 PowerShell 同名包装函数。若依赖其代理环境，从当前本机配置取得必要的非秘密值放入 spec `env`；这些值仅传给新工作区，不修改系统配置，也不硬编码其他机器的端口。

`plan + --sandbox` 不能直接声明为操作系统强制只读；`access: read-only` 是任务契约，实际强制边界需另行核验。只使用安装版帮助支持的工具限制参数，不照搬 OMP/Claude 工具列表或为单次审阅修改全局权限规则。

`collect` 刷新身份后，读取精确会话的 `~/.gemini/antigravity-cli/brain/<原生会话ID>/.system_generated/logs/transcript_full.jsonl`（`AGY_BRAIN_DIR` 可改根）。用 job ID 和 `last_request` 文件名定位 USER_INPUT，仅检查其后到下一输入前的区间。区间内最后 planner 须 DONE、有正文且无工具调用/错误，也无运行或错误尾部；GENERIC 工具结果和半行 JSONL 不作答案。该区间可能不是原生会话最后一轮。

默认最短 1 字，与其他原生 CLI 一致；需要长度门槛时显式 `--min-chars`，不足时返回 `answer_too_short`、实际长度和阈值。`--request` 不覆盖已记录的当前请求，旧 job 缺失轮次记录时才补选。失败撤销旧 `result_current`，保留旧文件追溯。计划文件只列路径；手工确认后可用 `finish --result-file` 登记，不按 mtime 或“最后一个 Markdown”自动替代本轮答案，也不替 Agy 按“Proceed”。

## Codex 图片恢复

Codex 原图因工具进程失败未复制时，可由宿主显式选取原图，保留源文件并记录 SHA-256：

```powershell
python "<skill>/scripts/run.py" collect-artifacts --job "<job>" --source "<CODEX_HOME>/generated_images/<精确会话UUID>/exec-<id>.png"
```

`--source` 可重复；只接受该 job 绑定会话目录中的明确 PNG，默认复制到 job 的 `artifacts/codex-images/<UUID>/`。同内容重复执行复用，目标内容不同时拒绝覆盖。需要复制到项目输出目录时，在 spec 用 `artifact_output_dirs` 列出获准绝对目录，再传 `--destination`；不把自然语言 `write_scope` 当路径解析，也不凭 SOURCE.txt、回答中的路径或目录里全部图片猜测本轮交付。此命令同时支持 Codex exec 与 Herdr，文本结果采集仍沿用各自路径。

## 释放与来源

验收后 `finish --close`。最后 Pane 关闭同时移除 Workspace 时，只把 Herdr 明确的 `workspace_not_found` 当作已不存在并补记关闭；连接、权限、身份错误仍拒绝。保留任务记录，不清理其他会话。

官方依据：[OMP 模型](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md)、[批准模式](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md)、[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[Claude 会话](https://code.claude.com/docs/en/sessions)、[Herdr integrations](https://herdr.dev/docs/integrations/)。参数和解析以已安装版本及实际验证为准。
