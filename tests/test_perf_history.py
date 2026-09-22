#!/usr/bin/env python3
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import perf_history


class BenchmarkDispatchTests(unittest.TestCase):
    def test_host_bind_metrics_drop_cold_bind_and_sum_within_each_bind(self):
        text = """[stamp] abc123 env ...
bind phase=host_orch start_ns=1 dur_ns=9000000
bind phase=graph_upload start_ns=2 dur_ns=3000000
bind phase=arena_h2d start_ns=3 dur_ns=1000000
bind phase=host_orch start_ns=4 dur_ns=1000000
bind phase=graph_upload start_ns=5 dur_ns=4000000
bind phase=arena_h2d start_ns=6 dur_ns=1000000
bind phase=host_orch start_ns=7 dur_ns=3000000
bind phase=graph_upload start_ns=8 dur_ns=1000000
bind phase=arena_h2d start_ns=9 dur_ns=1000000
"""
        metrics = perf_history.parse_host_bind_metrics(text, rounds=3, ranks=1)

        self.assertEqual(metrics["binds"], 3)
        self.assertEqual(metrics["warm_binds"], 2)
        self.assertEqual(metrics["metrics"]["host_orch"]["min_us"], 1000.0)
        self.assertEqual(metrics["metrics"]["host_orch"]["median_us"], 2000.0)
        # Per-bind totals are 6 ms and 5 ms. This must not be the 3 ms sum of
        # minima selected independently from different binds.
        self.assertEqual(
            metrics["metrics"]["control_plane"]["min_us"], 5000.0)

    def test_strace_bind_metrics_group_interleaved_ranks_by_pid_and_invocation(self):
        def spans(pid, inv, host_orch, graph_upload, arena_h2d):
            phases = {
                "host_orch": host_orch,
                "graph_upload": graph_upload,
                "arena_h2d": arena_h2d,
            }
            rows = [
                f"noise [STRACE] dur={dur} name=chip.run.bind.{phase} "
                f"inv={inv} pid={pid}"
                for phase, dur in phases.items()
            ]
            rows.append(
                f"noise [STRACE] name=chip.run.bind inv={inv} pid={pid} dur=99")
            return rows

        # Rank 20's warm bind arrives before rank 10's cold bind. Dropping the
        # first two global groups would therefore retain the wrong sample.
        text = "\n".join(
            spans(20, 1, 9000, 9000, 9000)
            + spans(20, 2, 2000, 3000, 4000)
            + spans(10, 1, 8000, 8000, 8000)
            + spans(10, 2, 4000, 5000, 6000)
        )

        metrics = perf_history.parse_host_bind_metrics(text, rounds=2, ranks=2)

        self.assertEqual(metrics["binds"], 4)
        self.assertEqual(metrics["warm_binds"], 2)
        self.assertEqual(metrics["metrics"]["host_orch"]["min_us"], 2.0)
        self.assertEqual(metrics["metrics"]["host_orch"]["max_us"], 4.0)
        self.assertEqual(metrics["metrics"]["control_plane"]["min_us"], 9.0)

    def test_strace_bind_requires_root_completion_record(self):
        text = """[STRACE] pid=7 inv=1 name=chip.run.bind.host_orch dur=1000
[STRACE] pid=7 inv=1 name=chip.run.bind.graph_upload dur=1000
[STRACE] pid=7 inv=1 name=chip.run.bind.arena_h2d dur=1000
"""
        with self.assertRaisesRegex(ValueError, "no complete"):
            perf_history.parse_host_bind_metrics(text, rounds=1, ranks=1)

    def test_partial_control_plane_phase_rejects_truncated_host_log(self):
        text = """bind phase=host_orch start_ns=1 dur_ns=1000000
bind phase=graph_upload start_ns=2 dur_ns=1000000
bind phase=arena_h2d start_ns=3 dur_ns=1000000
bind phase=host_orch start_ns=4 dur_ns=1000000
bind phase=arena_h2d start_ns=5 dur_ns=1000000
bind phase=host_orch start_ns=6 dur_ns=1000000
bind phase=graph_upload start_ns=7 dur_ns=1000000
bind phase=arena_h2d start_ns=8 dur_ns=1000000
"""
        with self.assertRaisesRegex(ValueError, "missing from some warm binds"):
            perf_history.parse_host_bind_metrics(text, rounds=3, ranks=1)

    def test_missing_bind_rejects_incomplete_host_log(self):
        text = """bind phase=host_orch start_ns=1 dur_ns=1000000
bind phase=graph_upload start_ns=2 dur_ns=1000000
bind phase=arena_h2d start_ns=3 dur_ns=1000000
bind phase=host_orch start_ns=4 dur_ns=1000000
bind phase=graph_upload start_ns=5 dur_ns=1000000
bind phase=arena_h2d start_ns=6 dur_ns=1000000
"""
        with self.assertRaisesRegex(ValueError, "expected exactly 3"):
            perf_history.parse_host_bind_metrics(text, rounds=3, ranks=1)

    def test_cann_env_override_is_strict_and_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "set_env.sh"
            script.write_text("export TEST_CANN_ENV=1\n")
            with mock.patch.dict(
                    perf_history.os.environ,
                    {"PERF_CANN_ENV_SCRIPT": str(script)}, clear=True):
                self.assertEqual(
                    perf_history.resolve_cann_env_script(), script.resolve())

    def test_missing_cann_env_override_fails_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-set_env.sh"
            with mock.patch.dict(
                    perf_history.os.environ,
                    {"PERF_CANN_ENV_SCRIPT": str(missing)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    perf_history.resolve_cann_env_script()

    def test_allocated_onboard_run_requires_arch_precheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            script = (repo / ".claude" / "skills" /
                      "onboard-arch-precheck" / "check.sh")
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env bash\nexit 0\n")
            self.assertEqual(
                perf_history.resolve_arch_precheck_script(
                    repo, "a2a3", task_submit=True),
                script.resolve())
            self.assertIsNone(perf_history.resolve_arch_precheck_script(
                repo, "a2a3sim", task_submit=True))
            with self.assertRaisesRegex(RuntimeError, "precheck is missing"):
                perf_history.resolve_arch_precheck_script(
                    repo / "missing", "a2a3", task_submit=True)

    def test_summary_retains_completion_counts(self):
        text = """noise
================
Performance Summary (runtime)
================
Example Host (us)
case 1.0
Benchmark complete (runtime): 1 passed, 0 failed (1 total)
================
"""
        summary = perf_history.extract_summary_block(text)
        self.assertIn("1 passed, 0 failed (1 total)", summary)

    def test_task_submit_wraps_only_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktree"
            venv = root / ".venv"
            logfile = root / "bench_raw.txt"
            venv.mkdir(parents=True)

            def fake_run(cmd, **kwargs):
                self.assertEqual(cmd[0], "task-submit")
                self.assertIn("--device", cmd)
                self.assertEqual(cmd[cmd.index("--device") + 1], "auto")
                self.assertIn("--max-time", cmd)
                self.assertIn("1800", cmd)
                self.assertIn("--run", cmd)
                run_cmd = cmd[cmd.index("--run") + 1]
                self.assertIn(str(root / "tools" / "benchmark_rounds.sh"), run_cmd)
                self.assertIn("/tmp/pto-perf-", run_cmd)
                self.assertIn("cleanup_perf_tmp", run_cmd)
                self.assertIn("--verbose", run_cmd)
                self.assertIn(str(venv / "bin" / "activate"), run_cmd)
                self.assertIn("source /dev/null", run_cmd)
                self.assertIn("/dev/null a2a3", run_cmd)
                self.assertIn("import torch_npu", run_cmd)
                self.assertIn('-d "$TASK_DEVICE"', run_cmd)
                kwargs["stdout"].write(
                    "[perf-tracker] device=7\nPerformance Summary\n")
                return subprocess.CompletedProcess(cmd, 0)

            with mock.patch.object(perf_history.subprocess, "run", side_effect=fake_run):
                with mock.patch.dict(perf_history.os.environ,
                                     {"TMP_DIR": str(root / "tool-tmp")}):
                    output, rc = perf_history.run_benchmark(
                        root, "3", 100, "tensormap_and_ringbuffer", "a2a3",
                        logfile, venv=venv, task_submit=True,
                        task_wait_timeout=86400, task_max_time=1800,
                        cann_env_script=Path("/dev/null"),
                        arch_precheck_script=Path("/dev/null"),
                    )

            self.assertEqual(rc, 0)
            self.assertIn("Performance Summary", output)
            self.assertEqual(perf_history.task_device_from_output(output), "7")

    def test_each_host_case_uses_its_required_device_count(self):
        for case_name, expected_devices in (("qwen3-14b", "1"),
                                            ("dsv4-flash", "2")):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "worktree"
                venv = root / ".venv"
                logfile = root / f"host_{case_name}.txt"
                venv.mkdir(parents=True)

                def fake_run(cmd, **kwargs):
                    self.assertEqual(cmd[0], "task-submit")
                    self.assertEqual(
                        cmd[cmd.index("--device-num") + 1], expected_devices)
                    run_cmd = cmd[cmd.index("--run") + 1]
                    self.assertIn("SIMPLER_HBG_BIND_BREAKDOWN_ENABLE=1", run_cmd)
                    self.assertIn("SIMPLER_SKIP_DEVICE_RUN=1", run_cmd)
                    self.assertIn("--rounds 6", run_cmd)
                    expected_entry = perf_history.HOST_CASES[case_name]["entry"]
                    self.assertIn(expected_entry, run_cmd)
                    if case_name == "qwen3-14b":
                        self.assertIn("--log-level timing", run_cmd)
                    else:
                        self.assertNotIn("--log-level timing", run_cmd)
                    kwargs["stdout"].write("[perf-tracker] device=7,8\n")
                    return subprocess.CompletedProcess(cmd, 0)

                with mock.patch.object(
                        perf_history.subprocess, "run", side_effect=fake_run):
                    output, rc = perf_history.run_host_benchmark(
                        root, perf_history.HOST_CASES[case_name], "3,4", 6,
                        logfile, venv=venv, task_submit=True,
                        task_wait_timeout=86400, task_max_time=3600,
                        cann_env_script=Path("/dev/null"),
                        arch_precheck_script=Path("/dev/null"),
                        commit_sha="abc123",
                    )

                self.assertEqual(rc, 0)
                self.assertTrue(output.startswith("[stamp] abc123 "))
                self.assertEqual(perf_history.task_device_from_output(output),
                                 "7,8")

    def test_verbose_benchmark_logging_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktree"
            logfile = root / "bench_raw.txt"
            root.mkdir(parents=True)

            def fake_run(cmd, **kwargs):
                run_cmd = cmd[cmd.index("--run") + 1]
                self.assertNotIn("--verbose", run_cmd)
                kwargs["stdout"].write("[perf-tracker] device=3\n")
                return subprocess.CompletedProcess(cmd, 1)

            with (
                mock.patch.dict(perf_history.os.environ,
                                {"PERF_BENCH_VERBOSE": "0"}),
                mock.patch.object(perf_history.subprocess, "run",
                                  side_effect=fake_run),
            ):
                perf_history.run_benchmark(
                    root, "auto", 1, "runtime", "a2a3", logfile,
                    task_submit=True, cann_env_script=Path("/dev/null"),
                )

    def test_benchmark_tmpdir_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktree"
            logfile = root / "bench_raw.txt"
            root.mkdir(parents=True)

            def fake_run(cmd, **kwargs):
                self.assertIn("TMPDIR=/custom/tmp", cmd)
                kwargs["stdout"].write("[perf-tracker] device=3\n")
                return subprocess.CompletedProcess(cmd, 0)

            with (
                mock.patch.dict(perf_history.os.environ,
                                {"PERF_BENCH_TMPDIR": "/custom/tmp"}),
                mock.patch.object(perf_history.subprocess, "run",
                                  side_effect=fake_run),
            ):
                perf_history.run_benchmark(
                    root, "auto", 1, "runtime", "a2a3", logfile,
                    task_submit=True, cann_env_script=Path("/dev/null"),
                )

    def test_managed_benchmark_tmp_is_home_backed_and_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktree"
            tools = root / "tools"
            tools.mkdir(parents=True)
            benchmark = tools / "benchmark_rounds.sh"
            benchmark.write_text(
                "#!/usr/bin/env bash\n"
                "echo TMPDIR=$TMPDIR\n"
                "echo REAL_TMPDIR=$(readlink -f -- \"$TMPDIR\")\n"
                "echo CANN_MARKER=$TEST_CANN_ENV\n"
                "touch \"$TMPDIR/probe\"\n"
            )
            benchmark.chmod(0o755)
            logfile = root / "bench_raw.txt"
            home_tmp = Path(tmp) / "tool-tmp"
            cann_env = Path(tmp) / "set_env.sh"
            cann_env.write_text("export TEST_CANN_ENV=loaded\n")

            with mock.patch.dict(
                    perf_history.os.environ,
                    {"TMP_DIR": str(home_tmp)}, clear=False):
                output, rc = perf_history.run_benchmark(
                    root, "0", 1, "runtime", "a2a3", logfile,
                    task_submit=False, cann_env_script=cann_env,
                )

            self.assertEqual(rc, 0, output)
            alias = next(
                line.removeprefix("TMPDIR=") for line in output.splitlines()
                if line.startswith("TMPDIR="))
            real = next(
                line.removeprefix("REAL_TMPDIR=") for line in output.splitlines()
                if line.startswith("REAL_TMPDIR="))
            self.assertTrue(alias.startswith("/tmp/pto-perf-"), alias)
            self.assertTrue(real.startswith(str(home_tmp / "benchmark")), real)
            self.assertIn("CANN_MARKER=loaded", output)
            self.assertFalse(Path(alias).exists())
            self.assertEqual(list((home_tmp / "benchmark").iterdir()), [])

    def test_live_task_timeout_is_not_treated_as_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktree"
            logfile = root / "bench_raw.txt"
            root.mkdir(parents=True)

            def fake_run(cmd, **kwargs):
                kwargs["stdout"].write("错误: 等待超时\n任务仍在运行\n")
                return subprocess.CompletedProcess(cmd, 1)

            with mock.patch.object(perf_history.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(perf_history.TaskStillRunning):
                    perf_history.run_benchmark(
                        root, "2", 1, "runtime", "a2a3", logfile,
                        task_submit=True, task_wait_timeout=10,
                        task_max_time=5,
                        cann_env_script=Path("/dev/null"),
                    )


if __name__ == "__main__":
    unittest.main()
