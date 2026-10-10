"""蒙太奇的最终成片必须是 C/D/E 全部成功后的原子提交。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from pycut import config
from pycut.commands import montage_pipeline
from pycut.core import montage_clips, montage_source, montage_subtitles


class MontagePublicationTests(unittest.TestCase):
    def _exercise(self, fail_at: str | None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source.mp4"
            src.write_bytes(b"source")
            out_dir = root / "output"
            out_dir.mkdir()
            final = out_dir / "混剪_source.mp4"
            old_bytes = b"previous good output" * 100
            final.write_bytes(old_bytes)
            srt = root / "source.srt"
            srt.write_text("dummy subtitle", encoding="utf-8")
            opts = montage_pipeline.MontageOptions(
                source=src, clip_dir=root, dedup_dir=root, out_dir=out_dir,
                use_pip=False, keep_audio=(fail_at == "audio"),
                srt=str(srt) if fail_at == "subtitle" else "",
            )
            profile = SimpleNamespace(
                duration=10.0, width=720, height=1280,
                segments=[], speech_total=10.0, has_audio=(fail_at == "audio"))
            def fake_concat(_picked, destination, **kwargs):
                destination.write_bytes(b"base" * 500)
                return {"ok": True, "duration": 10.0}
            def fake_publish(_cmd, _stage, working):
                self.assertEqual(final.read_bytes(), old_bytes)
                working.write_bytes(b"new complete video" * 120)
                return True
            def fake_burn(working, _slots, _canvas, _opts):
                self.assertEqual(final.read_bytes(), old_bytes)
                return fail_at != "subtitle"
            def fake_audio(working, _source, _crf):
                self.assertEqual(final.read_bytes(), old_bytes)
                return fail_at != "audio"
            with mock.patch.object(config, "MONTAGE_TMP", root / "tmp"), \
                 mock.patch.object(montage_source, "probe_source",
                                   return_value=profile), \
                 mock.patch.object(montage_source, "plan_clip_slots",
                                   return_value=[(0.0, 10.0)]), \
                 mock.patch.object(montage_subtitles, "parse_srt", return_value=[]), \
                 mock.patch.object(montage_clips, "scan_library",
                                   return_value=[object()]), \
                 mock.patch.object(montage_clips, "pick_clips",
                                   return_value=[{"item": object(), "take": 10.0}]), \
                 mock.patch.object(montage_clips, "concat_clips",
                                   side_effect=fake_concat), \
                 mock.patch.object(montage_pipeline, "_apply_variation"), \
                 mock.patch.object(montage_pipeline, "_publish_stage",
                                   side_effect=fake_publish), \
                 mock.patch.object(montage_pipeline, "_burn_subtitles",
                                   side_effect=fake_burn), \
                 mock.patch.object(montage_pipeline, "_attach_audio",
                                   side_effect=fake_audio), \
                 mock.patch.object(montage_pipeline, "_duration",
                                   return_value=10.0), \
                 mock.patch.object(montage_pipeline, "log"):
                result = montage_pipeline.run_pipeline(opts)
            if fail_at:
                self.assertEqual(result.get("error"),
                                 ("步骤D 字幕烧录失败" if fail_at == "subtitle"
                                  else "步骤E 音轨贴回失败"))
                self.assertEqual(result.get("final", ""), "")
                self.assertEqual(final.read_bytes(), old_bytes)
            else:
                self.assertNotIn("error", result)
                self.assertEqual(result["final"], str(final))
                self.assertEqual(final.read_bytes(), b"new complete video" * 120)
                self.assertEqual(result["final_duration"], 10.0)
            self.assertEqual(list(out_dir.glob("*.working_*.mp4")), [])
            self.assertEqual(list(out_dir.glob("*.stage_*.mp4")), [])

    def test_failed_subtitle_render_preserves_previous_final(self):
        self._exercise("subtitle")

    def test_failed_audio_attach_preserves_previous_final(self):
        self._exercise("audio")

    def test_complete_pipeline_publishes_new_final(self):
        self._exercise(None)


if __name__ == "__main__":
    unittest.main()
