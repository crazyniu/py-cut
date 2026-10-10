"""Read-only VMAF JSON reporting regression."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from pycut import cli
from pycut.commands import dedup_cmd
from pycut.core.quality_vmaf import VMAFResult, VMAFError


class VMAFReportTests(unittest.TestCase):
    def test_report_contains_score_and_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args([
                "dedup", "--check", "--vmaf", "--vmaf-min", "90", "--out-dir", tmp])
            with mock.patch.object(dedup_cmd, "resolve_output_path", return_value=Path(tmp) / "result.mp4"), \
                 mock.patch.object(Path, "is_file", return_value=True), \
                 mock.patch("pycut.core.fingerprint.compare", return_value={"phash_dist": 4, "psnr": 30}), \
                 mock.patch("pycut.core.quality_vmaf.assess", return_value=VMAFResult(92, 12, "3.0")):
                self.assertEqual(dedup_cmd._check_only([Path(tmp) / "input.mp4"], args), 0)
            data = json.loads((Path(tmp) / "vmaf-check.json").read_text())
            self.assertEqual(data["threshold"], 90)
            self.assertEqual(data["checks"][0]["vmaf"]["frames"], 12)
            self.assertEqual(data["checks"][0]["status"], "passed")

    def test_report_records_error_without_faking_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args([
                "dedup", "--check", "--vmaf", "--out-dir", tmp])
            with mock.patch.object(dedup_cmd, "resolve_output_path", return_value=Path(tmp) / "result.mp4"), \
                 mock.patch.object(Path, "is_file", return_value=True), \
                 mock.patch("pycut.core.fingerprint.compare", return_value={"phash_dist": 4, "psnr": 30}), \
                 mock.patch("pycut.core.quality_vmaf.assess", side_effect=VMAFError("no filter")):
                self.assertEqual(dedup_cmd._check_only([Path(tmp) / "input.mp4"], args), 2)
            data = json.loads((Path(tmp) / "vmaf-check.json").read_text())
            self.assertEqual(data["checks"][0]["status"], "failed")
            self.assertEqual(data["checks"][0]["vmaf_error"], "no filter")
            self.assertNotIn("vmaf", data["checks"][0])


if __name__ == "__main__":
    unittest.main()
