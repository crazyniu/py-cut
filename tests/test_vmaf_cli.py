"""VMAF CLI opt-in contract tests without real media."""
import unittest
from unittest import mock
from pathlib import Path
from pycut import cli
from pycut.commands import dedup_cmd
from pycut.core.quality_vmaf import VMAFError, VMAFResult


class VMAFCLITests(unittest.TestCase):
    def test_opt_in_required_for_threshold(self):
        args = cli.build_parser().parse_args(["dedup", "--check", "--vmaf-min", "80"])
        with mock.patch.object(dedup_cmd, "self_check") as check:
            self.assertEqual(dedup_cmd.run(args), 2)
            check.assert_not_called()

    def test_read_only_required(self):
        args = cli.build_parser().parse_args(["dedup", "--vmaf"])
        with mock.patch.object(dedup_cmd, "self_check") as check:
            self.assertEqual(dedup_cmd.run(args), 2)
            check.assert_not_called()

    def test_check_rejects_unavailable_vmaf(self):
        args = cli.build_parser().parse_args(["dedup", "--check", "--vmaf"])
        with mock.patch.object(dedup_cmd, "resolve_output_path", return_value=Path("/tmp/vmaf-result.mp4")), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch("pycut.core.fingerprint.compare", return_value={"phash_dist": 4, "psnr": 30}), \
             mock.patch("pycut.core.quality_vmaf.assess", side_effect=VMAFError("unavailable")):
            self.assertEqual(dedup_cmd._check_only([Path("/tmp/source.mp4")], args), 2)

    def test_check_applies_explicit_threshold(self):
        args = cli.build_parser().parse_args(["dedup", "--check", "--vmaf", "--vmaf-min", "95"])
        with mock.patch.object(dedup_cmd, "resolve_output_path", return_value=Path("/tmp/vmaf-result.mp4")), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch("pycut.core.fingerprint.compare", return_value={"phash_dist": 4, "psnr": 30}), \
             mock.patch("pycut.core.quality_vmaf.assess", return_value=VMAFResult(92, 100, "3.0")):
            self.assertEqual(dedup_cmd._check_only([Path("/tmp/source.mp4")], args), 2)


if __name__ == "__main__":
    unittest.main()
