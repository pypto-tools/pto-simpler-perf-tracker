---
name: pto-simpler-perf-tracker
description: Configure, install, inspect, and safely operate the pto-simpler-perf-tracker workflow for Simpler PR-level Ascend NPU benchmarks and optional Feishu reports. Use when Codex needs to validate configuration, choose commits or time windows, plan NPU runs, inspect generated performance data, troubleshoot a run, or upgrade this tool without losing state.
---

# PTO Simpler Perf Tracker

Use `pto-simpler-perf-tracker` as the only installed command. Treat benchmark execution and Feishu publication as explicit side effects.

## Locate files

Use these installed paths:

```text
/home/pypto-tools/pto-simpler-perf-tracker/app/
/home/pypto-tools/pto-simpler-perf-tracker/config/perf-tracker.env
/home/pypto-tools/pto-simpler-perf-tracker/state/
/home/pypto-tools/pto-simpler-perf-tracker/logs/
```

When working from the repository, use `./run.sh`; source mode writes to ignored `runtime/` paths. Never commit runtime data or the real environment file.

## Follow the safe workflow

1. Inspect the repository status, README, `.env.example`, active config location, available NPU devices, and existing task queue before changing or running anything.
2. Validate scripts with `bash -n` and run `bash tests/test_install.sh` after installation changes.
3. Edit configuration only when requested. Preserve existing webhook and machine-specific values; never print or commit them.
4. Present the exact benchmark scope before starting: commit selection, NPU count, rounds, expected publication behavior, and output location.
5. Run a benchmark only when the user explicitly requests it. Start with the smallest useful scope when validating:

   ```bash
   pto-simpler-perf-tracker --recent 1 -m 1 -r 10
   ```

6. Do not use `--push` unless the user explicitly authorizes publishing to Feishu. Inspect local reports first.
7. Do not delete worktrees, reports, raw JSONL, publication state, or retry data unless the user explicitly requests cleanup and the exact targets are verified.

## Install or upgrade

Use:

```bash
sudo ./install.sh --init-config  # first install only
sudo ./install.sh                # upgrade
```

Installation must update only `app/`, preserve `config/` and `state/`, and must not start benchmarks or send messages.

## Troubleshoot

Separate failures into repository access, build environment, task queue, NPU allocation, benchmark execution, report processing, and Feishu publication. Preserve raw evidence and report the failing layer before proposing changes. Do not retry a large batch blindly.
