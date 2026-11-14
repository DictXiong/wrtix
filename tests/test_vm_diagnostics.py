#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Public VM failure summaries exclude private controller output."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/vm"))
from controller_acceptance import failure_summary


class Diagnostics(unittest.TestCase):
    def test_only_known_errors_leave_the_private_workdir(self):
        stderr = (b"openwrt-deploy: target reboot not verified; automatic rollback remains armed\n"
                  b"openwrt-deploy: remote operation failed: post_reboot_readback_failed\n"
                  b"openwrt-deploy: remote operation failed: synthetic_private_value\n"
                  b"openwrt-deploy: password synthetic-private-value\n")
        result = subprocess.CompletedProcess([], 1, b"synthetic-private-value", stderr)
        report = failure_summary("restore-mac", "deploy", result)
        self.assertEqual(report["agent_errors"], ["post_reboot_readback_failed"])
        self.assertEqual(report["controller_errors"], ["target reboot not verified; automatic rollback remains armed"])
        self.assertNotIn("synthetic", str(report))


if __name__ == "__main__":
    unittest.main()
