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
                a = face / "video_1.mp4"
                a.write_bytes(b"x" * 2048)
                self.assertFalse(slice_pipeline.video_done("video", 2))
                b = noface / "video_2.mp4"
                b.write_bytes(b"y" * 2048)
                self.assertTrue(slice_pipeline.video_done("video", 2))
                self.assertFalse(slice_pipeline.video_done("video", 3))
                self.assertFalse(slice_pipeline.video_done("video", 2, require_sub=True))
                for clip in (a, b):
                    clip.with_name(clip.name + ".covered.json").write_text('{"version":2,"subtitle":false}', encoding="utf-8")
                self.assertTrue(slice_pipeline.video_done("video", 2, require_sub=True))
                b.write_bytes(b"")
                self.assertFalse(slice_pipeline.video_done("video", 2))
                b.write_bytes(b"y" * 2048)
                (face / "video_3.mp4").write_bytes(b"z" * 2048)
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
            (out / "video_1.mp4").write_bytes(b"stale" * 500)
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
                 mock.patch.object(montage_pipeline, "_duration", return_value=10.0), \
                 mock.patch.object(montage_pipeline, "log"):
                result = montage_pipeline.run_pipeline(opts)
            self.assertIn("画中画素材目录没有可用", result.get("error", ""))
            self.assertFalse(result.get("final"))


    def test_montage_probe_is_optional_flag_and_needs_no_source(self):
        from pycut.commands import montage_cmd
        args = cli.build_parser().parse_args(
            ["montage", "--probe", "--clip-dir", "/some/materials"])
        self.assertEqual(args.probe, 1)
        self.assertEqual(
            cli.build_parser().parse_args(["montage", "--probe", "1"]).probe, 1)
        with mock.patch.object(montage_cmd, "install_signal_handlers"), \
             mock.patch.object(montage_cmd, "reset_stop"), \
             mock.patch.object(montage_cmd, "self_check"), \
             mock.patch.object(montage_cmd, "_probe_library", return_value=0) as probe:
            self.assertEqual(montage_cmd.run(args), 0)
        probe.assert_called_once_with(Path("/some/materials"), False, 0)

    def test_montage_probe_returns_failure_when_library_empty(self):
        from pycut.commands import montage_cmd
        from pycut.core import montage_clips
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(montage_clips, "scan_library", return_value=[]), \
                 mock.patch.object(montage_cmd, "log"):
                self.assertEqual(montage_cmd._probe_library(Path(td), True, 0), 1)

    def test_dedup_preset_threshold_is_not_forced_to_standard(self):
        opts = cli.build_parser().parse_args(
            ["dedup", "--preset", "aggressive"])
        self.assertEqual(opts.phash_min, 0.0)
        self.assertEqual(cli.build_parser().parse_args(
            ["dedup", "--phash-min", "1.75"]).phash_min, 1.75)
        item = dedup._Report(params={}, dur_src=10, dur_dst=10,
                             phash_dist=1.5, psnr=25)
        gates = dedup._gates(item, dedup.DedupOptions(
            preset="aggressive", phash_min=opts.phash_min), 1.0)
        self.assertEqual(gates["phash_threshold"], 1.6)
        self.assertFalse(gates["phash_ok"])

    def test_dedup_check_uses_requested_output_directory(self):
        from pycut.commands import dedup_cmd
        from pycut.core import fingerprint
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a" / "input.mp4"
            source.parent.mkdir()
            source.write_bytes(b"src")
            out_dir = root / "output"
            target = out_dir / "input_1.mp4"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"generated")
            args = cli.build_parser().parse_args(
                ["dedup", "--file", str(source), "--check",
                 "--out-dir", str(out_dir), "--preset", "aggressive"])
            with mock.patch.object(fingerprint, "compare",
                                   return_value={"phash_dist": 1.7, "psnr": 21}) as comp, \
                 mock.patch.object(dedup_cmd, "log"):
                self.assertEqual(dedup_cmd._check_only([source], args), 0)
                comp.assert_called_once_with(
                    source, target, config.DEDUP_PHASH_SAMPLES)
                target.unlink()
                self.assertEqual(dedup_cmd._check_only([source], args), 2)

    def test_dedup_check_cannot_compare_in_place_output(self):
        from pycut.commands import dedup_cmd
        args = cli.build_parser().parse_args(
            ["dedup", "--file", "irrelevant.mp4", "--check", "--in-place"])
        with mock.patch.object(dedup_cmd, "log"):
            self.assertEqual(dedup_cmd.run(args), 2)

    def test_montage_stage_publish_preserves_existing_output_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "stage.mp4"
            final = root / "final.mp4"
            final.write_bytes(b"old" * 700)
            staged.write_bytes(b"new" * 700)
            with mock.patch.object(montage_pipeline, "_run", return_value=False):
                self.assertFalse(montage_pipeline._publish_stage([], staged, final))
            self.assertEqual(final.read_bytes(), b"old" * 700)
            self.assertFalse(staged.exists())
            staged.write_bytes(b"new" * 700)
            with mock.patch.object(montage_pipeline, "_run", return_value=True):
                self.assertTrue(montage_pipeline._publish_stage([], staged, final))
            self.assertEqual(final.read_bytes(), b"new" * 700)
            self.assertFalse(staged.exists())


    def test_montage_landscape_flag_changes_actual_scan_results(self):
        from pycut.core import montage_clips
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            portrait = root / "portrait.mp4"
            landscape = root / "landscape.mp4"
            portrait.write_bytes(b"p" * 1200)
            landscape.write_bytes(b"l" * 1300)
            def probe(path):
                width, height = ((1920, 1080) if path == landscape else (720, 1280))
                return montage_clips.ClipItem(
                    path=path, duration=10.0, width=width,
                    height=height, has_audio=False)
            with mock.patch.object(montage_clips, "_probe_one", side_effect=probe), \
                 mock.patch.object(montage_clips, "log"):
                normal = montage_clips.scan_library(
                    root, min_dur=1.0, workers=1)
                allowed = montage_clips.scan_library(
                    root, min_dur=1.0, workers=1, allow_landscape=True)
            self.assertEqual({item.path for item in normal}, {portrait})
            self.assertEqual({item.path for item in allowed},
                             {portrait, landscape})

    def test_montage_pipeline_passes_landscape_setting_to_scan(self):
        from pycut.core import montage_clips, montage_source
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prof = SimpleNamespace(
                duration=10.0, width=720, height=1280,
                segments=[], speech_total=10.0, has_audio=False)
            opts = montage_pipeline.MontageOptions(
                source=root / "src.mp4", clip_dir=root,
                out_dir=root / "out", srt="", allow_landscape=True)
            with mock.patch.object(config, "MONTAGE_TMP", root / "tmp"), \
                 mock.patch.object(montage_source, "probe_source", return_value=prof), \
                 mock.patch.object(montage_source, "plan_clip_slots",
                                   return_value=[(0.0, 10.0)]), \
                 mock.patch.object(montage_clips, "scan_library",
                                   return_value=[]) as scanned, \
                 mock.patch.object(montage_pipeline, "log"):
                result = montage_pipeline.run_pipeline(opts)
            self.assertEqual(result.get("error"), "素材库为空")
            scanned.assert_called_once_with(
                root, min_dur=opts.min_clip, max_dur=opts.max_clip,
                workers=opts.workers, allow_landscape=True)


    def test_reencoding_slice_invalidates_old_subtitle_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source.mp4"
            src.write_bytes(b"input")
            dst = root / "face" / "001.mp4"
            dst.parent.mkdir()
            dst.write_bytes(b"old" * 700)
            marker = dst.with_name(dst.name + ".covered.json")
            marker.write_text('{"subtitle": true}', encoding="utf-8")
            info = SimpleNamespace(path=src, has_audio=False, bitrate="5M")
            def encode_ok(cmd, timeout):
                Path(cmd[-1]).write_bytes(b"new" * 700)
                return 0, "", ""
            with mock.patch.object(slice_pipeline, "run_cmd", side_effect=encode_ok), \
                 mock.patch.object(slice_pipeline, "log"):
                ok, size = slice_pipeline.cut_clip(info, 0.0, 2.5, dst)
            self.assertTrue(ok)
            self.assertEqual(size, 2100)
            self.assertEqual(dst.read_bytes(), b"new" * 700)
            self.assertFalse(marker.exists())

            marker.write_text('{"version":2,"subtitle":false}', encoding="utf-8")
            with mock.patch.object(slice_pipeline, "run_cmd",
                                   return_value=(1, "", "encoder failed")), \
                 mock.patch.object(slice_pipeline, "log"):
                failed, size = slice_pipeline.cut_clip(info, 0.0, 2.5, dst)
            self.assertFalse(failed)
            self.assertEqual(size, 0)
            self.assertEqual(dst.read_bytes(), b"new" * 700)
            self.assertTrue(marker.exists())

    def test_montage_explicit_missing_subtitle_does_not_run_pipeline(self):
        from pycut.commands import montage_cmd
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            args = cli.build_parser().parse_args([
                "montage", "--source", str(source),
                "--srt", str(root / "missing.srt")])
            with mock.patch.object(montage_cmd, "install_signal_handlers"), \
                 mock.patch.object(montage_cmd, "reset_stop"), \
                 mock.patch.object(montage_cmd, "self_check"), \
                 mock.patch.object(montage_pipeline, "run_pipeline") as work, \
                 mock.patch.object(montage_cmd, "log"):
                self.assertEqual(montage_cmd.run(args), 2)
            work.assert_not_called()

    def test_ass_temp_file_is_cleaned_if_subtitle_encoding_fails(self):
        from pycut.core import montage_subtitles
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ass_path = root / "subtitles.ass"
            ass_path.write_text("[Script Info]", encoding="utf-8")
            opts = montage_pipeline.MontageOptions(srt="fake.srt")
            with mock.patch.object(montage_subtitles, "parse_srt",
                                   return_value=[montage_subtitles.SubtitleCue(1, 0, 5, "test")]), \
                 mock.patch.object(montage_subtitles, "assign_cues_to_slots",
                                   return_value=[object()]), \
                 mock.patch.object(montage_subtitles, "build_ass",
                                   return_value=(ass_path, {"font_name": "test", "font_size": 20})), \
                 mock.patch.object(montage_subtitles, "build_subtitle_cmd",
                                   return_value=["ffmpeg"]), \
                 mock.patch.object(montage_pipeline, "_duration",
                                   return_value=5.0), \
                 mock.patch.object(montage_pipeline, "_run",
                                   return_value=False), \
                 mock.patch.object(montage_pipeline, "log"):
                ok = montage_pipeline._burn_subtitles(
                    root / "output.mp4", [(0.0, 5.0)], (720, 1280), opts)
            self.assertFalse(ok)
            self.assertFalse(ass_path.exists())


    def test_montage_dedup_compares_full_files_on_partial_hash_collision(self):
        from pycut.core import montage_clips
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            chunk = 1 << 20
            first = root / "first.mp4"
            second = root / "second.mp4"
            copy = root / "copy.mp4"
            first.write_bytes(b"A" * chunk + b"B" * 128 + b"Z" * chunk)
            second.write_bytes(b"A" * chunk + b"C" * 128 + b"Z" * chunk)
            copy.write_bytes(first.read_bytes())
            entries = [
                montage_clips.ClipItem(
                    path=p, duration=5.0, width=720, height=1280, has_audio=False)
                for p in (first, second, copy)
            ]
            self.assertEqual(
                montage_clips._content_sig(first),
                montage_clips._content_sig(second),
                "test inputs must deliberately collide on sampled head/tail",
            )
            unique, count = montage_clips._drop_duplicates(entries)
            self.assertEqual(count, 1)
            self.assertEqual({v.path for v in unique}, {first, second})

    def test_montage_aborts_if_any_clip_encoding_is_missing(self):
        from pycut.core import montage_clips
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "out.mp4"
            input_rows = [
                {"item": object(), "offset": 0.0, "take": 2.0},
                {"item": object(), "offset": 0.0, "take": 3.0},
            ]
            with mock.patch.object(montage_clips, "_normalize_segments",
                                   return_value=[root / "0000.mp4"]), \
                 mock.patch.object(montage_clips, "_write_concat_list") as write_list, \
                 mock.patch.object(montage_clips, "log"):
                cmd, _listing = montage_clips.build_concat_cmd(
                    input_rows, out, (720, 1280), 30)
            self.assertEqual(cmd, [])
            write_list.assert_not_called()
            self.assertFalse(out.exists())

    def test_montage_truncated_concat_must_not_replace_existing_video(self):
        from pycut.core import montage_clips
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "video.mp4"
            out.write_bytes(b"older" * 300)
            concat_list = root / "concat.txt"
            concat_list.write_text("file 'segment.mp4'\n", encoding="utf-8")
            inputs = [{"item": object(), "take": 2.0},
                      {"item": object(), "take": 3.0}]
            def encode(_argv, timeout):
                staged = out.with_name(
                    f"{out.stem}.tmp_{__import__('os').getpid()}{out.suffix}")
                staged.write_bytes(b"new" * 600)
                return 0, "", ""
            with mock.patch.object(montage_clips, "build_concat_cmd",
                                   return_value=(["fake-ffmpeg"], concat_list)), \
                 mock.patch.object(montage_clips, "run_cmd", side_effect=encode), \
                 mock.patch.object(montage_clips, "_probe_duration",
                                   return_value=2.25), \
                 mock.patch.object(montage_clips, "_seg_dir",
                                   return_value=root / "segment_tmp"), \
                 mock.patch.object(montage_clips, "log"):
                result = montage_clips.concat_clips(
                    inputs, out, canvas=(720, 1280), fps=30)
            self.assertFalse(result["ok"])
            self.assertIn("拼接时长异常", result["error"])
            self.assertEqual(out.read_bytes(), b"older" * 300)
            self.assertFalse(concat_list.exists())
            self.assertFalse(out.with_name(
                f"{out.stem}.tmp_{__import__('os').getpid()}{out.suffix}").exists())


if __name__ == "__main__":
    unittest.main()
