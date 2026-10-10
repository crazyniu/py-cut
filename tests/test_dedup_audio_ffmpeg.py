"""Real FFmpeg contract test using fully synthetic non-user media.

Tests the actual dedup_audio Python mixer, not a separately reimplemented
equivalent filter string. Skips only when ffmpeg/ffprobe executables are absent.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pycut.core import dedup_audio, ffmpeg


class AudioFfmpegIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.encoder = shutil.which("ffmpeg")
        self.probe = shutil.which("ffprobe")
        if not self.encoder or not self.probe:
            self.skipTest("ffmpeg/ffprobe are not installed (device test is opt-in)")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clip = self.root / "clip.mp4"
        self.silent = self.root / "silent.mp4"
        self.bgm = self.root / "bgm.wav"
        self.voice = self.root / "voice.wav"
        self._create([
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-t", "2", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(self.clip)])
        self._create([
            "-f", "lavfi", "-i", "color=c=red:s=320x240:r=25",
            "-t", "2", "-c:v", "mpeg4", "-an", str(self.silent)])
        self._create([
            "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=0.5",
            str(self.bgm)])
        self._create([
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=0.8",
            str(self.voice)])

    def _create(self, args):
        command = [self.encoder, "-y", "-hide_banner", "-loglevel", "error",
                   "-nostdin", *args]
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=40)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode("utf-8", "replace"))

    def _run_real_mixer(self, path, options):
        # dedup_audio uses its own FFmpeg paths, while probe_video is an
        # imported function reading the ffmpeg module's FFPROBE global.
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(dedup_audio, "FFMPEG", self.encoder))
            stack.enter_context(mock.patch.object(dedup_audio, "FFPROBE", self.probe))
            stack.enter_context(mock.patch.object(ffmpeg, "FFPROBE", self.probe))
            return dedup_audio.mix_audio_file(path, options)

    def test_voiceover_and_looped_bgm_preserve_video_and_two_second_sync(self):
        before = self.clip.stat().st_size
        result = self._run_real_mixer(
            self.clip, dedup_audio.AudioMixOptions(
                bgm=str(self.bgm), voiceover=str(self.voice), fade_out=0.25))
        self.assertGreater(before, 1024)
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["channels"], 2)
        self.assertLess(abs(result["audio_duration"] - result["video_duration"]), 0.30)
        self.assertLess(abs(result["video_duration"] - 2.0), 0.30)
        self.assertTrue(self.clip.is_file())
        self.assertFalse(list(self.root.glob("*.audio_tmp*")))

    def test_silent_video_gets_bgm_audio_without_losing_video_stream(self):
        result = self._run_real_mixer(
            self.silent, dedup_audio.AudioMixOptions(bgm=str(self.bgm)))
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["audio_codec"], "aac")
        self.assertLess(abs(result["audio_duration"] - result["video_duration"]), 0.30)


if __name__ == "__main__":
    unittest.main()
