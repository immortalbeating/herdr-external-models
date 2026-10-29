# API 与一次性 CLI

## 能力与适配边界

`direct/exec/herdr` 是本技能的路由名。Herdr 官方 `agent start` 在 Pane 中启动交互 CLI，本身没有这组三选一字段。原生 CLI 支持非交互执行，不等于本技能已实现其独立执行、原生结果解析和续接适配。

| CLI | 原生非交互入口 | 本技能当前已实现的 CLI 路径 |
| --- | --- | --- |
| Codex | `codex exec`，JSON 事件与最终输出 | `run.py mode: exec` 或 `mode: herdr` |
| Claude | `claude -p`，`--output-format json` | `run.py mode: herdr`；独立 exec 尚未适配 |
| OMP | `omp -p --mode json`；另有 stdio RPC | `run.py mode: herdr`；独立 exec/RPC 尚未适配 |
| Agy | `agy -p --output-format json`；另有流式输入输出 | `run.py mode: herdr`；独立 exec 尚未适配 |
| DSH | headless `--json`，原生 session ID | 专用 `dsh.py` 在 Herdr Pane 内运行 headless；尚无脱离 Herdr 的启动后端 |

Claude/OMP/Agy 入口于 2026-10-28 核对本机帮助；这只是入口核实，不是本技能对应 exec 后端的端到端验证。参数以安装版帮助为准。`run.py mode: exec` 当前固定启动 Codex，填写其他 `kind` 不会切换执行器。Herdr 与非交互入口的模型、工具和权限分别由原生 CLI 配置；切换路由不会自动解除沙箱。

直接模型 API 只接收提供的材料并返回答案；agent SDK/RPC 则由 agent 运行时管理会话与工具。OMP RPC 和 DSH headless 属于后者，不能作为 `direct` 的 HTTP 模型地址。CLI 登录、订阅额度、技能与历史会话也不能假定可迁移到直接模型 API。

按需核对原生文档：[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[OMP CLI](https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md)、[Agy headless](https://antigravity.google/docs/cli/headless.md)；DSH 的安装版与事件要求见 [DSH 委派](dsh.md)。

## 直接 API

从 `$CODEX_HOME/config.toml`（未设置时 `~/.codex/config.toml`）的 `model_providers` 读取渠道。仅实现 Bearer 环境变量认证和 Responses / Chat Completions；不复制密钥。需要自定义 headers/OAuth 的渠道用其原生 CLI，不猜测认证协议。

配置来源使用 Codex TOML，不表示只能调用 Codex 模型；所选渠道支持兼容协议、认证及请求模型时即可使用。原生 Anthropic Messages、Gemini 协议尚未实现。此路径没有文件/命令工具循环，也不加载相应 CLI 的技能或原生会话；需要这些能力时使用已适配的 CLI 路径。

```json
{
  "mode": "direct",
  "workspace": "C:/path/project",
  "provider": "subagent",
  "model": "用户指定且已配置的模型ID",
  "parent_thread_id": "当前Codex任务ID（已知时填写）",
  "images": ["C:/path/reference.png"],
  "timeout": 180
}
```

`images` 可省略；`config` 可指定其他 Codex TOML 文件。模型不固定为某个历史默认值，也不查询付费接口来做每次调用前的探测。直接 API 仅处理提供的材料，没有工具执行循环。HTTP 返回的 incomplete/failed 不作为正常结果。

## Codex exec

```json
{
  "mode": "exec",
  "workspace": "C:/path/project",
  "provider": "subagent",
  "model": "用户指定且已配置的模型ID",
  "access": "read-only",
  "timeout": 600
}
```

脚本使用 `--json` 保存工具事件，保留正常原生会话而不使用 `--ephemeral`；通过 `thread.started` 记录 session ID。它启动独立 Codex 进程，不修改主会话 provider，也不会生成原生子代理卡片。

已授权实现时将 `access` 设置为 `workspace-write`，并提供非空 `write_scope` 数组，写明文件/模块与必须保持的接口。沙箱允许工作区写入，并不会根据数组逐文件隔离；并行修改有重叠时选择隔离 worktree。脚本不会自动提交、合并或创建 worktree。

测试需要写工作区外的目录时，优先保留沙箱：`access` 用 `workspace-write`，`add_dirs` 列出放行的绝对目录（脚本转成 `--add-dir`），必要时用 `env` 把程序的用户数据目录指到这个隔离目录（例如 Windows 上 Godot 读 `APPDATA`），派发前先实测确认重定向生效。

用户授权非沙箱运行时（例如沙箱挡住测试需要的进程或用户目录写入），`access` 可设为 `danger-full-access`，必须同时提供 `write_scope` 和 `sandbox_bypass_reason`。范围仍是任务契约，没有沙箱兜底，提示词里要写明不能改动的文件和目录。

可选：`images`、`effort`、`skip_git_repo_check`。`exec` 沿用本机 Codex 配置，不能用 `config` 字段切换文件；如需独立配置环境，在启动脚本前设置对应 `CODEX_HOME`。权限被阻塞时读取日志处理，不通过危险绕过标志自动解锁。

一次性调用保持前台同步；宿主工具返回运行中的进程 ID 时正常等待，别再派发一次。超时会终止直接子进程，但不保证其所有工具子进程都结束，记录为 `interrupted` 并检查工作区。

Windows 下脚本优先解析 npm 安装中的原生 `codex.exe`，避免 shell shim 改写参数。安装布局不同或存在多个候选时，将 `CODEX_EXECUTABLE` 设置为实际原生可执行文件路径；不会猜测另一个安装版本。

Windows 的沙箱 Codex spec 若首先解析到 WindowsApps 中的 Store/MSIX PowerShell，run/dry-run 给出兼容性警告；它不证明所有版本必然失败，也不修改 PATH、全局配置或权限。若产物已生成但 CLI 的复制进程失败，可用 [Codex 图片恢复](native-clis.md#codex-图片恢复) 在宿主显式取回原图并记录摘要。

兼容性快照（2026-10-28，Windows、Codex CLI 0.162.1、Python 3.14）：`workspace-write` 中，通过 `--add-dir` 放行的根下，`tempfile.mkdtemp()` 的 0700 受保护 DACL 仍可能阻止受限令牌写入。这是执行环境失败，不能计作任务断言失败，换成 Herdr TUI 不改变它。可控测试工具可在获准根下用继承 ACL 的独占工作目录（Windows 普通 mkdir）；必须使用未修改 tempfile 时，按已有授权选择明确的无沙箱 spec 并继续隔离 APPDATA。保留原始失败证据，不修改业务工具或全局 ACL 来掩盖问题；升级后重新验证，不能将该快照当作所有版本的结论。

需要继续该 CLI 会话时，先确认原进程已经退出，使用记录中的 ID 和本机 `codex exec resume --help`；不要使用模糊的 `--last`。新一轮需留档，可转入 Herdr：启动同一 Codex 原生会话的 resume 命令，并在新任务 spec 的 `related_job` 指回原 job。不同时恢复一个会话给多个写入者。

## 暂缓扩展（2026-10-28）

- Claude/OMP/Agy 独立 exec 与 DSH 脱离 Herdr 的启动后端：出现明确的无面板、批量自动化需求后，按所需 CLI 单独实现并验证结果、续接、权限和中断；当前继续使用已适配路径。
- 原生模型 API 协议与完整 API agent：先由现有 `direct` 满足材料咨询；实际需求超出时再扩展协议。工具循环、权限和会话管理复用原生 agent，避免维护第二套运行时。

以上是候选记录，不新增执行后端、模型探测或必经步骤；失败时沿用原任务诊断，不自动换路由、渠道或权限重试。
