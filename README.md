# Herdr External Models

[中文](README.md) | [English](README.en.md)

通过 Herdr 协调 Codex、Claude、OMP、Antigravity CLI（Agy）及 DSH 的 Agent Skill，补充任务记录、原生结果采集、验收和面板释放。

这是独立维护的辅助技能。Herdr 的基础操作由安装版本自带的 `herdr --skill` 提供，本仓库按需加载该指引；不捆绑或替代 Herdr、模型 CLI 和认证配置。

## 能做什么

- **Herdr 协作**：按项目 Workspace、任务 Tab、CLI 会话 Pane 组织工作；支持继续对话和核实身份后的会话复用。
- **原生结果回收**：从 Codex、Claude、OMP、Agy 的原生记录中定位对应任务和轮次，保留可见最终答案及回执。
- **其他路径**：独立 `codex exec`、兼容 Responses / Chat Completions 的直接 API，以及 Herdr Pane 中的 DSH headless 适配。
- **验收与收尾**：项目 `.agent-runs` 保存请求、结果、验收与状态；确认归属和运行状态后释放自有面板，成功关闭 Codex 面板后归档其精确原生会话。
- **遗留检查**：按任务处理连通测试、正式调用和失败尝试；兼容 DSH 与普通 job 混合存放，单个坏记录不会中断其他有效任务的释放。
- **统一等待与恢复**：实时状态与有界多任务等待，按需自动采集本轮答案；显式留存过期结果，或取回指定的 Codex 原图并记录 SHA-256。

`finish` 是调用者的验收记录，CLI 的 idle/done 不是项目通过证明。采集或启动失败的收尾见 [释放与恢复](references/herdr.md#释放与恢复)；主控仍需执行收尾命令，本技能不运行后台定时回收。

## 前置条件

- Python **3.11+**；运行脚本只使用标准库。
- 使用 Herdr 路径时，安装 Herdr、启动指定 server，并按所选 CLI 配置 integration、模型和登录。
- DSH 路径还需要 Node、兼容的 DSH headless CLI 和已配置的 headless profile。
- `direct` / Codex `exec` 不要求 Herdr。直接 API 从本机 Codex TOML 的 `model_providers` 读取兼容渠道，并通过环境变量取得凭据。

验证环境以 **Windows** 为主；DSH 进程对账当前仅支持 Windows，其他操作系统尚未进行完整端到端验证。CLI 参数和原生记录格式可能随版本变化，使用时核对安装版帮助；文档中的历史快照不代表最新版本支持承诺。

## 安装

把整个仓库放到客户端的用户技能目录，目录名保留为 `herdr-external-models`。例如在 PowerShell 中安装到 Codex：

```powershell
$herdrSkillPath = Join-Path $HOME ".codex/skills/herdr-external-models"
git clone https://github.com/immortalbeating/herdr-external-models.git $herdrSkillPath
```

Claude Code 可放到 `~/.claude/skills/herdr-external-models`。也可让多个客户端链接到同一份技能目录。已有同名技能时先核对来源与本地修改，保留 `SKILL.md`、`references/`、`scripts/` 的相对结构。

安装后让客户端重新发现技能，使用时直接要求：

> 使用 herdr-external-models，通过已运行的 Herdr 服务派发这项任务，保留结果并在验收后释放面板。

技能会按分支读取本机 Herdr 官方指引及对应参考。安装技能不会安装外部 CLI，也不会授予额外模型、文件或进程权限。

## 使用入口

完整规则从 [SKILL.md](SKILL.md) 开始。以下 `<skill>` 是实际技能目录，spec 中使用绝对路径、用户已配置的模型与明确的服务目标。

```powershell
python "<skill>/scripts/run.py" run --spec "<spec.json>" --prompt-file "<task.md>" --dry-run
python "<skill>/scripts/run.py" run --spec "<spec.json>" --prompt-file "<task.md>"
python "<skill>/scripts/run.py" wait --job "<job-directory>" --timeout 600 --collect
python "<skill>/scripts/run.py" status --job "<job-directory>"
python "<skill>/scripts/run.py" collect --job "<job-directory>"
python "<skill>/scripts/run.py" finish --job "<job-directory>" --review-file "<review.md>" --outcome completed --close
```

DSH 使用 `scripts/dsh.py` 的独立生命周期，具体命令见下表。模型、权限或工具配置改变后，应在隔离目录验证任务实际需要的能力。

开放 Herdr job 的 `status` 默认实时查询，离线读取用 `--cached`。`wait` 可重复传 `--job`，用 `--all` 等待全部；`--collect` 等到本轮有效答案已回收，超时不取消或重发。job ID 可配合 `--workspace` 使用。失败 JSON 保留在 stdout，stderr 提供错误摘要。

输入已经变化时，`collect --accept-changed` 只留存带标记的过期答案，不能验收为 completed。`prune` 可补记同一服务中已确认消失的面板记录，仍存在或身份不明的面板保留。图片复制使用明确来源和目标范围，详见 [Codex 图片恢复](references/native-clis.md#codex-图片恢复)。

| 内容 | 入口 |
| --- | --- |
| 官方指引、布局、投递、补问与释放 | [Herdr 会话](references/herdr.md) |
| OMP / Claude / Codex / Agy 参数及原生记录 | [原生 CLI](references/native-clis.md) |
| 直接 API 与 Codex exec | [API 与一次性 CLI](references/direct-exec.md) |
| DSH headless、续接与进程对账 | [DSH 委派](references/dsh.md) |
| 任务状态、附件、验收及输入版本核对 | [记录与验收](references/records.md) |

## 开发与验证

在仓库根目录运行：

```powershell
python -B -X utf8 -m unittest discover -s scripts -p "test_*.py"
```

测试使用临时目录、模拟 CLI / Herdr 和 localhost 接口，不调用付费模型、不操作实际 Herdr 面板。原生 CLI 升级后的真实接入能力需另外验证；测试通过不代表所有未来版本均兼容。

运行记录、凭据、原生会话和本机缓存不属于发布内容。修改脚本时运行相关行为测试，文档更新保持官方指引与辅助契约的边界。
