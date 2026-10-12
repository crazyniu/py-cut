"""Subtitle covering must preserve audio and refuse invalid / incomplete inputs."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest import mock

from pycut import cli, config, tui
from pycut.commands import cover_cmd
from pycut.core import scan, subtitle


class CoverSafetyTests(unittest.TestCase):
    def test_cover_keeps_original_audio_with_optional_stream_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "voice.mp4"
            clip.write_bytes(b"original" * 200)
            tmp = clip.with_name("voice.tmp_cover.mp4")
            info = SimpleNamespace(width=16, height=16, duration=1, has_audio=True)
            child = mock.Mock(returncode=0)
            child.poll.return_value = 0
            def launch(*args, **kwargs):
                tmp.write_bytes(b"processed" * 180)
                return child
            with mock.patch("pycut.core.montage_clips._probe_one", return_value=info), \
                 mock.patch.object(subtitle.subprocess, "Popen", side_effect=launch) as popen:
                self.assertTrue(subtitle.cover_clip(clip, (2, 2, 8, 8)))
            self.assertEqual(clip.read_bytes(), b"processed" * 180)
            cmd = popen.call_args.args[0]
            self.assertIn("0:a:0?", cmd)
            self.assertIn(["-c:a", "copy"], [cmd[i:i+2] for i in range(len(cmd)-1)])
            self.assertNotIn("rawvideo", cmd)

    def test_failed_or_missing_output_does_not_replace_original(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "voice.mp4"
            original = b"original" * 200
            tmp = clip.with_name("voice.tmp_cover.mp4")
            info = SimpleNamespace(width=16, height=16, duration=1, has_audio=True)
            for rc, content in ((0, None), (1, b"partial" * 200)):
                clip.write_bytes(original)
                child = mock.Mock(returncode=rc)
                child.poll.return_value = rc
                def launch(*args, **kwargs):
                    if content is not None:
                        tmp.write_bytes(content)
                    return child
                with mock.patch("pycut.core.montage_clips._probe_one", return_value=info), \
                     mock.patch.object(subtitle.subprocess, "Popen", side_effect=launch), \
                     mock.patch.object(subtitle, "log"):
                    self.assertFalse(subtitle.cover_clip(clip, (2, 2, 8, 8)))
                self.assertEqual(clip.read_bytes(), original)
                self.assertFalse(tmp.exists())

    def test_cover_selection_only_includes_existing_mp4(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mp4 = root / "clip.mp4"
            mov = root / "different.mov"
            mp4.write_bytes(b"a")
            mov.write_bytes(b"b")
            options = cli.build_parser().parse_args(
                ["cover", "--src-dir", td])
            self.assertEqual(cover_cmd._pick_clips(options), [mp4])
            opts = cli.build_parser().parse_args(
                ["cover", "--file", str(mp4), str(mp4)])
            self.assertEqual(cover_cmd._pick_clips(opts), [mp4])
            opts = cli.build_parser().parse_args(
                ["cover", "--file", str(mp4), str(mov)])
            with self.assertRaisesRegex(ValueError, "MP4"):
                cover_cmd._pick_clips(opts)

    def test_invalid_cover_options_stop_before_video_processing(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "absent.mp4")
            for options in (
                ["cover", "--file", missing],
                ["cover", "--file", missing, "--workers", "0"],
                ["cover", "--file", missing, "--samples", "0"],
            ):
                args = cli.build_parser().parse_args(options)
                with mock.patch.object(cover_cmd, "self_check") as checked, \
                     mock.patch.object(cover_cmd, "log"):
                    self.assertEqual(cover_cmd.run(args), 2)
                checked.assert_not_called()

    def test_covered_command_returns_interrupted_status(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"test")
            args = cli.build_parser().parse_args(
                ["cover", "--file", str(clip), "--workers", "1"])
            model = SimpleNamespace(calls=0, failures=0, switched_to=None)
            with mock.patch.object(cover_cmd, "install_signal_handlers"), \
                 mock.patch.object(cover_cmd, "reset_stop"), \
                 mock.patch.object(cover_cmd, "self_check"), \
                 mock.patch.object(cover_cmd, "make_llm", return_value=model), \
                 mock.patch.object(cover_cmd.STOP, "flag", True), \
                 mock.patch.object(cover_cmd, "process_clip") as processing, \
                 mock.patch.object(cover_cmd, "log"):
                self.assertEqual(cover_cmd.run(args), 130)
            processing.assert_not_called()

    def test_cover_tui_preview_counts_only_mp4(self):
        files = [Path("/a/first.mp4"), Path("/a/second.mov")]
        def confirm(screen, lines):
            self.assertEqual(lines[-1][1], "1 个")
            return True
        with mock.patch.object(config, "CLIP_DIR", Path("/a")), \
             mock.patch.object(scan, "list_videos_recursive",
                               return_value=files), \
             mock.patch.object(tui, "_confirm", side_effect=confirm):
            self.assertEqual(tui._wizard_cover(object(), 100),
                             [["cover", "--src-dir", "/a"]])


    def test_failed_frame_extraction_does_not_mark_clip_as_clean(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"source")
            model = SimpleNamespace(
                enabled=True, ask_vision=mock.Mock(return_value=None))
            info = {"format=duration": "12.0", "stream=width": "720",
                    "stream=height": "1280"}
            with mock.patch.object(subtitle, "probe_scalar",
                                   side_effect=lambda p, entry: info[entry]), \
                 mock.patch.object(subtitle, "_grab_frame", return_value=False), \
                 mock.patch.object(subtitle, "log"):
                self.assertEqual(subtitle.process_clip(model, clip), "review")
            self.assertFalse(subtitle._marker(clip).exists())
            model.ask_vision.assert_not_called()

    def test_verified_negative_replies_can_mark_no_subtitles(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"source")
            model = SimpleNamespace(
                enabled=True, ask_vision=mock.Mock(
                    return_value={"frames": [{"frame":i,"has_subtitle":False,"box":None} for i in range(subtitle.SAMPLES)]}))
            info = {"format=duration": "12.0", "stream=width": "720",
                    "stream=height": "1280"}
            with mock.patch.object(subtitle, "probe_scalar",
                                   side_effect=lambda p, entry: info[entry]), \
                 mock.patch.object(subtitle, "_grab_frame", return_value=True), \
                 mock.patch.object(subtitle.cv2, "imread", return_value=object()), \
                 mock.patch.object(subtitle, "log"):
                self.assertEqual(subtitle.process_clip(model, clip), "nosub")
            self.assertTrue(subtitle._marker(clip).is_file())
            self.assertEqual(model.ask_vision.call_count, 1)

    def test_partial_llm_failure_and_malformed_positive_do_not_mark_clean(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"source")
            info = {"format=duration": "12.0", "stream=width": "720",
                    "stream=height": "1280"}
            cases = [
                [{"has_subtitle": False}, None, None, None, None],
                [{"has_subtitle": True, "box": None}]
                 + [{"has_subtitle": False}] * 4,
            ]
            for responses in cases:
                model = SimpleNamespace(
                    enabled=True,
                    ask_vision=mock.Mock(return_value={"frames": [dict(r or {}, frame=i) for i,r in enumerate(responses)]}))
                with mock.patch.object(subtitle, "probe_scalar",
                                       side_effect=lambda p, entry: info[entry]), \
                     mock.patch.object(subtitle, "_grab_frame", return_value=True), \
                     mock.patch.object(subtitle.cv2, "imread", return_value=object()), \
                     mock.patch.object(subtitle, "log"):
                    self.assertEqual(subtitle.process_clip(model, clip), "review")
                self.assertFalse(subtitle._marker(clip).exists())

    def test_one_valid_subtitle_detection_remains_actionable(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"source")
            info = {"format=duration": "12.0", "stream=width": "720",
                    "stream=height": "1280"}
            model = SimpleNamespace(
                enabled=True, ask_vision=mock.Mock(side_effect=[
                    {"frames": [{"frame":0,"has_subtitle":True,"box":[.2,.6,.8,.72],"text":"标题"}] + [{"frame":i,"has_subtitle":False} for i in range(1,5)]},
                    {"frames": [{"frame":i,"has_subtitle":False} for i in range(3)]}]))
            with mock.patch.object(subtitle, "probe_scalar",
                                   side_effect=lambda p, entry: info[entry]), \
                 mock.patch.object(subtitle, "_grab_frame", return_value=True), \
                 mock.patch.object(subtitle.cv2, "imread", return_value=object()), \
                 mock.patch.object(subtitle, "cover_clip", return_value=True) as cover, \
                 mock.patch.object(subtitle, "log"):
                self.assertEqual(subtitle.process_clip(model, clip), "covered")
            cover.assert_called_once()
            self.assertTrue(subtitle._marker(clip).is_file())


if __name__ == "__main__":
    unittest.main()
