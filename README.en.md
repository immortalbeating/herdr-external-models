# Herdr External Models

[中文](README.md) | [English](README.en.md)

An agent skill for coordinating Codex, Claude, OMP, Antigravity CLI (Agy), and DSH through Herdr, with task records, native response collection, review, and pane cleanup.

This is an independently maintained companion skill. It loads the official guide supplied by the installed Herdr version through `herdr --skill`. It does not bundle or replace Herdr, the model CLIs, or their authentication settings.

## Features

- **Herdr coordination:** organize work as a project Workspace, task Tabs, and CLI session Panes, with follow-up messages and session reuse after identity checks.
- **Native response collection:** locate the matching task and turn in Codex, Claude, OMP, and Agy session records, preserving the visible final answer and a collection receipt.
- **Other routes:** independent `codex exec`, direct APIs compatible with Responses or Chat Completions, and a DSH headless adapter running inside a Herdr Pane.
- **Review and cleanup:** store requests, results, reviews, and status under the project's `.agent-runs` directory. Release owned panes after checking identity and runtime state; after successfully closing a Codex pane, archive its exact native session.
- **Leftover checks:** handle connectivity tests, regular assignments, and failed attempts by task. DSH and regular job records can coexist; one unreadable record does not prevent other valid jobs from being released.
- **Shared waiting and recovery:** live status and bounded waits across jobs, with optional collection of the current answer; explicitly retain stale results or recover selected Codex images with SHA-256 receipts.

`finish` records the caller's review. A CLI reporting idle/done does not prove that a project task passed. See [release and recovery](references/herdr.md#释放与恢复) for collection or startup failures. The controller must still execute cleanup commands; this skill does not run a background cleanup service.

## Requirements

- Python **3.11+**. Runtime scripts use only the standard library.
- For the Herdr route, install Herdr, start the selected server, and configure the integrations, model, and authentication required by each CLI.
- The DSH route also requires Node, a compatible DSH headless CLI, and a configured headless profile.
- `direct` and Codex `exec` do not require Herdr. Direct API calls read compatible providers from the local Codex TOML `model_providers` section and obtain credentials from environment variables.

Validation has primarily used **Windows**. DSH process reconciliation currently supports Windows only; other operating systems have not received full end-to-end validation. CLI flags and native record formats may change between versions. Check the installed CLI's help; historical compatibility notes are not a promise of support for the latest version.

## Installation

Place the entire repository in your client's user skills directory, keeping the directory name `herdr-external-models`. For example, install it for Codex from PowerShell:

```powershell
$herdrSkillPath = Join-Path $HOME ".codex/skills/herdr-external-models"
git clone https://github.com/immortalbeating/herdr-external-models.git $herdrSkillPath
```

For Claude Code, use `~/.claude/skills/herdr-external-models`. Multiple clients can also link to the same skill directory. If a skill with that name already exists, check its source and preserve local changes first. Keep `SKILL.md`, `references/`, and `scripts/` in their original relative locations.

Have the client discover the newly installed skill, then request it directly:

> Use herdr-external-models to delegate this task through the running Herdr service, preserve the result, and release the pane after review.

The skill loads the local official Herdr guide and the relevant references as needed. Installing it does not install external CLIs or grant additional model, filesystem, or process permissions.

## Usage

Start with [SKILL.md](SKILL.md) for the complete operating rules. In the commands below, `<skill>` is the actual skill directory. Specs use absolute paths, models configured by the user, and an explicit server target.

```powershell
python "<skill>/scripts/run.py" run --spec "<spec.json>" --prompt-file "<task.md>" --dry-run
python "<skill>/scripts/run.py" run --spec "<spec.json>" --prompt-file "<task.md>"
python "<skill>/scripts/run.py" wait --job "<job-directory>" --timeout 600 --collect
python "<skill>/scripts/run.py" status --job "<job-directory>"
python "<skill>/scripts/run.py" collect --job "<job-directory>"
python "<skill>/scripts/run.py" finish --job "<job-directory>" --review-file "<review.md>" --outcome completed --close
```

DSH uses its own lifecycle in `scripts/dsh.py`; see the reference below for commands. After changing a model, permission policy, or tool configuration, validate the capabilities required by the task in an isolated directory.

For open Herdr jobs, `status` now refreshes live state by default; use `--cached` for offline records. Repeat `--job` with `wait`, and use `--all` to wait for every job. `--collect` waits for a valid answer from the current round. Timeouts do not cancel or resubmit work. Job IDs can be resolved with `--workspace`. Failure JSON remains on stdout, with a readable error summary on stderr.

When inputs have changed, `collect --accept-changed` only retains an explicitly marked stale answer; it cannot approve it as completed. `prune` can reconcile records for terminals confirmed absent on the same server, while preserving live or uncertain panes. Image recovery requires explicit sources and destination scope; see [Codex image recovery](references/native-clis.md#codex-图片恢复).

| Topic | Reference |
| --- | --- |
| Official guide, layout, dispatch, follow-up, and cleanup | [Herdr sessions](references/herdr.md) |
| OMP / Claude / Codex / Agy arguments and native records | [Native CLIs](references/native-clis.md) |
| Direct API and Codex exec | [API and one-shot CLI](references/direct-exec.md) |
| DSH headless, continuation, and process reconciliation | [DSH delegation](references/dsh.md) |
| Task states, attachments, review, and input version checks | [Records and review](references/records.md) |

The operating skill and detailed references are currently written in Chinese; both README files cover installation and the public entry points.

## Development and validation

Run from the repository root:

```powershell
python -B -X utf8 -m unittest discover -s scripts -p "test_*.py"
```

Tests use temporary directories, mocked CLI / Herdr calls, and localhost interfaces. They do not call paid models or operate real Herdr panes. Validate actual integrations separately after native CLI upgrades; passing tests does not establish compatibility with every future release.

Runtime records, credentials, native sessions, and machine caches are not part of the published skill. Run relevant behavioral tests when changing scripts, and keep documentation clear about the boundary between the official guide and this companion skill's contracts.
