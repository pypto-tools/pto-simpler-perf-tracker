#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_daily_report", ROOT / "ci_daily_report.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PatternTests(unittest.TestCase):
    def test_signature_normalises_dynamic_values(self):
        one = MODULE.failure_signature(
            "2026-09-20T01:02:03Z ERROR run 12345 commit abcdef123456\n"
            "password=secret-value\nTimeout while waiting for /tmp/build-123")
        two = MODULE.failure_signature(
            "2026-09-21T04:05:06Z ERROR run 98765 commit abcdef123456\n"
            "password=other-value\nTimeout while waiting for /tmp/build-456")
        self.assertEqual(one["pattern_id"], two["pattern_id"])
        self.assertNotIn("secret-value", json.dumps(one))


class ReportTests(unittest.TestCase):
    def test_report_with_no_findings_is_renderable(self):
        report = MODULE.render_report("2026-09-20", [], [], [])
        self.assertIn("无问题", report)
        self.assertIn("无失败详情", report)

    def test_report_has_resolved_and_pending_sections(self):
        pattern = {"pattern_id": "deadbeef", "summary": "Timeout",
                   "evidence": ["ERROR: worker exited", "device timeout"]}
        report = MODULE.render_report(
            "2026-09-20",
            [{"job": "st-sim-a5", "run_attempt": 1, "pattern": pattern,
              "run_url": "https://example/fail"}],
            [{"pattern_id": "deadbeef", "pattern": pattern,
              "failed": {"job": "st-sim-a5", "run_attempt": 1,
                          "run_url": "https://example/fail"},
              "resolved": {"run_attempt": 2}}])
        self.assertIn("## 问题汇总", report)
        self.assertIn("重跑恢复", report)
        self.assertIn("待复查", report)
        self.assertIn("状态", report)
        self.assertIn("```text", report)
        self.assertIn("deadbeef", report)
        self.assertIn("## 失败详情", report)
        self.assertIn("ERROR: worker exited", report)
        self.assertIn("https://example/fail", report)

    def test_report_keeps_multiple_evidence_lines_in_summary(self):
        pattern = {"pattern_id": "p", "summary": "fatal error",
                   "evidence": ["traceback line", "assertion details"]}
        report = MODULE.render_report(
            "2026-09-20",
            [{"job": "st-sim-a5", "failed_step": "pytest", "run_id": 7,
              "pattern": pattern}], [], [])
        self.assertIn("fatal error \\| traceback line \\| assertion details", report)

    def test_common_pattern_requires_multiple_pull_requests(self):
        pattern = {"pattern_id": "shared", "summary": "device timeout"}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "2026-09-19.json").write_text(json.dumps({
                "failures": [{"run_id": 1, "pr_number": 101, "job": "st-sim-a5",
                              "pattern": pattern}], "resolved": []}))
            common = MODULE.summarize_patterns(
                state, "2026-09-20",
                [{"run_id": 2, "pr_number": 102, "job": "st-sim-a5",
                  "pattern": pattern}], [])
        self.assertEqual(common[0]["pr_count"], 2)
        self.assertEqual(common[0]["run_count"], 2)

    def test_common_pattern_falls_back_to_summary_when_signatures_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = MODULE.summarize_patterns(
                Path(tmp), "2026-09-20", [
                    {"run_id": 1, "pr_number": 101, "job": "st-sim-a5",
                     "pattern": {"pattern_id": "one", "summary": "timeout"}},
                    {"run_id": 2, "pr_number": 102, "job": "st-sim-a5",
                     "pattern": {"pattern_id": "two", "summary": "timeout"}},
                ], [])
        self.assertEqual(common[0]["pr_count"], 2)
        self.assertTrue(common[0]["pattern_id"].startswith("summary-"))

    def test_empty_common_pattern_section_explains_why(self):
        report = MODULE.render_report("2026-09-20", [], [], [])
        self.assertIn("没有同一失败模式出现在多个 PR", report)


class ScanTests(unittest.TestCase):
    def test_failed_attempt_followed_by_success_is_resolved(self):
        run = {"id": 7, "run_attempt": 2, "head_sha": "abc",
               "html_url": "https://example/run/7"}
        failed = {"run_id": 7, "run_attempt": 1, "head_sha": "abc",
                  "run_url": run["html_url"], "job_id": 10, "job": "st-sim-a5",
                  "conclusion": "failure", "pattern": {"pattern_id": "p", "summary": "x"}}
        success = {**failed, "run_attempt": 2, "job_id": 11, "conclusion": "success"}
        client = mock.Mock()
        with mock.patch.object(MODULE, "list_day_runs", return_value=[run]), \
                mock.patch.object(MODULE, "_attempt_records", return_value=[failed, success]):
            failures, resolved = MODULE.scan_day(client, "o/r", "ci.yml", "2026-09-20", [])
        self.assertEqual(failures, [])
        self.assertEqual(resolved[0]["status"], "resolved_by_rerun")


class PublishingTests(unittest.TestCase):
    def test_daily_reports_are_partitioned_by_month(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(MODULE.os.environ, {
                    "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
                }, clear=False), \
                mock.patch("feishu_perf_report.tenant_token", return_value="token"), \
                mock.patch("feishu_perf_report.docx_create",
                           side_effect=["index-doc", "month-doc"]), \
                mock.patch("feishu_perf_report.set_doc_link_editable"), \
                mock.patch("feishu_perf_report.docx_append"), \
                mock.patch("feishu_perf_report.clear_doc"):
            state_dir = Path(tmp)
            url = MODULE.publish_daily_report(state_dir, "2026-09-20", "# report")

            state = json.loads((state_dir / "feishu-state.json").read_text())
            self.assertEqual(state["index_doc"], "index-doc")
            self.assertEqual(state["weeks"]["2026-W38"]["doc_id"], "month-doc")
            self.assertTrue(state["weeks"]["2026-W38"]["days"]["2026-09-20"])
            self.assertTrue(url.endswith("/docx/index-doc"))

    def test_already_published_day_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(MODULE.os.environ, {
                    "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
                }, clear=False), \
                mock.patch("feishu_perf_report.tenant_token") as token:
            state_dir = Path(tmp)
            (state_dir / "feishu-state.json").write_text(json.dumps({
                "index_doc": "index-doc", "published_days": {"2026-09-20": True},
            }))
            MODULE.publish_daily_report(state_dir, "2026-09-20", "# report")
            token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
