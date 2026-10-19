"""Offline ASS outline/box comparison planning without FFmpeg or user media."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from pycut import config
from pycut.core import montage_subtitles as subs
from tools import compare_subtitle_styles as compare


_SRT = """1
00:00:00,200 --> 00:00:02,000
婚礼现场日本語 ABC

2
00:00:02,000 --> 00:00:03,800
第二条字幕，保持原有时间
"""


class CompareSubtitleStylesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.video = self.root / "输入视频.mp4"
        self.video.write_bytes(b"not-a-real-video")
        self.srt = self.root / "字幕.srt"
        self.srt.write_text(_SRT, encoding="utf-8")
        self.dest = self.root / "对照输出"

    def _plan(self):
        return compare.make_plan(
            self.video, self.srt, self.dest, (720, 1276), "Noto Sans CJK",
        )

    def test_default_style_unchanged_and_explicit_variants_are_separate(self):
        with mock.patch.object(config, "MONTAGE_TMP", self.root / "default"):
            plain, base = subs.build_ass(
                subs.parse_srt(self.srt), canvas=(720, 1276),
                font_family="Noto Sans CJK",
            )
        self.assertEqual(base["border_style"], 3)
        self.assertTrue(Path(plain).is_file())

        plan = self._plan()
        self.assertEqual([x["border_style"] for x in plan["jobs"]], [1, 3])
        self.assertEqual([x["style"]["border_style"] for x in plan["jobs"]],
                         [1, 3])
        outline, box = plan["jobs"]
        self.assertGreater(outline["style"]["outline"],
                           box["style"]["outline"])
        for job in plan["jobs"]:
            self.assertEqual(job["style"]["font_size"], base["font_size"])
            self.assertEqual(job["style"]["cues"], 2)
            ass = Path(job["ass"]).read_text(encoding="utf-8")
            style_line = next(x for x in ass.splitlines()
                              if x.startswith("Style: pycut,"))
            self.assertEqual(int(style_line.split(",")[15]), job["border_style"])
            self.assertIn("Dialogue: 0,0:00:00.20,0:00:02.00", ass)
        self.assertNotEqual(outline["ass"], box["ass"])

    def test_reject_unknown_style_before_creating_ass(self):
        with self.assertRaisesRegex(ValueError, "BorderStyle"):
            subs.build_ass(
                subs.parse_srt(self.srt), canvas=(720, 1276),
                border_style=2, ass_dir=self.root / "not-created",
            )
        self.assertFalse((self.root / "not-created").exists())

    def test_plan_keeps_source_audio_without_rebuilding_existing_filter(self):
        plan = self._plan()
        for job in plan["jobs"]:
            args = job["argv"]
            self.assertIn("-vf", args)
            self.assertIn("subtitles=filename=", args[args.index("-vf") + 1])
            self.assertNotIn("-an", args)
            self.assertEqual(
                args[args.index("-map") + 1], "0:v:0"
            )
            self.assertEqual(args.count("-map"), 2)
            self.assertIn("0:a?", args)
            self.assertEqual(args[args.index("-c:a") + 1], "copy")
            self.assertNotEqual(args[-1], job["output"])

    def test_only_explicit_execute_can_invoke_media_tools(self):
        with mock.patch.object(compare, "run_cmd") as run:
            rc = compare.main([
                "--video", str(self.video), "--srt", str(self.srt),
                "--output-dir", str(self.dest), "--font", "Noto Sans CJK"
            ])
            run.assert_not_called()
        self.assertEqual(rc, 0)
        manifest = json.loads(
            (self.dest / "subtitle_style_comparison.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(len(manifest["jobs"]), 2)
        self.assertTrue(all(Path(x["ass"]).is_file() for x in manifest["jobs"]))
        self.assertTrue(all(not Path(x["output"]).exists()
                            for x in manifest["jobs"]))

    def test_execute_without_authorization_rejected_before_any_io(self):
        with mock.patch.object(compare, "probe_video") as probe, \
             mock.patch.object(compare, "run_cmd") as run:
            rc = compare.main([
                "--video", str(self.video), "--srt", str(self.srt),
                "--output-dir", str(self.dest), "--execute",
            ])
            self.assertEqual(rc, 2)
            probe.assert_not_called()
            run.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_missing_inputs_and_empty_srt_are_explicit_errors(self):
        with self.assertRaisesRegex(ValueError, "视频不存在"):
            compare.make_plan(
                self.root / "missing.mp4", self.srt, self.dest,
                (720, 1276), "Noto Sans CJK",
            )
        self.srt.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "没有合法"):
            self._plan()

    def test_existing_video_is_preserved_without_overwrite(self):
        plan = self._plan()
        first = Path(plan["jobs"][0]["output"])
        first.write_bytes(b"existing-result")
        info = SimpleNamespace(width=720, height=1276, duration=4.0,
                               has_audio=True)
        with mock.patch.object(compare, "probe_video", return_value=info), \
             mock.patch.object(compare, "run_cmd") as cmd:
            with self.assertRaises(FileExistsError):
                compare.execute_plan(plan)
            cmd.assert_not_called()
        self.assertEqual(first.read_bytes(), b"existing-result")

    def test_ffmpeg_failure_does_not_publish_empty_video(self):
        plan = self._plan()
        info = SimpleNamespace(width=720, height=1276, duration=4.0,
                               has_audio=True)
        with mock.patch.object(compare, "probe_video", return_value=info), \
             mock.patch.object(compare, "run_cmd",
                               return_value=(2, "", "simulated encode error")):
            with self.assertRaisesRegex(RuntimeError, "simulated encode error"):
                compare.execute_plan(plan)
        self.assertTrue(all(not Path(x["output"]).exists()
                            for x in plan["jobs"]))

    def test_video_and_audio_probe_validation_rejects_truncated_output(self):
        plan = self._plan()
        src = SimpleNamespace(width=720, height=1276, duration=4.0,
                              has_audio=True)
        bad = SimpleNamespace(width=720, height=1276, duration=4.0,
                              has_audio=False)
        def make_temporary(command, label, timeout=900):
            Path(command[-1]).write_bytes(b"truncated")
        with mock.patch.object(compare, "probe_video", side_effect=[src, bad]), \
             mock.patch.object(compare, "_run_checked",
                               side_effect=make_temporary):
            with self.assertRaisesRegex(RuntimeError, "原声存在性"):
                compare.execute_plan(plan)
        self.assertTrue(all(not Path(x["output"]).exists()
                            for x in plan["jobs"]))

    def test_mock_success_writes_snapshots_and_requires_visual_review(self):
        plan = self._plan()
        src = SimpleNamespace(width=720, height=1276, duration=4.0,
                              has_audio=True)
        def create_output(command, description, timeout=900):
            output = Path(command[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"mock-nonempty")
        with mock.patch.object(compare, "probe_video", return_value=src), \
             mock.patch.object(compare, "_run_checked", side_effect=create_output):
            result = compare.execute_plan(plan)
        self.assertEqual(result["status"], "rendered_pending_visual_review")
        self.assertEqual(len(result["results"]), 2)
        for rec in result["results"]:
            self.assertTrue(Path(rec["output"]).exists())
            self.assertEqual(len(rec["snapshots"]), 3)
            for snap in rec["snapshots"]:
                self.assertTrue(Path(snap).exists())
        self.assertIn("visual_review", result)


if __name__ == "__main__":
    unittest.main()
