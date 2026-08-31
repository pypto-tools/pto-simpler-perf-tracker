#!/usr/bin/env python3
import unittest

import perf_finalize


class HostMarkdownTests(unittest.TestCase):
    def test_host_only_entry_is_a_complete_measurement(self):
        entry = {
            "rc": 0, "summary": "", "device_status": "disabled",
            "host": {"status": "ok", "cases": {}},
        }
        self.assertTrue(perf_finalize.entry_complete(entry))

    def test_host_only_entry_requires_successful_host_data(self):
        entry = {
            "rc": 0, "summary": "", "device_status": "disabled",
            "host": {"status": "failed", "cases": {}},
        }
        self.assertFalse(perf_finalize.entry_complete(entry))

    def test_host_delta_uses_control_plane_minimum(self):
        current = {
            "status": "ok",
            "cases": {
                "qwen3-14b": {
                    "status": "ok", "device": "1", "binds": 6,
                    "warm_binds": 5,
                    "metrics": {
                        "control_plane": {
                            "min_us": 1100.0, "median_us": 1500.0,
                            "max_us": 2000.0,
                        },
                    },
                },
            },
        }
        previous = {
            "status": "ok",
            "cases": {
                "qwen3-14b": {
                    "status": "ok", "device": "1",
                    "metrics": {
                        "control_plane": {
                            "min_us": 1000.0, "median_us": 1400.0,
                            "max_us": 1800.0,
                        },
                    },
                },
            },
        }

        report = "\n".join(perf_finalize.host_tables_md(current, previous))
        self.assertIn("1100.0 (+10.0% 🔺)", report)

    def test_cross_device_host_delta_is_suppressed(self):
        current = {
            "cases": {"dsv4-flash": {
                "status": "ok", "device": "3,4", "binds": 12,
                "warm_binds": 10,
                "metrics": {"control_plane": {
                    "min_us": 5000.0, "median_us": 5500.0,
                    "max_us": 6000.0,
                }},
            }},
        }
        previous = {
            "cases": {"dsv4-flash": {
                "status": "ok", "device": "5,6",
                "metrics": {"control_plane": {"min_us": 4000.0}},
            }},
        }

        report = "\n".join(perf_finalize.host_tables_md(current, previous))
        self.assertIn("| control_plane | 5000.0 |", report)
        self.assertNotIn("+25.0%", report)


if __name__ == "__main__":
    unittest.main()
