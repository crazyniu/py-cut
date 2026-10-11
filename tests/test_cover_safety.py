"""Subtitle covering must preserve audio and refuse invalid / incomplete inputs."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest import mock

from pycut import cli, tui
from pycut.commands import cover_cmd
from pycut.core import scan, subtitle


class _Decoder:
    def __init__(self, frames: list[bytes], returncode: int = 0):
        self.frames = frames.copy()
        self.stdout = self
        self.returncode = returncode

    def read(self, amount):
        return self.frames.pop(0) if self.frames else b""

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


class _Encoder:
    def __init__(self, target: Path):
        self.target = target
        self.returncode = 0
        self.stdin = self
        self.writes = 0

    def write(self, data):
        self.writes += 1
        return len(data)

    def close(self):
        pass

    def wait(self, timeout=None):
        self.target.write_bytes(b"processed" * 180)
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


class CoverSafetyTests(unittest.TestCase):
    def test_cover_keeps_original_audio_with_optional_stream_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "voice.mp4"
            clip.write_bytes(b"original" * 200)
            tmp = clip.with_name("voice.tmp_cover.mp4")
            dec = _Decoder([bytes([30]) * (16 * 16 * 3)])
            enc = _Encoder(tmp)
            probe = {"stream=width": "16", "stream=height": "16",
                     "stream=avg_frame_rate": "30/1"}
            with mock.patch.object(subtitle, "probe_scalar",
                                   side_effect=lambda p, key: probe[key]), \
                 mock.patch.object(subtitle.subprocess, "Popen",
                                   side_effect=[dec, enc]) as popen:
                ok = subtitle.cover_clip(clip, (2, 2, 8, 8))
            self.assertTrue(ok)
            self.assertEqual(enc.writes, 1)
            self.assertEqual(clip.read_bytes(), b"processed" * 180)
            cmd = popen.call_args_list[1].args[0]
            self.assertIn(["-map", "0:v:0", "-map", "1:a:0?"],
                          [cmd[i:i+4] for i in range(len(cmd) - 3)])
            self.assertEqual(cmd[cmd.index("-c:v") + 1],
                             "h264_videotoolbox" if sys.platform == "darwin" else "libx264")
            self.assertIn(["-c:a", "copy"],
                          [cmd[i:i+2] for i in range(len(cmd) - 1)])
            self.assertIn(["-i", str(clip)],
                          [cmd[i:i+2] for i in range(len(cmd) - 1)])

    def test_zero_frames_or_decoder_error_do_not_replace_original(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "voice.mp4"
            original = b"original" * 200
            tmp = clip.with_name("voice.tmp_cover.mp4")
            probe = {"stream=width": "16", "stream=height": "16",
                     "stream=avg_frame_rate": "30/1"}
            for frames, dec_rc in (([], 0),
                                   ([bytes([30]) * (16 * 16 * 3)], 1)):
                clip.write_bytes(original)
                with mock.patch.object(subtitle, "probe_scalar",
                                       side_effect=lambda p, key: probe[key]), \
                     mock.patch.object(subtitle.subprocess, "Popen",
                                       side_effect=[_Decoder(frames, dec_rc),
                                                    _Encoder(tmp)]), \
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
        with mock.patch.object(tui, "_pick_dir", return_value="/a"), \
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
                self.assertEqual(subtitle.process_clip(model, clip), "fail")
            self.assertFalse(subtitle._marker(clip).exists())
            model.ask_vision.assert_not_called()

    def test_verified_negative_replies_can_mark_no_subtitles(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"source")
            model = SimpleNamespace(
                enabled=True, ask_vision=mock.Mock(
                    return_value={"has_subtitle": False, "box": None}))
            info = {"format=duration": "12.0", "stream=width": "720",
                    "stream=height": "1280"}
            with mock.patch.object(subtitle, "probe_scalar",
                                   side_effect=lambda p, entry: info[entry]), \
                 mock.patch.object(subtitle, "_grab_frame", return_value=True), \
                 mock.patch.object(subtitle.cv2, "imread", return_value=object()), \
                 mock.patch.object(subtitle, "log"):
                self.assertEqual(subtitle.process_clip(model, clip), "nosub")
            self.assertTrue(subtitle._marker(clip).is_file())
            self.assertEqual(model.ask_vision.call_count, subtitle.SAMPLES)

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
                    ask_vision=mock.Mock(side_effect=responses))
                with mock.patch.object(subtitle, "probe_scalar",
                                       side_effect=lambda p, entry: info[entry]), \
                     mock.patch.object(subtitle, "_grab_frame", return_value=True), \
                     mock.patch.object(subtitle.cv2, "imread", return_value=object()), \
                     mock.patch.object(subtitle, "log"):
                    self.assertEqual(subtitle.process_clip(model, clip), "fail")
                self.assertFalse(subtitle._marker(clip).exists())

    def test_one_valid_subtitle_detection_remains_actionable(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            clip.write_bytes(b"source")
            info = {"format=duration": "12.0", "stream=width": "720",
                    "stream=height": "1280"}
            model = SimpleNamespace(
                enabled=True, ask_vision=mock.Mock(side_effect=[
                    {"has_subtitle": True, "box": [0.2, 0.6, 0.8, 0.72],
                     "text": "标题"},
                    None, None, None, None] + [{"has_subtitle": False}] * 3))
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
