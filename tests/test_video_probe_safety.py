"""Malformed video metadata must not enter slice/montage encoding paths."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut.core import ffmpeg, montage_clips, montage_source


class VideoProbeSafetyTests(unittest.TestCase):
    def test_video_probe_rejects_zero_dimensions_with_valid_duration(self):
        cases = [(0, 1280), (720, 0), (0, 0)]
        for width, height in cases:
            with self.subTest(width=width, height=height):
                payload = json.dumps({"streams": [{
                    "width": width, "height": height, "duration": "12.0",
                }], "format": {"duration": "12.0"}})
                with mock.patch.object(ffmpeg, "run_cmd",
                                       return_value=(0, payload, "")) as runner:
                    self.assertIsNone(ffmpeg.probe_video(Path("invalid.mp4")))
                    runner.assert_called_once()

    def test_valid_video_metadata_is_accepted(self):
        payload = json.dumps({"streams": [{
            "width": 720, "height": 1280, "duration": "12.0",
        }], "format": {"duration": "12.0"}})
        with mock.patch.object(ffmpeg, "run_cmd",
                               side_effect=[(0, payload, ""), (0, "", "")]):
            info = ffmpeg.probe_video(Path("valid.mp4"))
        self.assertEqual((info.width, info.height), (720, 1280))
        self.assertFalse(info.has_audio)

    def test_montage_rejects_bad_dimensions_before_audio_probe(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "damaged.mp4"
            src.write_bytes(b"not a real video")
            data = json.dumps({"streams": [{"codec_type": "video", "width": 720,
                               "height": 0, "duration": "12.0"}, {"codec_type": "audio"}],
                               "format": {"duration": "12.0"}})
            with mock.patch.object(montage_clips, "run_cmd", return_value=(0, data, "")) as probe, \
                 mock.patch.object(montage_source, "run_cmd") as runner, \
                 mock.patch.object(montage_source, "log"):
                self.assertIsNone(montage_source.probe_source(src))
            probe.assert_called_once()
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
