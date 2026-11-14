#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""SDK release metadata must describe the actual package revision."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "build_package", Path(__file__).resolve().parents[1] / "ci/build-package.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackageMetadata(unittest.TestCase):
    def test_actual_sdk_revision_survives_release_metadata(self):
        for release, package in (("1.1.0-rc.1", "1.1.0_rc1-r2"),
                                 ("1.1.0", "1.1.0-r1"),
                                 ("1.1.0-beta.2", "1.1.0_beta2-r12")):
            with self.subTest(package=package):
                dumped = "# ADB v0.0\npkginfo:\n  name: wrtix-agent\n  version: " + package + "\n  arch: noarch\n"
                self.assertEqual(builder.sdk_package_version(dumped, release), package)

    def test_wrong_upstream_or_missing_revision_fails_closed(self):
        for dumped in ("version: 1.0.0-r2\n", "version: 1.1.0\n", "version: 1.1.0-r0\n", "arch: noarch\n"):
            with self.subTest(dumped=dumped), self.assertRaises(RuntimeError):
                builder.sdk_package_version(dumped, "1.1.0")


if __name__ == "__main__":
    unittest.main()
