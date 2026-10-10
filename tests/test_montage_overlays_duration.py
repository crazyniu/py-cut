"""混剪现有功能的回归：参考时长、连续画中画、指定贴纸与实际 FFmpeg 像素。"""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pycut import config
from pycut.commands import montage_cmd, montage_pipeline
from pycut.core import ffmpeg as ffmpeg_core
from pycut.core import montage_clips, montage_pip, montage_source, montage_sticker
from pycut.core.montage_pip import PipItem


class MontageOverlayContractTests(unittest.TestCase):
    def test_default_duration_is_full_source_duration(self):
        self.assertEqual(config.MONTAGE_TARGET_FILL_RATIO, 1.0)
        args = montage_cmd.argparse.Namespace(
            seam_fade=0.0, fission=1, fill_ratio=0.98)
        with mock.patch.object(montage_cmd, "log"):
            self.assertEqual(montage_cmd.run(args), 2)

    def test_subtitle_gaps_are_filled_with_clip_slots(self):
        slots = [(0.0, 1.2), (1.5, 3.0)]
        result = montage_pipeline._complete_slot_budget(slots, 7.33, 2.0)
        self.assertEqual(result[:2], slots)
        self.assertLessEqual(max(b - a for a, b in result), 2.0)
        self.assertAlmostEqual(sum(b - a for a, b in result), 7.33, places=6)

    def test_continuous_pip_plans_full_video_not_gap_based_budget(self):
        pool = [PipItem(Path(f"clip_{i}.mp4"), 2.0) for i in range(5)]
        items = montage_pip.plan_pip_timeline(pool, 3.25, slot=1.0,
                                               seed=42, continuous=True)
        self.assertAlmostEqual(sum(i["duration"] for i in items), 3.25, places=2)
        for first, second in zip(items, items[1:]):
            self.assertAlmostEqual(first["start"] + first["duration"],
                                   second["start"], delta=0.002)
        self.assertEqual(len(montage_pip._clamp_entries(items)), len(items))

    def test_explicit_sticker_is_forwarded_without_reading_default_dir(self):
        chosen = Path("/tmp/custom/sticker.png")
        with mock.patch.object(montage_sticker, "_dir_stickers",
                               side_effect=AssertionError("wrong directory")), \
             mock.patch.object(montage_sticker, "build_sticker_cmd",
                               return_value=(None, None)) as builder:
            self.assertFalse(montage_sticker.apply_sticker_to_file(
                Path("/tmp/in.mp4"), Path("/tmp/out.mp4"),
                (160, 284), sticker=chosen))
        self.assertEqual(builder.call_args.args[1], [chosen])

    def test_speed_variation_never_reads_beyond_source_end(self):
        item = montage_clips.ClipItem(Path("clip.mp4"), 2.0, 160, 284, False)
        rows = [{"item": item, "take": 1.8, "offset": 0.2}]
        opts = montage_pipeline.MontageOptions(speed_range=(1.0, 1.5))
        montage_pipeline._apply_variation(rows, 123, opts)
        self.assertLessEqual(rows[0].get("speed", 1.0) * 1.8 + 0.2, 2.001)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "synthetic integration needs ffmpeg + ffprobe")
class MontageSyntheticMediaTests(unittest.TestCase):
    @staticmethod
    def _run(*args) -> bytes:
        process = subprocess.run(
            [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
             *map(str, args)], capture_output=True, timeout=90)
        if process.returncode:
            raise AssertionError(process.stderr.decode("utf-8", "replace")[-2000:])
        return process.stdout

    @classmethod
    def _video(cls, path: Path, color: str, seconds: float):
        cls._run("-f", "lavfi", "-i",
                 f"color=c={color}:size=160x284:rate=30:duration={seconds}",
                 "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", path)

    @staticmethod
    def _duration(path: Path) -> float:
        result = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True)
        return float(result.stdout.strip())

    def _patch_ffmpeg(self, stack: ExitStack, temp: Path):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        stack.enter_context(mock.patch.object(config, "MONTAGE_TMP", temp / "working"))
        stack.enter_context(mock.patch.object(config, "FFMPEG", ffmpeg))
        for mod in (ffmpeg_core, montage_clips, montage_pip):
            stack.enter_context(mock.patch.object(mod, "FFMPEG", ffmpeg))
            stack.enter_context(mock.patch.object(mod, "FFPROBE", ffprobe))
        stack.enter_context(mock.patch.dict(os.environ, {"PYCUT_FFMPEG": ffmpeg}))

    def test_rendered_video_has_both_overlays_and_reference_duration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "clips").mkdir()
            (root / "pips").mkdir()
            (root / "stickers").mkdir()
            source = root / "reference.mp4"
            self._video(source, "gray", 2.333333)
            self._video(root / "clips" / "main.mp4", "gray", 5.0)
            for i in range(3):
                self._video(root / "pips" / f"pip{i}.mp4", "red", 2.0)
            self._run("-f", "lavfi", "-i",
                      "color=c=green:size=64x64", "-frames:v", "1",
                      root / "stickers" / "logo.png")

            ref_duration = self._duration(source)
            profile = montage_source.SourceProfile(
                source, ref_duration, 160, 284, 30.0, False, [], 0.0)
            opts = montage_pipeline.MontageOptions(
                source=source, clip_dir=root / "clips",
                dedup_dir=root / "pips", out_dir=root / "out",
                sticker_dir=str(root / "stickers"), sticker_chance=1.0,
                pip_slot=1.0, use_pip=True, pip_continuous=True,
                keep_audio=False, srt="", min_clip=0.5, max_clip=4.0,
                seed=9)
            with ExitStack() as stack:
                self._patch_ffmpeg(stack, root)
                stack.enter_context(mock.patch.object(
                    montage_source, "probe_source", return_value=profile))
                result = montage_pipeline.run_pipeline(opts)

            self.assertNotIn("error", result, result)
            self.assertEqual(result["stickers"], 1)
            self.assertGreaterEqual(result["pip_count"], 2)
            final = Path(result["final"])
            self.assertTrue(final.is_file())
            self.assertLessEqual(abs(self._duration(final) - ref_duration),
                                 1.0 / 30 + 0.01)

            frame = self._run("-ss", "0.5", "-i", final,
                              "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "pipe:1")
            self.assertEqual(len(frame), 160 * 284 * 3)
            def rgb(x, y):
                pos = (y * 160 + x) * 3
                return frame[pos], frame[pos + 1], frame[pos + 2]
            red, green, blue = rgb(135, 43)
            self.assertGreater(red, green + 35, "upper-right PiP must be visible")
            self.assertGreater(red, blue + 35)
            green_pixels = sum(
                1 for r, g, b in zip(frame[::3], frame[1::3], frame[2::3])
                if g > r + 25 and g > b + 25)
            self.assertGreater(green_pixels, 40, "sticker must be visible in decoded frame")

    def test_short_audio_does_not_shorten_video(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            audio = root / "short.m4a"
            self._video(video, "blue", 3.0)
            self._run("-f", "lavfi", "-i",
                      "sine=frequency=440:duration=1.0",
                      "-c:a", "aac", audio)
            with ExitStack() as stack:
                self._patch_ffmpeg(stack, root)
                self.assertTrue(montage_pipeline._attach_audio(video, audio))
            self.assertLessEqual(abs(self._duration(video) - 3.0), 1.0 / 30)


if __name__ == "__main__":
    unittest.main()
