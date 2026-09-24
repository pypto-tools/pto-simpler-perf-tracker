#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "feishu_perf_report", ROOT / "feishu_perf_report.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublishSelectionTests(unittest.TestCase):
    def test_unmeasured_commit_is_not_deduplicated(self):
        entries = [
            {"sha": "good", "rc": 0, "metrics": {"case": {"Device": 1.0}}},
            {"sha": "partial", "rc": 1,
             "metrics": {"case": {"Device": 1.5}}},
            {"sha": "failed", "rc": 1, "metrics": {}},
            {"sha": "old", "rc": 0, "metrics": {"case": {"Device": 2.0}}},
            {"sha": "host-failed", "rc": 0,
             "metrics": {"case": {"Device": 3.0}},
             "host": {"status": "failed", "cases": {}}},
        ]
        fresh = MODULE.unpublished_with_metrics(entries, {"old"})
        self.assertEqual([entry["sha"] for entry in fresh], ["good"])

    def test_host_only_commit_is_publishable(self):
        entry = {
            "sha": "host-only", "rc": 0, "metrics": {},
            "device_status": "disabled",
            "host": {"status": "ok", "cases": {}},
        }
        self.assertEqual(
            MODULE.unpublished_with_metrics([entry], set()), [entry])

    def test_host_bind_tables_are_rendered_for_each_case(self):
        host_case = {
            "status": "ok", "device": "1", "binds": 6, "warm_binds": 5,
            "metrics": {
                "control_plane": {
                    "min_us": 1000.0, "median_us": 1100.0,
                    "max_us": 1200.0,
                },
            },
        }
        entry = {
            "sha": "a" * 40, "subject": "fixture", "date": "2026-08-31",
            "device": "1", "metrics": {"case": {"Device": 2.0}},
            "present": ["Device"],
            "host": {"status": "ok", "cases": {
                "qwen3-14b": host_case,
                "dsv4-flash": host_case,
            }},
        }

        _top, descendants = MODULE._commit_descendants(entry, None, [0])
        rendered = json.dumps(descendants, ensure_ascii=False)
        self.assertIn("Host bind · qwen3-14b", rendered)
        self.assertIn("Host bind · dsv4-flash", rendered)
        self.assertIn("control_plane", rendered)

    def test_host_only_commit_keeps_host_tables(self):
        entry = {
            "sha": "b" * 40, "subject": "host only", "date": "2026-08-31",
            "device_status": "disabled", "metrics": {}, "present": [],
            "host": {"status": "ok", "cases": {"qwen3-14b": {
                "status": "ok", "device": "1", "binds": 6,
                "warm_binds": 5,
                "metrics": {"control_plane": {
                    "min_us": 100.0, "median_us": 110.0, "max_us": 120.0,
                }},
            }}},
        }

        _top, descendants = MODULE._commit_descendants(entry, None, [0])
        rendered = json.dumps(descendants, ensure_ascii=False)
        self.assertIn("Device benchmark disabled", rendered)
        self.assertIn("Host bind · qwen3-14b", rendered)


class DirectConnectionTests(unittest.TestCase):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"code": 0, "data": {"ok": True}}).encode()

    def test_feishu_api_uses_proxy_free_opener(self):
        proxy_env = {
            "HTTP_PROXY": "http://proxy.invalid:8080",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "http_proxy": "http://proxy.invalid:8080",
            "https_proxy": "http://proxy.invalid:8080",
        }
        with mock.patch.dict(os.environ, proxy_env, clear=False), \
                mock.patch.object(
                    MODULE._FEISHU_OPENER, "open",
                    return_value=self.Response()) as direct_open, \
                mock.patch.object(
                    MODULE.urllib.request, "urlopen",
                    side_effect=AssertionError("default proxy-aware opener used")):
            payload = MODULE._api(
                "GET", "https://open.feishu.cn/open-apis/test", retries=1)

        self.assertEqual(payload["data"], {"ok": True})
        direct_open.assert_called_once()
        request = direct_open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://open.feishu.cn/open-apis/test")

    def test_code_blocks_do_not_force_wrap(self):
        block = MODULE._code_block("a very wide report")
        self.assertFalse(block["code"]["style"]["wrap"])


class MonthIndexTests(unittest.TestCase):
    def test_month_index_is_rebuilt_newest_first(self):
        months = {
            "2026-06": "jun-doc",
            "2026-05": "may-doc",
            "2026-08": "aug-doc",
            "2026-07": "jul-doc",
        }
        with mock.patch.object(MODULE, "clear_doc") as clear, \
                mock.patch.object(MODULE, "docx_append") as append:
            MODULE.sync_month_index("token", "index-doc", months,
                                    "feishu.example")

        clear.assert_called_once_with("token", "index-doc")
        append.assert_called_once()
        blocks = append.call_args.args[2]
        labels = [b["text"]["elements"][0]["text_run"]["content"]
                  for b in blocks]
        self.assertEqual(labels, [
            "📅 2026-08 → https://feishu.example/docx/aug-doc",
            "📅 2026-07 → https://feishu.example/docx/jul-doc",
            "📅 2026-06 → https://feishu.example/docx/jun-doc",
            "📅 2026-05 → https://feishu.example/docx/may-doc",
        ])

    def test_month_index_lists_newest_volume_first(self):
        with mock.patch.object(MODULE, "clear_doc"), \
                mock.patch.object(MODULE, "docx_append") as append:
            MODULE.sync_month_index(
                "token", "index-doc", {"2026-09": "part-1"},
                "feishu.example",
                {"2026-09": ["part-1", "part-2"]})

        blocks = append.call_args.args[2]
        labels = [b["text"]["elements"][0]["text_run"]["content"]
                  for b in blocks]
        self.assertEqual(labels, [
            "📅 2026-09 · 卷 2 → https://feishu.example/docx/part-2",
            "📅 2026-09 · 卷 1 → https://feishu.example/docx/part-1",
        ])


class MultipartPublishTests(unittest.TestCase):
    @staticmethod
    def entry(sha, date="2026-09-23"):
        return {
            "sha": sha,
            "date": date,
            "subject": sha,
            "rc": 0,
            "metrics": {"case": {"Device": 1.0}},
            "present": ["Device"],
        }

    def write_legacy_state(self, directory):
        path = Path(directory) / "work" / "feishu_state.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "index_doc": "index-doc",
            "months": {"2026-09": "legacy-doc"},
            "pushed": ["old"],
        }))
        return path

    def publish_patches(self):
        return (
            mock.patch.object(MODULE, "docx_create", return_value="part-2"),
            mock.patch.object(MODULE, "set_doc_link_editable"),
            mock.patch.object(MODULE, "docx_append_descendant"),
            mock.patch.object(MODULE, "clear_doc"),
            mock.patch.object(MODULE, "docx_append"),
            mock.patch.object(MODULE, "docx_root_sha_prefixes",
                              return_value=set()),
            mock.patch("report_hub.sync_report_hub"),
        )

    def test_legacy_month_is_sealed_and_new_commit_uses_part_two(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_legacy_state(directory)
            patches = self.publish_patches()
            with patches[0] as create, patches[1], patches[2] as insert, \
                    patches[3], patches[4], patches[5], patches[6]:
                MODULE.publish_monthly(
                    "token", [self.entry("new")], state_path,
                    domain="feishu.example")

            state = json.loads(state_path.read_text())
            self.assertEqual(state["month_parts"]["2026-09"],
                             ["legacy-doc", "part-2"])
            self.assertIn("legacy-doc", state["sealed_docs"])
            self.assertIn("new", state["pushed"])
            self.assertGreater(state["doc_blocks"]["part-2"], 0)
            create.assert_called_once()
            self.assertEqual(insert.call_args.args[1], "part-2")

    def test_each_successful_commit_is_checkpointed_before_next_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_legacy_state(directory)
            patches = self.publish_patches()
            with patches[0], patches[1], patches[2] as insert, \
                    patches[3], patches[4], patches[5], patches[6]:
                insert.side_effect = [{}, RuntimeError("second write failed")]
                with self.assertRaisesRegex(RuntimeError, "second write failed"):
                    MODULE.publish_monthly(
                        "token", [self.entry("newest"), self.entry("older")],
                        state_path, domain="feishu.example")

            state = json.loads(state_path.read_text())
            self.assertIn("newest", state["pushed"])
            self.assertNotIn("older", state["pushed"])

    def test_legacy_remote_commit_is_reconciled_without_duplicate_write(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_legacy_state(directory)
            patches = list(self.publish_patches())
            patches[5] = mock.patch.object(
                MODULE, "docx_root_sha_prefixes", return_value={"abcdef1234"})
            with patches[0] as create, patches[1], patches[2] as insert, \
                    patches[3], patches[4], patches[5], patches[6]:
                MODULE.publish_monthly(
                    "token", [self.entry("abcdef1234567890")], state_path,
                    domain="feishu.example")

            state = json.loads(state_path.read_text())
            self.assertIn("abcdef1234567890", state["pushed"])
            self.assertIn("legacy-doc", state["reconciled_docs"])
            create.assert_not_called()
            insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
