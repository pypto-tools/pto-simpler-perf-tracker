#!/usr/bin/env python3
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_weekly_report", ROOT / "ci_weekly_report.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WeekAndStatisticTests(unittest.TestCase):
    def test_previous_week_crosses_iso_year(self):
        now = dt.datetime(2026, 1, 5, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(MODULE.previous_week(now), "2026-W01")
        start, end = MODULE.week_bounds("2026-W01")
        self.assertEqual(start.astimezone(MODULE.REPORT_TZ).date(),
                         dt.date(2025, 12, 29))
        self.assertEqual(end.astimezone(MODULE.REPORT_TZ).date(),
                         dt.date(2026, 1, 5))
        self.assertEqual(start.hour, 16)

    def test_nearest_rank_percentiles(self):
        values = list(range(1, 11))
        self.assertEqual(MODULE.percentile(values, 0.50), 5)
        self.assertEqual(MODULE.percentile(values, 0.90), 9)
        self.assertIsNone(MODULE.percentile([], 0.90))

    def test_week_rollover_uses_beijing_midnight(self):
        before = dt.datetime(2026, 8, 23, 15, 59, tzinfo=dt.timezone.utc)
        after = dt.datetime(2026, 8, 23, 16, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(MODULE.previous_week(before), "2026-W33")
        self.assertEqual(MODULE.previous_week(after), "2026-W34")

    def test_current_week_uses_beijing_date(self):
        before = dt.datetime(2026, 8, 23, 15, 59, tzinfo=dt.timezone.utc)
        after = dt.datetime(2026, 8, 23, 16, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(MODULE.current_week(before), "2026-W34")
        self.assertEqual(MODULE.current_week(after), "2026-W35")

    def test_weekly_report_code_tables_are_aligned(self):
        aggregates = [{
            "job": "st-sim-a5", "os": "ubuntu", "path": "github-hosted",
            "runner_tier": "github-standard", "n": 9, "total": 10,
            "wall": {"p50": 123, "p90": 456},
            "phases": {"test": {"n": 9, "p50": 80, "p90": 100}},
            "slowest": [],
        }, {
            "job": "st-onboard-a5", "os": "linux", "path": "self-hosted",
            "runner_tier": "standard", "n": 7, "total": 8,
            "wall": {"p50": 234, "p90": 567},
            "phases": {"setup": {"n": 7, "p50": 10, "p90": 20}},
            "slowest": [],
        }]
        rendered = MODULE.weekly_markdown(
            "2026-W35", "2026-09- multiline", aggregates)
        code_blocks = rendered.split("```text")[1:]
        self.assertEqual(len(code_blocks), 2)
        for block in code_blocks:
            lines = [line for line in block.split("```", 1)[0].strip().splitlines()
                     if line]
            self.assertEqual(len({len(line) for line in lines}), 1)
            self.assertNotIn(" | ", block)
        phase_block = code_blocks[1].split("```", 1)[0]
        self.assertIn("\n\nst-onboard-a5", phase_block)


class NormalizationTests(unittest.TestCase):
    def test_self_hosted_runner_is_replaced_by_tier(self):
        run = {"id": 7, "html_url": "https://example/run/7"}
        job = {
            "id": 8,
            "name": "st-onboard-a2a3 / st-onboard-a2a3",
            "labels": ["self-hosted", "a2a3"],
            "runner_id": 46,
            "runner_name": "private-hostname",
            "conclusion": "success",
            "started_at": "2026-08-20T00:00:00Z",
            "completed_at": "2026-08-20T00:10:00Z",
            "steps": [
                {"name": "Set up environment", "conclusion": "success",
                 "started_at": "2026-08-20T00:00:10Z",
                 "completed_at": "2026-08-20T00:02:10Z"},
                {"name": "Run pytest scene tests (a2a3)", "conclusion": "success",
                 "started_at": "2026-08-20T00:02:10Z",
                 "completed_at": "2026-08-20T00:09:10Z"},
            ],
        }
        record = MODULE.normalize_job(job, run, {"46": "standard"}, "salt")
        self.assertEqual(record["runner_tier"], "standard")
        self.assertEqual(record["wall_seconds"], 600)
        self.assertEqual(record["phases"], {
            "install_build": 120, "test": 420})
        self.assertNotIn("runner_id", record)
        self.assertNotIn("runner_name", record)
        self.assertNotIn("private-hostname", json.dumps(record))

    def test_unclassified_runners_stay_in_separate_anonymous_buckets(self):
        base = {"labels": ["self-hosted", "a5"]}
        one = MODULE.runner_dimensions({**base, "runner_id": 1}, {}, "salt")
        two = MODULE.runner_dimensions({**base, "runner_id": 2}, {}, "salt")
        self.assertNotEqual(one[2], two[2])
        self.assertTrue(one[2].startswith("unclassified-"))

    def test_skipped_reusable_caller_is_not_a_performance_sample(self):
        run = {"id": 7, "html_url": "https://example/run/7"}
        job = {
            "id": 8, "name": "st-onboard-a5 / st-onboard-a5",
            "labels": ["ubuntu-latest"], "conclusion": "skipped",
        }
        self.assertIsNone(MODULE.normalize_job(job, run, {}, "salt"))

    def test_aggregation_keeps_paths_separate(self):
        records = [
            {"job": "st-sim-a5", "os": "ubuntu", "path": "github-hosted",
             "runner_tier": "github-standard", "conclusion": "success",
             "wall_seconds": 100, "phases": {"test": 50}, "url": "u1"},
            {"job": "st-sim-a5", "os": "macos", "path": "github-hosted",
             "runner_tier": "github-standard", "conclusion": "success",
             "wall_seconds": 200, "phases": {"test": 80}, "url": "u2"},
        ]
        groups = MODULE.aggregate_records(records)
        self.assertEqual(len(groups), 2)
        self.assertEqual({group["os"] for group in groups}, {"ubuntu", "macos"})


class PublishingTests(unittest.TestCase):
    def test_feishu_hierarchy_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"ci_index_doc": None, "weeks": {}}
            created = iter(("ci-index", "week-doc"))
            with mock.patch.object(MODULE.feishu, "docx_create",
                                   side_effect=lambda *_args: next(created)) as create, \
                    mock.patch.object(MODULE.feishu, "set_doc_link_editable"), \
                    mock.patch.object(MODULE.feishu, "clear_doc"), \
                    mock.patch.object(MODULE.feishu, "docx_append"), \
                    mock.patch.object(MODULE, "doc_root_blocks", return_value=[]):
                first = MODULE.publish_feishu(
                    "# report", "2026-W34", state, state_path, "total-index",
                    "token", "feishu.example")
                second = MODULE.publish_feishu(
                    "# report", "2026-W34", state, state_path, "total-index",
                    "token", "feishu.example")

            self.assertEqual(first, second)
            self.assertEqual(create.call_count, 2)
            saved = json.loads(state_path.read_text())
            self.assertTrue(saved["weeks"]["2026-W34"]["complete"])
            self.assertEqual(saved["ci_index_doc"], "ci-index")

    def test_unowned_index_is_never_rewritten(self):
        state = {"ci_index_doc": "some-existing-doc", "weeks": {}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(MODULE.feishu, "clear_doc") as clear:
            with self.assertRaisesRegex(SystemExit, "unowned"):
                MODULE.publish_feishu(
                    "# report", "2026-W34", state, Path(tmp) / "state.json",
                    "total-index", "token", "feishu.example")
        clear.assert_not_called()

    def test_issue_comment_is_created_once_then_updated(self):
        class Client:
            def __init__(self):
                self.calls = []

            def paginate(self, path):
                self.calls.append(("LIST", path))
                return []

            def request(self, method, path, body):
                self.calls.append((method, path, body))
                return {"id": 123}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {}
            client = Client()
            MODULE.upsert_issue_comment(
                client, "org/repo", 1772, MODULE.COMMENT_MARKER, state, state_path)
            MODULE.upsert_issue_comment(
                client, "org/repo", 1772, MODULE.COMMENT_MARKER, state, state_path)

        methods = [call[0] for call in client.calls]
        self.assertEqual(methods, ["LIST", "POST", "PATCH"])


class CredentialReuseTests(unittest.TestCase):
    def test_existing_environment_token_takes_precedence(self):
        with mock.patch.dict(MODULE.os.environ, {"GH_TOKEN": "existing"},
                             clear=True), \
                mock.patch.object(MODULE.subprocess, "run") as run:
            self.assertEqual(MODULE.resolve_github_token(), "existing")
        run.assert_not_called()

    def test_github_cli_login_is_reused(self):
        completed = MODULE.subprocess.CompletedProcess(
            ["gh", "auth", "token"], 0, stdout="from-gh\n", stderr="")
        with mock.patch.dict(MODULE.os.environ, {}, clear=True), \
                mock.patch.object(MODULE.subprocess, "run",
                                  return_value=completed) as run:
            self.assertEqual(MODULE.resolve_github_token(), "from-gh")
        self.assertEqual(run.call_args.args[0][:3], ["gh", "auth", "token"])


if __name__ == "__main__":
    unittest.main()
