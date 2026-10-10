"""Pure-logic libvmaf contract tests; no media or FFmpeg required."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from pycut.core import quality_vmaf
from pycut.core.ffmpeg import VideoInfo


class VMAFTests(unittest.TestCase):
    def test_probe_unavailable(self):
        with mock.patch.object(quality_vmaf.subprocess, "run", side_effect=OSError()):
            self.assertFalse(quality_vmaf.libvmaf_available("/missing"))

    def test_probe_filter_list(self):
        result = subprocess.CompletedProcess([], 0, " ... libvmaf V->V Calculate VMAF\n", "")
        with mock.patch.object(quality_vmaf.subprocess, "run", return_value=result):
            self.assertTrue(quality_vmaf.libvmaf_available("ffmpeg"))

    def test_parse_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "score.json"
            path.write_text(json.dumps({"frames": [{"frameNum": 0}],
                "pooled_metrics": {"vmaf": {"mean": 96.25}}, "version": "3.0"}))
            self.assertEqual(quality_vmaf._read_score(path).mean, 96.25)
            path.write_text(json.dumps({"frames": [], "pooled_metrics": {"vmaf": {"mean": 100}}}))
            with self.assertRaises(quality_vmaf.VMAFError):
                quality_vmaf._read_score(path)

    def test_reject_unaligned_without_running_filter(self):
        def info(w):
            return VideoInfo(Path("/tmp/a.mp4"), "a", 3.0, w, 720, False, 0, "1M")
        with mock.patch.object(quality_vmaf, "libvmaf_available", return_value=True), \
             mock.patch.object(quality_vmaf.ffmpeg, "probe_video", side_effect=[info(1280), info(720)]), \
             mock.patch.object(quality_vmaf.subprocess, "run") as runner:
            with self.assertRaisesRegex(quality_vmaf.VMAFError, "dimensions"):
                quality_vmaf.assess(Path("a.mp4"), Path("b.mp4"))
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
