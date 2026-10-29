# Herdr 会话与任务记录

本页说明官方 Herdr 操作与本技能任务记录的衔接。DSH 的 worker、状态和释放使用 [DSH 委派](dsh.md)；四种原生 CLI 的参数与结果格式见 [原生 CLI](native-clis.md)。

## 读取官方指引

首次进入 Herdr 路径、当前上下文尚未读取指引，或 Herdr 升级后，执行 `herdr --version` 和 `herdr --skill`，实际读取后者的完整输出。它是本机版本配套的官方技能，无需另外安装或让用户调用第二个技能。仅贴网址、看到环境变量或安装 integration 均不等于已加载指引。

| 要处理的内容 | 权威来源 |
| --- | --- |
| Workspace/Tab/Pane、agent 启动与投递、等待/读取、键盘输入、integration | 本机 `herdr --skill`；参数不匹配时查相关 `--help` 或 `herdr api schema` |
| spec、项目/任务分组、job 身份、原生按轮采集、验收与释放记录 | 本技能本页及所链接参考 |
| 模型、工具和权限参数 | 所选 CLI 的当前帮助与配置；本技能记录真实选择 |

读取失败时，改读[官方技能说明](https://herdr.dev/docs/agent-skill/)及与 `herdr --version` 匹配的仓库技能；仍无法核实命令则停止依赖该命令的操作，不凭旧示例猜测。不要把整份官方手册复制进本技能，避免维护两份语法。

### 调用者与目标

- **Herdr 面板内**：核对 `HERDR_ENV=1`，遵守官方技能的当前面板、身份和焦点规则；通过实时返回值取得 ID。
- **Herdr 面板外**：官方技能的面板内流程在此停止。仅在用户已明确授权当前调用者控制指定 server/任务时，使用本技能辅助命令的显式目标连接（原生 CLI 用 `run.py`，DSH 用 `dsh.py`）；spec 或命令明确填写 `session`，已有 job 沿用其中的目标与身份。不要伪造 `HERDR_ENV`、借用 UI 当前焦点或猜测属于自己的会话。没有这项授权时先说明调用者不在 Herdr 内，继续不依赖控制会话的工作。
- 官方 Herdr 原语本身不创建本技能的 job。需要 `.agent-runs` 回执且项目任务分组已获授权时用对应辅助命令；普通布局检查不必建立空任务记录。

## 前置条件与显示

Server 必须已运行；脚本不自动启动服务或安装 integration。用 `herdr --session <name> status server` 确认目标，后续命令保持相同目标。客户端和仍在运行的 server 可能版本不同，不能只凭客户端升级推断功能可用。

- 前台观察由用户运行 `herdr --session <name>`。现有任务可用 `run.py show --job <job>` 请求打开外部窗口，返回 `launch_requested` 后仍需核对连接。
- 后台派发保持 `visible: false`。`visible: true` 会改变 Herdr 内布局焦点，不等于弹出终端窗口；后台也不隐藏工具调用记录。
- 用户要求新建 Windows 后台服务时，以解析后的 Herdr 可执行文件和明确 session 用 `Start-Process -WindowStyle Hidden` 启动 `server`，写日志并检查状态。实验使用独立命名服务，保留正在工作的服务。

### Windows 拒绝访问

先用 `run.py doctor --session <name>` 读取调用者和目标服务器的权限令牌。Medium 调用者连接 High 服务器时，按用户授权安排停止该服务器，再从普通终端重启；只关闭前台窗口可能留下后台服务器。权限等级相同则继续检查实际连接错误和日志。

不要删除 `.sock`、放宽 ACL 或自动提权来绕过连接错误；`.sock` 标记可读不代表 Windows 命名管道可连接。

## 创建任务

```json
{
  "mode": "herdr",
  "workspace": "C:/path/project",
  "project_name": "项目显示名称",
  "task_id": "稳定任务ID",
  "task_name": "当前协作任务名称",
  "role": "代码复核",
  "kind": "claude",
  "model": "用户指定的模型ID",
  "cli_args": ["--model", "用户指定的模型ID"],
  "access": "read-only",
  "permission_note": "填写实际核实的权限措施，并相应调整cli_args",
  "visible": false,
  "session": "已授权的Herdr服务名"
}
```

这是字段示例，尚未配置强制只读权限。`model` 只记录意图，生效选择在 `cli_args` 或 CLI 配置中。`kind: codex` 可省略 `cli_args`，按 `provider/model/access/effort` 生成参数，默认只读；其他 CLI 必须提供 `cli_args` 和非空 `permission_note`。自定义 Codex 参数也需权限说明，并自行包含模型与权限选择。

### 布局与复用

官方普通操作默认在调用者当前 Tab/CWD 创建同级 Pane。以下是本技能辅助命令的项目任务分组，适用于用户本次要求或已有明确项目约定授权的布局；只调用技能本身不新增这项授权。未授权分组时沿用官方默认，将结果保存到已授权项目文件；当前辅助命令不导入这些手动创建的 Pane，也不声称为它们生成了 job/原生回执。用户明确指定当前 Tab 或其他拓扑时遵从其要求，不借用 `run` 暗自另建布局。

- `workspace` 是实际执行目录；`project_root` 默认 Git checkout 根，非 Git 为执行目录。规范化路径生成项目标识，不同 worktree 默认分开；仅有意分组时指定共同根。
- 辅助命令只复用带本项目标识或已核实 terminal 绑定的 Workspace，不因 CWD 相同就接管主控所在的 Space。因此同一项目可显示“主控 Space + 代理 Space”；后续任务复用后者，在其中按任务建 Tab、按 CLI 会话建 Pane。这是布局归属与收尾边界，不提供文件或权限隔离。
- `project_name` 默认目录名。已知当前 Codex 任务真实 ID 时可传 `parent_thread_id`；也可单独用稳定 `task_id`。同一任务复用 Tab，两者都缺失时每次建新 Tab。`run --task-id/--task-name` 可覆盖共享 spec 的任务字段。
- 默认每次 `run` 新建 CLI Pane，同一任务交替向右/下分屏，`split_direction` 可指定；不占用用户 shell。显示名称可由 `agent_label` 覆盖，控制用唯一 alias 和实时 ID。
- spec 的 `reuse: true` 只考虑本任务最新未关闭 job：工作区、session、CLI、模型、权限、范围、环境及实际启动参数均相同，身份仍匹配且 idle/done、未被其他操作锁住时，将提示作为该 job 的 `followup`。不满足时新建；发送一旦开始，即使失败也不另开 Pane 重发。

映射在项目 `.agent-runs/herdr-layout.json`，按配置位置与 session 隔离。创建布局使用短时项目锁，模型执行不持锁。记录 `workspace_id/tab_id/pane_id/terminal_id` 并核对实时布局；重命名可保留关联，身份冲突则停止。关闭后重建布局不恢复旧任务，也不移动或关闭旧版布局。

长任务正文写入项目请求文件，确保 CLI 已获准读取该绝对路径。首次提交前，脚本等待同一 name/terminal/pane 明确 ready 且 idle/done；Claude 还须已有原生身份，Codex/Agy 允许从未绑定过的新会话首条输入后才分配 ID。初始化、信任对话或失败时先被动 `read`，处理现有草稿或任务；详见 [原生 CLI](native-clis.md)。

## 跟进与收集

```powershell
python "<skill>/scripts/run.py" status --job "<job>" --refresh
python "<skill>/scripts/run.py" read --job "<job>" --lines 120
python "<skill>/scripts/run.py" followup --job "<job>" --prompt-file "<followup.md>"
python "<skill>/scripts/run.py" collect --job "<job>"
python "<skill>/scripts/run.py" cancel --job "<job>"
```

`read` 默认被动读取 visible 并返回快照路径，长历史可选 recent/recent-unwrapped；完整答案优先用 [原生按轮采集](native-clis.md)。`followup` 只对匹配身份的 idle/done 会话发送。`cancel` 向匹配 agent 发 Ctrl+C，记录 `cancel_requested` 后仍须确认停止。

开放 Herdr job 的 `status` 默认实时核对，返回 `status_source: live` 和 `agent_observed_at`；`--refresh` 保留为显式写法。离线查记录用 `status --cached`。已关闭、未启动或非 Herdr job 只需读本地。实时 `agent_status` 与任务验收 `status` 分开：idle/done 不自动变为 completed。

### 等待

```powershell
python "<skill>/scripts/run.py" wait --job "<job>" --timeout 600 --collect
python "<skill>/scripts/run.py" wait --job "<job-A>" --job "<job-B>" --all --timeout 600 --interval 20 --collect
```

`wait` 默认在任一 job 变为 idle/done/closed 时返回，`--all` 等全部；它逐次核对同一任务和原生身份，轮询间不持 job 锁。`--collect` 还要求按当前请求采集到有效最终答案，避免等待一个尚未由 collect 生成的 `result.md`，也不会把旧文件误作本轮结果。默认每 20 秒检查，实际响应时间还包括查询与采集耗时；轮询不生成面板快照。

blocked/unknown、身份或请求变化、查询错误及时返回待处理；超时返回非零，保留正在执行的任务，不取消、不重发、不自动验收或关闭。等待中的变更输入留存选项与普通 collect 相同，见 [审查输入版本](records.md#审查输入版本)。DSH 用自己的 `status --wait`，不混入此命令。

超时不证明未投递；Herdr 的等待观察生命周期，不能证明特定任务或后台子任务已完成。`agent_prompt_stalled` 也表示提示可能已经投递：脚本保留请求并被动核对同一身份，观察到 working 可继续等；其他情况报告不确定，再用 wait/collect 核对本轮，绝不自动重发。通过 job/请求标记、原生最终答案与产物确认结果，验收格式见 [记录与验收](records.md)。

只读审查的输入绑定与 `finish --related-job` 收尾核对见 [轻量核对](records.md#轻量核对)；普通咨询保持原命令，不为核对再开模型或全量扫描。

## 释放与恢复

```powershell
python "<skill>/scripts/run.py" finish --job "<job>" --review-file "<review.md>" --outcome completed --close
python "<skill>/scripts/run.py" list --workspace "<project>" --open [--task-key "<task_key>"]
python "<skill>/scripts/run.py" close --job "<job>" [--job "<另一个>"]
python "<skill>/scripts/run.py" close --task-key "<task_key>" --workspace "<project>"
python "<skill>/scripts/run.py" prune --workspace "<project>"
```

- **验收**：`finish --close` 先记验收再释放。释放拒绝仍保留验收，返回 `close: refused` 和退出码 2；非 Herdr job 为 `close: no_pane`。仍需补问时保留 Pane，使用 `followup`。
- **身份与忙碌**：`close` 仅关闭该 job 仍拥有的 terminal/pane，原生身份存在时也核对。working/blocked 默认拒绝；`--force` 只用于用户已授权的中断。明确 agent_not_found 的自有 shell 可关闭，查询错误、被替换身份或其他活跃 agent 则拒绝。
- **布局与记录**：检查和关闭持项目布局锁，释放后重查，Tab 确认空才关闭并更新映射。失败不记为已关闭，重试可完成空 Tab 收尾；job、结果、原生 ID 保留。部分创建失败也保存已分配的布局 ID，供后续检查释放。
- **Codex 归档**：辅助关闭成功后归档绑定的原生会话；失败与重试见[关闭后归档](native-clis.md#关闭后归档)。
- **遗留检查**：`list --open` 实时核对记录。`pane_live: false` 可经 `close` 补记关闭；null 表示未查明。按 task 批量关闭独立处理各 job，有拒绝则退出码 2。`prune` 清理过期映射，并对同一服务中已确认 terminal 不存在的 run.py job 再核实、补记关闭；重新出现的 terminal、其他服务、身份或查询不明的记录保留。它不主动关闭仍存在的 Pane，Codex 补记关闭后沿用精确会话归档。
- **任务结束**：连通测试沿用所属任务的稳定 `task_id`；任务结束时，用已有 `task_key` 执行上面的批量 `close`，覆盖测试、正式派单及启动失败留下的自有 Pane，再用 `list --open --task-key <task_key>` 核对。保留会话继续复查时不执行这次批量释放。DSH 记录由 `dsh.py` 收尾，批量扫描跳过它们；其他无法解析的记录逐项报错，仍处理有效 job，整批返回退出码 2。
- **异常收尾**：`collect: none` 或等待超时后先查原生记录和 Pane；从 Pane 取得有效答案时，保存文件并用 `finish --result-file ... --review-file ... --outcome ... --close` 登记。任务取消或启动失败且没有有效答案时，用 `close --job` 释放并保留错误记录。无法确认已结束、仍忙碌或身份不符时报告具体 job，保留待处理，不以 `--force` 兜底。布局已经不存在时也用 `close` 补记，再按需 `prune`。
- **断线与恢复**：前台断开可以保留进程，server 重启会结束旧进程并可能通过 integration 恢复原生对话。OMP/Claude 的显式 `recover` 见 [原生 CLI](native-clis.md)；其他 CLI 核对原生会话后建立带 `related_job` 的关联记录，不猜测绑定、不自动重发。

原生恢复需对应 integration，按实际 CLI 检查 `herdr integration status`。安装 hook/plugin 是独立配置变更，仅在任务已授权接入配置时进行并保留原配置；支持启动不等于支持恢复。终端快照不是完整档案，重要可见消息可用 CLI 导出再 `attach`，不声称保存了隐藏推理。

官方来源：[技能](https://herdr.dev/docs/agent-skill/)、[自动化](https://herdr.dev/docs/agent-automation/)、[恢复](https://herdr.dev/docs/session-state/)、[integrations](https://herdr.dev/docs/integrations/)。
