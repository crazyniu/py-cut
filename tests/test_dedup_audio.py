"""Opt-in dedup audio: mapping, invalid input and atomic publication tests.

No FFmpeg, real videos, network or LLM access is needed.
"""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import cli
from pycut.commands import dedup_cmd
from pycut.core import dedup, dedup_audio
from pycut.core.ffmpeg import VideoInfo


def info(path: Path, *, duration: float = 5.0, audio: bool = True) -> VideoInfo:
    return VideoInfo(path=path, stem=path.stem, duration=duration,
                     width=720, height=1280, has_audio=audio,
                     rotation=0, bitrate="4000k")


class AudioMixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "original.mp4"
        self.video.write_bytes(b"video-input" * 300)
        self.bgm = self.root / "licensed-bgm.mp3"
        self.bgm.write_bytes(b"placeholder")
        self.ambient = self.root / "ambient.wav"
        self.ambient.write_bytes(b"placeholder")
        self.voice = self.root / "narration.wav"
        self.voice.write_bytes(b"placeholder")

    def test_legacy_cli_and_encoding_mapping_unchanged_without_audio(self):
        args = cli.build_parser().parse_args(["dedup", "--file", str(self.video)])
        self.assertEqual((args.bgm, args.ambience, args.voiceover), ("", "", ""))
        self.assertEqual(args.original_gain, 1.0)
        self.assertFalse(dedup_audio.AudioMixOptions().enabled)
        params = {"crf": 20, "gop": 60, "abitrate": "128k", "seed": 3}
        original_opts = dedup.DedupOptions()
        with mock.patch.object(dedup, "build_video_chain", return_value=["hflip"]):
            cmd, _ = dedup._build_cmd(
                self.video, self.root / "result.mp4", params,
                info(self.video), original_opts, "")
        self.assertIn("0:a?", cmd)
        self.assertIn("0:v:0", cmd)
        self.assertIn("-af", cmd) if "-af" in cmd else self.assertNotIn("-filter_complex", cmd)
        self.assertNotIn("-stream_loop", cmd)

    def test_bgm_and_ambience_with_original_track_map_one_audio(self):
        opts = dedup_audio.AudioMixOptions(
            bgm=str(self.bgm), ambience=str(self.ambient),
            bgm_gain=0.2, ambience_gain=0.35, fade_out=1.0)
        cmd = dedup_audio.build_audio_mux_cmd(
            self.video, self.root / "out.mp4", duration=7.5,
            video_has_audio=True, options=opts)
        filt = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(cmd.count("-stream_loop"), 2)
        self.assertEqual(cmd.count("-map"), 2)
        self.assertEqual(cmd[cmd.index("-map")+1], "0:v:0")
        self.assertIn("[aout]", cmd)
        self.assertEqual(cmd[cmd.index("-c:v")+1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a")+1], "aac")
        self.assertIn("[0:a:0]", filt)
        self.assertIn("[1:a:0]", filt)
        self.assertIn("[2:a:0]", filt)
        self.assertIn("amix=inputs=3:duration=longest:normalize=0", filt)
        self.assertIn("afade=t=out:st=6.500000:d=1.000000", filt)
        self.assertIn("atrim=duration=7.500000", filt)

    def test_revoice_replaces_original_and_pads_short_voice(self):
        opts = dedup_audio.AudioMixOptions(
            bgm=str(self.bgm), voiceover=str(self.voice))
        cmd = dedup_audio.build_audio_mux_cmd(
            self.video, self.root / "out.mp4", duration=8.0,
            video_has_audio=True, options=opts)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("[0:a:0]", graph)
        self.assertIn("[1:a:0]", graph)
        self.assertIn("[2:a:0]", graph)
        self.assertIn("amix=inputs=2:", graph)
        self.assertIn("apad", graph)
        self.assertEqual(cmd.count("-stream_loop"), 1)
        self.assertEqual(cmd.count("-map"), 2)
        self.assertEqual(cmd[cmd.index("-t")+1], "8.000000")

    def test_silent_video_can_gain_audio_from_bgm_only(self):
        opts = dedup_audio.AudioMixOptions(bgm=str(self.bgm))
        cmd = dedup_audio.build_audio_mux_cmd(
            self.video, self.root / "out.mp4", duration=5,
            video_has_audio=False, options=opts)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("[0:a:0]", graph)
        self.assertIn("[1:a:0]", graph)
        self.assertIn("anull", graph)
        with self.assertRaisesRegex(dedup_audio.AudioMixError, "没有音轨"):
            dedup_audio.build_audio_mux_cmd(
                self.video, self.root / "out.mp4", duration=5,
                video_has_audio=False,
                options=dedup_audio.AudioMixOptions(fade_out=0.5))

    def test_audio_option_validation_rejects_missing_invalid_and_check_only(self):
        cases = [
            dedup_audio.AudioMixOptions(bgm=str(self.root / "missing.mp3")),
            dedup_audio.AudioMixOptions(original_gain=-1),
            dedup_audio.AudioMixOptions(bgm_gain=0.4),
            dedup_audio.AudioMixOptions(voice_gain=0.2),
            dedup_audio.AudioMixOptions(fade_out=11),
            dedup_audio.AudioMixOptions(original_gain=math.inf),
            dedup_audio.AudioMixOptions(voiceover=str(self.voice), original_gain=0.7),
        ]
        for opt in cases:
            with self.subTest(option=opt), self.assertRaises(dedup_audio.AudioMixError):
                dedup_audio.validate_audio_options(opt)
        with self.assertRaises(dedup_audio.AudioMixError):
            dedup_audio.validate_audio_options(
                dedup_audio.AudioMixOptions(bgm=str(self.bgm)), check_only=True)

    def test_missing_or_ambiguous_audio_stream_rejected_before_encode(self):
        opt = dedup_audio.AudioMixOptions(bgm=str(self.bgm))
        for streams in ([], [{"channels": 2}, {"channels": 2}],
                        [{"channels": 0}], [{"channels": None}],
                        [{"channels": 9}]):
            with self.subTest(streams=streams), \
                 mock.patch.object(dedup_audio, "_probe_audio_streams",
                                   return_value=streams), \
                 self.assertRaises(dedup_audio.AudioMixError):
                dedup_audio.preflight_external_audio(opt)
        with mock.patch.object(dedup_audio, "_probe_audio_streams",
                               return_value=[{"channels": 2, "duration": "5"}]):
            dedup_audio.preflight_external_audio(opt)

    def test_error_before_self_check_with_invalid_cli_audio(self):
        args = cli.build_parser().parse_args([
            "dedup", "--file", str(self.video), "--bgm", str(self.root / "absent.mp3")])
        with mock.patch.object(dedup_cmd, "self_check") as check, \
             mock.patch.object(dedup_cmd, "log"):
            self.assertEqual(dedup_cmd.run(args), 2)
        check.assert_not_called()

    def test_mux_failure_never_replaces_staged_file(self):
        opts = dedup_audio.AudioMixOptions(original_gain=0.5)
        original = self.video.read_bytes()
        with mock.patch.object(dedup_audio, "probe_video", return_value=info(self.video)), \
             mock.patch.object(dedup_audio, "_require_one_audio",
                               return_value={"channels": 2, "duration": "5.0"}), \
             mock.patch.object(dedup_audio, "run_cmd",
                               return_value=(1, "", "fake mux failure")):
            with self.assertRaisesRegex(dedup_audio.AudioMixError, "混合失败"):
                dedup_audio.mix_audio_file(self.video, opts)
        self.assertEqual(self.video.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.audio_tmp*")))

    def test_mux_rejects_audio_video_duration_mismatch_without_replacing(self):
        opts = dedup_audio.AudioMixOptions(original_gain=0.4)
        original = self.video.read_bytes()
        def fake_mux(cmd, timeout):
            Path(cmd[-1]).write_bytes(b"bad-audio-output" * 200)
            return (0, "", "")
        with mock.patch.object(dedup_audio, "probe_video", return_value=info(self.video)), \
             mock.patch.object(dedup_audio, "_require_one_audio",
                               return_value={"channels": 2, "duration": "1.0"}), \
             mock.patch.object(dedup_audio, "run_cmd", side_effect=fake_mux):
            with self.assertRaisesRegex(dedup_audio.AudioMixError, "时长不一致"):
                dedup_audio.mix_audio_file(self.video, opts)
        self.assertEqual(self.video.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.audio_tmp*")))

    def test_verified_mux_only_replaces_staging_file(self):
        opts = dedup_audio.AudioMixOptions(original_gain=0.6)
        def fake_mux(cmd, timeout):
            Path(cmd[-1]).write_bytes(b"verified-output" * 300)
            return (0, "", "")
        with mock.patch.object(dedup_audio, "probe_video", return_value=info(self.video)), \
             mock.patch.object(dedup_audio, "_require_one_audio",
                               return_value={"channels": 2, "duration": "5.0"}), \
             mock.patch.object(dedup_audio, "run_cmd", side_effect=fake_mux):
            result = dedup_audio.mix_audio_file(self.video, opts)
        self.assertTrue(self.video.read_bytes().startswith(b"verified-output"))
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["audio_duration"], 5.0)

    def test_pipeline_does_not_replace_existing_output_if_mux_fails(self):
        dst = self.root / "out.mp4"
        dst.write_bytes(b"previous-good-output")
        audio = dedup_audio.AudioMixOptions(original_gain=0.6)
        options = dedup.DedupOptions(audio_mix=audio, force=True)
        def fake_encode(cmd, timeout):
            Path(cmd[-1]).write_bytes(b"temporary-video" * 300)
            return 0, "", ""
        with mock.patch.object(dedup, "probe_video", return_value=info(self.video)), \
             mock.patch.object(dedup, "_video_duration", return_value=5.0), \
             mock.patch.object(dedup, "_build_cmd",
                               side_effect=lambda src, tmp, *_: (["fake", str(tmp)], [])), \
             mock.patch.object(dedup, "run_cmd", side_effect=fake_encode), \
             mock.patch.object(dedup, "mix_audio_file",
                               side_effect=dedup_audio.AudioMixError("bad stream")):
            report = dedup.process_one(self.video, dst, options)
        self.assertFalse(report["ok"])
        self.assertIn("bad stream", report["error"])
        self.assertEqual(dst.read_bytes(), b"previous-good-output")
        self.assertFalse(list(self.root.glob("*.dedup_tmp*")))


if __name__ == "__main__":
    unittest.main()
