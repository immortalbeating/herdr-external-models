---
name: herdr-external-models
description: Coordinate external-model tasks through Herdr using its bundled official guide and project-local task records, or use direct APIs, Codex exec, and DSH headless. Use for Herdr agent collaboration or external-model consultation; not for ordinary local work or native Codex subagents.
---

# Herdr 外部模型

这是外部模型协作的统一入口。主模型负责分工、整合和验收；沿用用户指定的 CLI、渠道、模型与权限。Herdr 基础操作读取本机官方指引，本技能补充任务规格、原生结果回收和验收记录。

## 选择路径

沿用用户或项目明确指定的模式与既有任务 spec。未指定模式且要求通过 Herdr 协作时，优先 `herdr`，一次性任务也适用；明确需要独立非交互执行或批量自动化时选已适配的 `exec`。同一任务连续复查用原会话 `followup`；需要独立审查视角时新建会话。模式须在 spec 中显式填写，脚本没有自动默认值；变更项目既有约定时同步调整规则和模板。

| 当前需要 | 路径 | 按需读取 |
| --- | --- | --- |
| 通过 Herdr 协作、持续对话或观察终端 | 优先 `herdr`，包括一次性委派 | [Herdr 会话](references/herdr.md)，先按其中“读取官方指引”加载 `herdr --skill` 的完整输出 |
| 咨询已提供的文本、代码、日志或图片 | `direct` | [API 与一次性 CLI](references/direct-exec.md) |
| 独立非交互执行 Codex 任务 | `exec`：独立 `codex exec` | 同上；其他 CLI 的原生能力与适配状态见其中“能力与适配边界” |
| Herdr 中的 OMP、Claude、Codex、Agy 参数与按轮回收 | `herdr` 原生适配 | 再读 [原生 CLI](references/native-clis.md) 中所选 CLI 的部分 |
| 向 DSH 派单、续接或清理它的布局 | DSH headless 辅助命令 | [DSH 委派](references/dsh.md)，使用专用 `dsh.py`，不套用 `run.py` 的生命周期命令 |

只需调用本技能；引用的官方指引和参考仍须实际读取，链接本身不会加载内容。官方技能无需另装；CLI 新会话首次使用 Herdr 路径或 Herdr 升级后重新读取。`direct/exec` 不依赖 Herdr。

## 调用与留档

Python 3.11+，仅标准库。`<skill>` 解析到当前技能目录；spec 内路径使用绝对路径。以下是非 DSH 路径的常用命令，补问、恢复和释放细节见所选参考。

```powershell
python "<skill>/scripts/run.py" run --spec "<spec.json>" --prompt-file "<task.md>" --dry-run
python "<skill>/scripts/run.py" run --spec "<spec.json>" --prompt-file "<task.md>"
python "<skill>/scripts/run.py" status --job "<job-directory>"
python "<skill>/scripts/run.py" collect --job "<job-directory>"
python "<skill>/scripts/run.py" finish --job "<job-directory>" --review-file "<review.md>" --outcome completed --close
```

`dry-run` 用于首次接入或配置变化，只检查本地方案。CLI、渠道或权限/工具组合变化后，在隔离目录验证本次任务所需能力，交叉核对实际工具调用与宿主可见产物；启动、`OK` 回复或配置中的模型名都不能替代验证。配置未变时复用有效证据。

## 共用契约

- 任务给出目标、必要材料、范围和验收条件；派单前核对项目当前角色与阶段的授权，不把某个主控模型的分工写死成通用规则。写入任务提供 `write_scope` 并保护已有修改；强制权限由 CLI 或隔离环境实施，不静默换模型或扩大写入范围。
- 已授权的项目任务分组按项目 → Workspace、任务 → Tab、CLI 会话 → Pane 组织；普通官方操作保留当前 Tab/CWD 的默认布局。用稳定任务 ID，同一任务补问复用原生对话；不同任务可并行，同一 job 由一个调度者操作。外部任务不递归调用本技能。
- 认证沿用现有登录或环境变量；spec、提示词和日志不放密钥。`model_requested`、API 的 `model_reported` 与 CLI 观察值分别记录，不把请求参数或显示名称当作服务内部模型证明。
- 经辅助命令的真实调用保存在 `.agent-runs/<job-id>/`；普通官方操作由主模型保存所需项目结果，不伪称已有 job。主上下文优先接收状态、结果和路径。完整追溯、导出或解释状态时读 [记录与验收](references/records.md)。
- 失败或超时先查已有状态、原生身份和输出，再处理现有任务；不自动重发。返回、idle/done 和零退出码均不等于验收通过；检查产物后用对应 `finish` 记录结论，再释放自有布局。连通测试与异常分支也须按 [释放与恢复](references/herdr.md#释放与恢复) 收尾。

`finish` 内置本地收尾核对；只读审查可绑定明确输入文件，任务结束时可附上已知相关 job，无需另开检查命令。字段、过期结果处理及暂缓项见 [记录与验收](references/records.md#轻量核对)。

交付说明任务编号、渠道/模型、结果、必要验证和记录位置；完整历史留在磁盘，按需回查片段。
