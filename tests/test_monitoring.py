#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ParallelRunStatusTests(unittest.TestCase):
    def make_repo(self, root):
        repo = root / "simpler"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "perf-test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name",
                        "Perf Test"], check=True)
        workflow = repo / ".github" / "workflows"
        workflow.mkdir(parents=True)
        (workflow / "ci.yml").write_text(
            "PTO_ISA_COMMIT: 0123456789abcdef0123456789abcdef01234567\n")
        (repo / "README.md").write_text("fixture\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"],
                       check=True)
        return repo, subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def run_parallel(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self.make_repo(root)
            bindir = root / "bin"
            bindir.mkdir()
            fake_python = bindir / "python"
            fake_python.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json
                import os
                import sys

                args = sys.argv[1:]
                jsonl = args[args.index("--append-jsonl") + 1]
                sha = args[args.index("--commit-list") + 1].split()[0]
                mode = os.environ["FAKE_BENCH_RESULT"]
                success = mode != "failure"
                entry = {
                    "sha": sha,
                    "subject": "fixture",
                    "rc": 0 if success else 1,
                    "summary": "Performance Summary" if success else "",
                    "device": "1",
                    "host": {
                        "status": "failed" if mode == "host_failure" else "ok",
                        "cases": {},
                    },
                }
                with open(jsonl, "a") as stream:
                    stream.write(json.dumps(entry) + "\\n")
                """))
            fake_python.chmod(0o755)
            fake_task_submit = bindir / "task-submit"
            fake_task_submit.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_task_submit.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "PATH": f"{bindir}:{env['PATH']}",
                "FAKE_BENCH_RESULT": mode,
                "PERF_MAX_ROUNDS": "1",
                "PERF_MAX_ATTEMPT": "1",
            })
            return subprocess.run(
                ["bash", str(ROOT / "perf_history_parallel.sh"),
                 "--repo", str(repo), "--workdir", str(root / "work"),
                 "--commit-list", sha, "-m", "1", "-r", "1"],
                text=True, capture_output=True, env=env,
            )

    def test_all_failed_measurements_return_monitoring_failure(self):
        result = self.run_parallel("failure")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("all 1 selected commit(s) failed", result.stderr)

    def test_strict_success_keeps_zero_exit_status(self):
        result = self.run_parallel("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("finished: 1 / 1 commit(s) strictly succeeded", result.stdout)

    def test_host_measurement_failure_is_retried_and_fails_batch(self):
        result = self.run_parallel("host_failure")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("all 1 selected commit(s) failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
