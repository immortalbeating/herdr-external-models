# Herdr 向 DSH 委派任务

供 Codex、Claude、Agy 等调用者使用：先按 [Herdr 会话的官方指引与目标确认](herdr.md#读取官方指引)核对连接，再用 `scripts/dsh.py` 在无焦点 pane 中运行 DSH headless，保存结果与原生会话 ID。此适配通过 `pane run` 启动、`pane report-agent` 上报状态，不依赖原生 `agent start --kind dsh` 或社区插件。

## 能力边界

当前实现按需启动，使用 Python 标准库。布局按项目 workspace、任务 tab、DSH 对话 pane 组织；与原生 CLI 共用项目命名规则，DSH 的绑定记录独立保存在项目 `.agent-runs/dsh-layout.json`。每轮单独留档，同一对话续问复用已空闲、身份仍可确认的自有 pane，不为每轮创建一个 workspace。

`herdr agent prompt` 持续通信、统一取消、服务重启后自动重发 DSH 任务，以及 DSH 主动调用其他 agent 的专用工具均未实现。重启后通过进程对账识别中断，再由调用者明确续接原生对话。安装状态插件不等于补齐消息入口。进程对账当前支持 Windows；无法检查进程的平台或权限不足时保留为未知，不自动复用或清理。

## 前置条件

- 已运行的 Herdr server；命名会话用相同的 `--session`。Windows 升级客户端不会替换仍运行的旧 server；另起新版命名服务可保留现有会话。
- 安装版 DSH 具有已核实的 headless `--json`、`--session-id` 参数和兼容事件格式，且 Node 可用；版本升级按文末步骤复验。
- `herdr` DSH profile 已配置：以 `headless` 模板为基础，选择用户授权的 provider/model，保留其环境变量凭据引用。它不加载网页 UI 插件。`--profile` 可选择其他已配置的 headless profile。
- 调用者提供已授权的目标、范围和验收条件；辅助命令不会额外建立文件沙箱。写入任务在提示文件中声明可写范围并保护既有修改。

DSH headless 的凭据读取行为与 Web/Desktop 不同；按入口的能力验证规则检查实际模型请求与所需工具。

## 历史兼容性快照

2026-10-18 验证了 Herdr 0.9.5 与 DSH 0.2.1-rc.2 的任务调用、文件工具、原生续问和 `herdr-dsh` 启动命令。

这些是带日期的证据，不是当前安装状态或任务默认值。调用时以用户指定的 profile/模型、实际配置和原生记录为准；Web/Desktop 默认值不能证明 headless 的选择。示例 `default` 是 Herdr 服务名，`herdr` 是 DSH profile 名，按已授权目标调整。

## 提交与继续

任务正文写入 UTF-8 文件，不拼接到 shell 命令：

```powershell
python "<skill>/scripts/dsh.py" run --workspace "C:/worktree" --project-root "C:/project" --project-name "项目名称" --task-id "T-071" --task-name "T-071 特效验证" --prompt-file "C:/tasks/task.md" --session default --timeout 300
python "<skill>/scripts/dsh.py" status --job "C:/project/.agent-runs/dsh-<id>" --wait 30
python "<skill>/scripts/dsh.py" run --workspace "C:/worktree" --project-root "C:/project" --task-id "T-071" --prompt-file "C:/tasks/followup.md" --session default --session-id "session-<DSH原生ID>" --timeout 300
python "<skill>/scripts/dsh.py" finish --job "C:/project/.agent-runs/dsh-<id>" --review-file "C:/tasks/review.md" --outcome completed
```

本机若已安装 `herdr-dsh` 启动命令，可替代示例中的 `python "<skill>/scripts/dsh.py"`；先核对它指向本技能维护的版本。

第一次返回 JSON 中的 `dsh_session_id` 用于后续请求；保持相同工作目录。`--session` 是 Herdr 服务名，`--session-id` 是 DSH 对话 ID。补问仍启动新的 headless 进程，并在原 pane 中恢复对话；它不向已经退出的进程注入提示词。原进程未结束或存活状态不明时拒绝同一会话的新请求；等待超时不构成重新派发的理由。

`--project-root` 省略时使用执行目录的 Git 根（非 Git 为执行目录），不同 worktree 默认分开；仅在有意分组时指定共同项目根。`--task-id` 使用稳定任务编号，`--task-name` 为显示名称。省略任务编号的独立请求各有自己的标签；续问会沿用原记录的任务标识。仅复用面板不等于恢复对话历史，仍需传 `--session-id`。

Windows 可通过 `--herdr` 指定升级后的原生 `herdr.exe`；`--node` 指定原生 Node，`--dsh-bin` 指定全局 npm DSH 的 `lib/bin.js`。脚本不执行 `.cmd/.ps1` 包装器。它只在自己的子进程中把不支持的 SOCKS `ALL_PROXY` 换成已有 HTTP 代理，不修改系统环境。

## 结果与关闭

项目 `.agent-runs/dsh-<id>/` 保存 `job.json`、`prompt.md`、`events.jsonl`、`stderr.log`，成功后保存 `result.md`。旧记录仍可从执行目录读取。终端显示任务、阶段、工具名称和等待心跳；`progress.json` 保存最近进度，完整输出仍保存在任务记录内。显示进度不输出工具参数、原始提示词或思考正文。

成功必须同时具有 `turn_end.reason.kind=completed`、`final` 事件和零退出码。DSH 在错误后也可能发送空 `final`，单独看到它不算成功。

退出码：调用成功 0，失败、中断或状态不确定 1，仍在运行 2。`status` 核对 worker/DSH 的进程身份；确认旧进程已不存在才把活动状态改为 `interrupted`。PID 被其他进程复用不会被当成原任务存活。无法核对时保留未知；等待超时不会停止或重发请求。`dispatch_uncertain`、`monitor_lost` 和 `interrupted` 先查事件与恢复提示，避免重复请求。

DSH 回答结束与主模型验收分开。读取 `result.md` 和任务产物后，提供实际验收说明给 `finish --review-file ... --outcome completed|needs_work`；默认关闭该 job 仍拥有的空闲 pane/tab，保留全部记录。需继续补问时加 `--keep-pane`。较早轮次已被后继复用的面板不会被旧轮次关闭。仅想释放已结束面板时可用 `close --job`，它不等于项目验收通过。

`finish` 同时写本地 `finish-check.json`；任务结束可附重复的 `--related-job <已知job目录>`，仅核对这些记录，不扫描或操作其他会话。语义和退出码见 [轻量核对](records.md#轻量核对)。

### 重启与历史布局清理

```powershell
python "<skill>/scripts/dsh.py" prune --workspace "C:/worktree" --project-root "C:/project" --session default
python "<skill>/scripts/dsh.py" prune --workspace "C:/worktree" --project-root "C:/project" --session default --apply
```

先预览，再按用户已授权的清理范围执行 `--apply`。清理只处理确认已结束或中断、且仍属于记录的空闲布局；活进程、其他 agent、外来布局和检查失败均保留。恢复后 `terminal_id` 变化时，必须同时核对项目/任务或旧 job 的唯一标签、执行目录、当前进程及面板归属；不能只凭名称批量关闭。旧的无 PID 派发记录仅在启动宽限期后、未认领 worker 且自有面板可证实为空闲时判中断；其他无法确认的记录继续保留。

例行收尾使用 `finish`；`prune` 用于崩溃或遗留布局，不处理共享 workspace 内其他 CLI 的面板。

## 插件与通信方向

- 本辅助命令：其他 agent → Python 辅助命令 → Herdr CLI → pane 中的 DSH CLI；结果写入任务文件。
- `@dsh-blue/herdr-agent-state`：DSH → Herdr 本地 socket／Windows named pipe，上报状态；本身不接收新任务。
- `dsh-herdr-bridge`：DSH 内的工具 → Herdr CLI，调用其他 agent；方向与本需求相反。

无需为这条委派路径安装上述插件。辅助命令的状态上报也走 Herdr CLI；模型推理由 DSH 按现有 provider 配置完成。

## 后续升级

Herdr CLI/API 或 DSH 参数、JSON 事件变化都可能影响适配。优先沿用公开接口；升级后核对本页涉及的 `--help` 和事件字段，运行 `python -m unittest discover -s "<skill>/scripts" -p "test_dsh*.py"`，再在隔离目录验证一次文件读取及同会话续问。自动测试与模型实测分别记录，新版本不能仅凭旧版本通过就视为兼容。
