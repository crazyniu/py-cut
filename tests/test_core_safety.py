"""视频核心流程的无媒体回归：避免丢片、错报完成与意外覆盖。

仅 mock 外部探测/编码，使用临时文件验证真实源/产物路径逻辑。
执行：python -m unittest discover -s tests -p 'test_core_safety.py' -v
"""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from pycut import cli, config
from pycut.commands import montage_pipeline, slice_cmd
from pycut.core import dedup, slice_pipeline


class CoreSafetyTests(unittest.TestCase):
    def test_slice_resume_needs_all_expected_clips(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(config, "OUT_FACE", root / "face"), \
                 mock.patch.object(config, "OUT_NOFACE", root / "noface"):
                face, noface = slice_pipeline.out_dirs_for("video")
                face.mkdir(parents=True)
                noface.mkdir(parents=True)
                a = face / "001.mp4"
                a.write_bytes(b"x" * 2048)
                self.assertFalse(slice_pipeline.video_done("video", 2))
                b = noface / "002.mp4"
                b.write_bytes(b"y" * 2048)
                self.assertTrue(slice_pipeline.video_done("video", 2))
                self.assertFalse(slice_pipeline.video_done("video", 3))
                self.assertFalse(slice_pipeline.video_done("video", 2, require_sub=True))
                for clip in (a, b):
                    clip.with_name(clip.name + ".covered.json").write_text("{}", encoding="utf-8")
                self.assertTrue(slice_pipeline.video_done("video", 2, require_sub=True))
                b.write_bytes(b"")
                self.assertFalse(slice_pipeline.video_done("video", 2))
                b.write_bytes(b"y" * 2048)
                (face / "003.mp4").write_bytes(b"z" * 2048)
                self.assertFalse(slice_pipeline.video_done("video", 2))

    def test_slice_dry_run_never_skips_scenes(self):
        info = SimpleNamespace(
            path=Path("/tmp/movie.mp4"), stem="movie",
            duration=5.0, width=720, height=1280, rotation=0)
        with mock.patch.object(slice_pipeline, "detect_scenes", return_value=[2.0]), \
             mock.patch.object(slice_pipeline, "build_segments", return_value=[(0.0, 2.0), (2.0, 5.0)]), \
             mock.patch.object(slice_pipeline, "video_done") as resume:
            result = slice_pipeline.process_video(
                SimpleNamespace(enabled=False), info,
                slice_pipeline.SliceOptions(dry_run=True), mock.Mock())
        self.assertEqual(result.segs, 2)
        self.assertFalse(result.skipped)
        resume.assert_not_called()

    def test_identical_stems_from_different_folders_are_rejected(self):
        paths = [Path("/a/one.mp4"), Path("/b/ONE.mp4"), Path("/c/two.mp4")]
        conflicts = slice_cmd._colliding_stems(paths)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(set(conflicts[0]), set(paths[:2]))

    def test_slice_returns_interruption_code(self):
        llm = SimpleNamespace(calls=0, failures=0, switched_to=None)
        manifest = SimpleNamespace(csv_path=Path("/tmp/x.csv"))
        with mock.patch.object(slice_cmd, "_stopped", return_value=True), \
             mock.patch.object(slice_cmd, "log"):
            self.assertEqual(slice_cmd._summary([], llm, manifest, 0, 0, 0), 130)

    def test_dedup_preflights_duplicate_output_destinations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = [root / "a" / "clips" / "part.mp4",
                     root / "b" / "clips" / "part.mp4",
                     root / "c" / "other" / "unique.mp4"]
            opts = dedup.DedupOptions(out_dir=str(root / "out"), force=True)
            with mock.patch.object(dedup, "process_one", return_value={"ok": True}) as process, \
                 mock.patch.object(dedup, "log"):
                rows = dedup.process_batch(files, opts, workers=3)
            self.assertEqual(process.call_count, 1)
            self.assertTrue(rows[2]["ok"])
            for i in (0, 1):
                self.assertFalse(rows[i]["ok"])
                self.assertFalse(rows[i]["skipped"])
                self.assertIn("冲突", rows[i]["error"])

    def _mock_dedup_encode(self, accepted):
        """创建真实临时产物，模拟仅 ffmpeg 和质量门禁。"""
        def build(_src, tmp, _params, _info, _opts, _logo):
            return ["fake-ffmpeg", str(tmp)], []

        def run(cmd, timeout):
            Path(cmd[-1]).write_bytes(b"processed" * 300)
            return 0, "", ""

        def check(item, src, tmp, opts):
            self.assertNotEqual(src.resolve(), tmp.resolve())
            self.assertEqual(src.read_bytes(), b"original" * 300)
            self.assertTrue(tmp.read_bytes().startswith(b"processed"))
            item.ok = accepted
            item.error = "" if accepted else "画质不达标"

        return build, run, check

    def test_in_place_must_check_original_before_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            original = Path(td) / "a.mp4"
            info = SimpleNamespace(
                path=original, stem="a", duration=5.0,
                width=720, height=1280, has_audio=False)
            opts = dedup.DedupOptions(in_place=True, force=True)
            build, run, check = self._mock_dedup_encode(True)
            for accepted in (False, True):
                original.write_bytes(b"original" * 300)
                build, run, check = self._mock_dedup_encode(accepted)
                with mock.patch.object(dedup, "probe_video", return_value=info), \
                     mock.patch.object(dedup, "_video_duration", return_value=5.0), \
                     mock.patch.object(dedup, "build_params", return_value={}), \
                     mock.patch.object(dedup, "_build_cmd", side_effect=build), \
                     mock.patch.object(dedup, "run_cmd", side_effect=run), \
                     mock.patch.object(dedup, "_self_check", side_effect=check) as gate, \
                     mock.patch.object(dedup, "_probe_size", return_value=(720, 1280)), \
                     mock.patch.object(dedup, "log"):
                    result = dedup.process_one(original, original, opts)
                gate.assert_called_once()
                self.assertIs(result["ok"], accepted)
                if accepted:
                    self.assertTrue(original.read_bytes().startswith(b"processed"))
                else:
                    self.assertEqual(original.read_bytes(), b"original" * 300)
                    self.assertIn("原地覆盖已取消", result["error"])

    def test_in_place_without_force_returns_error_not_skip(self):
        with tempfile.TemporaryDirectory() as td:
            original = Path(td) / "a.mp4"
            original.write_bytes(b"original" * 300)
            info = SimpleNamespace(
                path=original, stem="a", duration=5.0,
                width=720, height=1280, has_audio=False)
            with mock.patch.object(dedup, "probe_video", return_value=info), \
                 mock.patch.object(dedup, "_video_duration", return_value=5.0), \
                 mock.patch.object(dedup, "build_params", return_value={}):
                row = dedup.process_one(original, original,
                                        dedup.DedupOptions(in_place=True, force=False))
            self.assertFalse(row["ok"])
            self.assertFalse(row["skipped"])
            self.assertIn("--force", row["error"])
            self.assertEqual(original.read_bytes(), b"original" * 300)

    def test_montage_no_pip_copy_error_cannot_report_stale_file_as_success(self):
        from pycut.core import montage_clips, montage_pip, montage_source
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "video.mp4"
            source.write_bytes(b"original")
            out = root / "output"
            out.mkdir()
            (out / "混剪_video.mp4").write_bytes(b"stale" * 500)
            prof = SimpleNamespace(
                duration=10.0, width=720, height=1280,
                segments=[], speech_total=10.0, has_audio=False)
            opts = montage_pipeline.MontageOptions(
                source=source, clip_dir=root, dedup_dir=root,
                out_dir=out, keep_audio=False, srt="")
            def fake_concat(_picked, destination, **kwargs):
                destination.write_bytes(b"base" * 500)
                return {"ok": True, "duration": 10.0}
            with mock.patch.object(config, "MONTAGE_TMP", root / "tmp"), \
                 mock.patch.object(montage_source, "probe_source", return_value=prof), \
                 mock.patch.object(montage_source, "plan_clip_slots", return_value=[(0., 10.)]), \
                 mock.patch.object(montage_clips, "scan_library", return_value=[object()]), \
                 mock.patch.object(montage_clips, "pick_clips",
                                   return_value=[{"take": 10., "item": None}]), \
                 mock.patch.object(montage_clips, "concat_clips", side_effect=fake_concat), \
                 mock.patch.object(montage_pip, "scan_dedup_pool", return_value=[]), \
                 mock.patch.object(montage_pipeline, "_run", return_value=False), \
                 mock.patch.object(montage_pipeline, "_apply_variation"), \
                 mock.patch.object(montage_pipeline, "log"):
                result = montage_pipeline.run_pipeline(opts)
            self.assertEqual(result.get("error"), "步骤C 失败")
            self.assertFalse(result.get("final"))


if __name__ == "__main__":
    unittest.main()
