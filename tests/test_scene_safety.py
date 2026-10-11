"""回归：切片不得丢掉结尾画面，也不能把 FFmpeg 失败误报成无切点。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from pycut import cli, config
from pycut.commands import slice_cmd
from pycut.core import scene


class SceneSafetyTests(unittest.TestCase):
    def test_two_to_fifteen_seconds_survive_short_tail_merge(self):
        for cuts, duration in [([15], 16), ([14, 15.5], 17),
                               ([], 60), ([1, 14.9, 15.1], 31)]:
            with self.subTest(cuts=cuts, duration=duration):
                segments = scene.build_segments(cuts, duration, 2, 15)
                self.assertEqual(segments[0][0], 0)
                self.assertEqual(segments[-1][1], duration)
                self.assertTrue(all(2 <= e - s <= 15 for s, e in segments))
                self.assertTrue(all(a[1] == b[0] for a, b in zip(segments, segments[1:])))

    def test_source_shorter_than_minimum_is_not_a_valid_slice(self):
        self.assertEqual(scene.build_segments([], 1.9, 2, 15), [])

    def test_short_trailing_segment_is_merged_without_losing_end(self):
        # 过去尾段 [3.0, 3.5] 被“合并”成 [0, 3.0]，导致丢掉末尾 0.5s。
        self.assertEqual(scene.build_segments([3.0], 3.5, 2.0, 60.0),
                         [(0.0, 3.5)])
        self.assertEqual(scene.build_segments([2.0, 4.0], 4.5, 2.0, 60.0),
                         [(0.0, 2.0), (2.0, 4.5)])

    def test_each_segment_covers_source_contiguously_including_tail(self):
        examples = [
            ([], 14.0),
            ([0.2, 2.0, 6.1, 9.0], 10.0),
            ([3.0, 6.5, 10.0], 10.1),
            ([1.9, 2.1, 4.7, 7.2], 8.0),
            ([12.5], 16.0),
            ([], 150.5),
        ]
        for cuts, duration in examples:
            with self.subTest(cuts=cuts, duration=duration):
                segments = scene.build_segments(cuts, duration, 2.0, 60.0)
                self.assertTrue(segments)
                self.assertAlmostEqual(segments[0][0], 0.0, places=3)
                self.assertAlmostEqual(segments[-1][1], duration, places=3)
                for (_s0, end), (start, _e1) in zip(segments, segments[1:]):
                    self.assertAlmostEqual(end, start, places=3)
                self.assertAlmostEqual(
                    sum(end - start for start, end in segments),
                    duration, places=3)

    def test_ffmpeg_scene_error_raises_not_empty_cut_list(self):
        with mock.patch.object(scene, "run_cmd",
                               return_value=(1, "", "decoder failed")):
            with self.assertRaisesRegex(RuntimeError, "场景检测失败 rc=1"):
                scene.detect_scenes(Path("bad.mp4"), config.SCENE_TH)
        with mock.patch.object(scene, "run_cmd",
                               return_value=(-1, "", "timeout after 1800s")):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                scene.detect_scenes(Path("hung.mp4"), config.SCENE_TH)

    def test_successful_empty_scene_detection_remains_valid(self):
        with mock.patch.object(scene, "run_cmd", return_value=(0, "", "")):
            self.assertEqual(scene.detect_scenes(Path("single.mp4"), 0.30), [])
        with mock.patch.object(scene, "run_cmd",
                               return_value=(0, "", "[Parsed_showinfo_1] pts_time:3.141")):
            self.assertEqual(scene.detect_scenes(Path("multi.mp4"), 0.30), [3.141])

    def test_one_failed_video_has_nonzero_final_code_and_does_not_abort_batch(self):
        paths = [Path("/tmp/a.mp4"), Path("/tmp/b.mp4")]
        args = cli.build_parser().parse_args(["slice", "--no-sub"])
        result = SimpleNamespace(segs=1, face=1, noface=0, failed=0,
                                 bytes_out=1000, detect_s=0.0, enc_s=0.0,
                                 sub_s=0.0, sub_covered=0, sub_fail=0, sub_review=0,
                                 skipped=False)
        info = lambda p: SimpleNamespace(path=p, stem=p.stem)
        llm = SimpleNamespace(calls=0, failures=0, switched_to=None)
        manifest = mock.Mock(csv_path=Path("/tmp/manifest.csv"))
        with mock.patch.object(slice_cmd, "install_signal_handlers"), \
             mock.patch.object(slice_cmd, "reset_stop"), \
             mock.patch.object(slice_cmd, "self_check", return_value=object()), \
             mock.patch.object(slice_cmd, "load_cascades", return_value=object()), \
             mock.patch.object(slice_cmd, "bind_cascades"), \
             mock.patch.object(slice_cmd, "_pick_files", return_value=paths), \
             mock.patch.object(slice_cmd, "_colliding_stems", return_value=[]), \
             mock.patch.object(slice_cmd, "probe_video", side_effect=info), \
             mock.patch.object(slice_cmd, "make_llm", return_value=llm), \
             mock.patch.object(slice_cmd, "Manifest", return_value=manifest) as mf, \
             mock.patch.object(slice_cmd, "process_video",
                               side_effect=[RuntimeError("scene failed"), result]) as process, \
             mock.patch.object(slice_cmd, "_stopped", return_value=False), \
             mock.patch.object(slice_cmd, "log"):
            self.assertEqual(slice_cmd.run(args), 2)
        self.assertEqual(process.call_count, 2)
        mf.assert_called_once()
        manifest.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
