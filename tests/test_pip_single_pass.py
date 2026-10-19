"""PIP builder executes one final encode and preserves prior output on failure."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from pycut import config
from pycut.commands import montage_pipeline
from pycut.core import montage_clips, montage_pip, montage_source


class SinglePassPipTests(unittest.TestCase):
    def _run(self, *, fail: bool):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            out_dir = root / "output"
            out_dir.mkdir()
            final = out_dir / "source_1.mp4"
            final.write_bytes(b"old good result" * 200)

            profile = SimpleNamespace(
                duration=10.0, width=720, height=1280,
                segments=[], speech_total=10.0, has_audio=False)
            opts = montage_pipeline.MontageOptions(
                source=source, clip_dir=root, dedup_dir=root, out_dir=out_dir,
                use_pip=True, pip_continuous=False, keep_audio=False, srt="")
            timeline = [{"start": 0.0, "duration": 2.0}]
            def fake_concat(_picked, base, **_kwargs):
                base.write_bytes(b"base" * 500)
                return {"ok": True, "duration": 10.0}
            def fake_build(_base, _timeline, target, **_kwargs):
                self.assertEqual(final.read_bytes(), b"old good result" * 200)
                if fail:
                    raise RuntimeError("PiP render failed")
                target.write_bytes(b"single render" * 250)
                return ["ffmpeg already executed"]

            with mock.patch.object(config, "MONTAGE_TMP", root / "tmp"), \
                 mock.patch.object(montage_source, "probe_source",
                                   return_value=profile), \
                 mock.patch.object(montage_source, "plan_clip_slots",
                                   return_value=[(0.0, 10.0)]), \
                 mock.patch.object(montage_clips, "scan_library",
                                   return_value=[object()]), \
                 mock.patch.object(montage_clips, "pick_clips",
                                   return_value=[{"item": object(), "take": 10.0}]), \
                 mock.patch.object(montage_clips, "concat_clips",
                                   side_effect=fake_concat), \
                 mock.patch.object(montage_pip, "scan_dedup_pool",
                                   return_value=[object()]), \
                 mock.patch.object(montage_pip, "plan_pip_timeline",
                                   return_value=timeline), \
                 mock.patch.object(montage_pip, "build_pip_cmd",
                                   side_effect=fake_build) as builder, \
                 mock.patch.object(montage_pipeline, "_apply_variation"), \
                 mock.patch.object(montage_pipeline, "_run") as runner, \
                 mock.patch.object(montage_pipeline, "_duration",
                                   return_value=10.0), \
                 mock.patch.object(montage_pipeline, "log"):
                result = montage_pipeline.run_pipeline(opts)
            builder.assert_called_once()
            runner.assert_not_called()  # The builder has already executed FFmpeg; never execute returned argv again.
            if fail:
                self.assertEqual(result.get("error"), "步骤C 画中画叠加失败: PiP render failed")
                self.assertEqual(final.read_bytes(), b"old good result" * 200)
            else:
                self.assertNotIn("error", result)
                self.assertEqual(final.read_bytes(), b"single render" * 250)
                self.assertEqual(result["pip_count"], 1)
            self.assertFalse(list(out_dir.glob("*.working_*.mp4")))

    def test_pip_is_encoded_only_once(self):
        self._run(fail=False)

    def test_pip_exception_keeps_previous_final(self):
        self._run(fail=True)


if __name__ == "__main__":
    unittest.main()
