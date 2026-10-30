"""Montage reference timestamps, fixed output geometry, and online-sticker failure boundaries."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

from pycut import cli, config
from pycut.commands import montage_cmd, montage_pipeline
from pycut.core import dedup, montage_clips, montage_subtitles, online_stickers
from pycut.core import montage_source
from pycut.core.montage_pip import PipItem, plan_pip_timeline


class MontageRequirementsTests(unittest.TestCase):
    def test_reused_background_is_rejected_before_rendering(self):
        profile = SimpleNamespace(duration=6, width=1920, height=1080,
                                  segments=[], speech_total=0)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(config, "MONTAGE_TMP", Path(directory) / "tmp"), \
             mock.patch.object(montage_source, "probe_source", return_value=profile), \
             mock.patch.object(montage_source, "plan_clip_slots", return_value=[(0, 6)]), \
             mock.patch.object(montage_clips, "scan_library", return_value=[object()]), \
             mock.patch.object(montage_clips, "pick_clips", return_value=[
                 {"item": object(), "take": 6, "reused": True}]), \
             mock.patch.object(montage_pipeline, "_apply_variation"), \
             mock.patch.object(montage_clips, "concat_clips") as render:
            result = montage_pipeline.run_pipeline(montage_pipeline.MontageOptions(
                source=Path(directory) / "source.mp4", out_dir=Path(directory), srt="", keep_audio=False))
            self.assertIn("重复", result["error"])
            render.assert_not_called()

    def test_continuous_pip_uses_available_footage_instead_of_leaving_a_gap(self):
        pool = [PipItem(Path("a.mp4"), 2), PipItem(Path("b.mp4"), 12)]
        for seed in range(10):
            with self.subTest(seed=seed):
                timeline = plan_pip_timeline(pool, 8, slot=2.5, seed=seed, continuous=True)
                self.assertGreater(sum(e["duration"] for e in timeline), 8)
                self.assertLessEqual(sum(e["duration"] for e in timeline), 14)
                self.assertEqual(len({e["item"].path for e in timeline}), len(timeline))
    def test_both_variation_and_seed_are_reproducible_without_changing_timing(self):
        params, chain = dedup.montage_variation(720, 1280, 123)
        self.assertEqual((params, chain), dedup.montage_variation(720, 1280, 123))
        self.assertNotEqual(params, dedup.montage_variation(720, 1280, 124)[0])
        self.assertIn("crop=", chain)
        self.assertIn("noise=", chain)
        self.assertNotIn("setpts", chain)
        self.assertNotIn("fps=", chain)

    def test_subtitles_keep_gaps_and_short_cues_despite_unrelated_shots(self):
        cues = [montage_subtitles.SubtitleCue(1, 1.2, 1.6, "短句"),
                montage_subtitles.SubtitleCue(2, 3.1, 4.0, "下一句")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ass = root / "sub.ass"
            ass.touch()
            with mock.patch.object(montage_subtitles, "parse_srt", return_value=cues), \
                 mock.patch.object(montage_subtitles, "build_ass", return_value=(ass, {})) as build, \
                 mock.patch.object(montage_pipeline, "_duration", return_value=5), \
                 mock.patch.object(montage_pipeline, "_run", return_value=False):
                montage_pipeline._burn_subtitles(root / "video.mp4", [(0, 5)],
                                                (720, 1280), montage_pipeline.MontageOptions())
            actual = build.call_args.args[0]
            self.assertEqual([(c.start, c.end) for c in actual], [(1.2, 1.6), (3.1, 4.0)])

    def test_online_download_rejects_non_images_and_oversize_responses(self):
        for payload in [b"", b"<html>error</html>", b"x" * (online_stickers.MAX_BYTES + 1)]:
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as directory:
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = payload
                with mock.patch.object(online_stickers.urllib.request, "urlopen", return_value=response):
                    with self.assertRaises(ValueError):
                        online_stickers.download_stickers(Path(directory))
                self.assertFalse(list(Path(directory).glob("*.png")))

    def test_online_download_fetches_again_and_records_license(self):
        ok, png = cv2.imencode(".png", np.zeros((12, 12, 4), dtype=np.uint8))
        self.assertTrue(ok)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = png.tobytes()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(online_stickers.urllib.request, "urlopen", return_value=response) as fetch:
            root = Path(directory)
            self.assertEqual(len(online_stickers.download_stickers(root)), 3)
            online_stickers.download_stickers(root)
            self.assertEqual(fetch.call_count, 6)
            self.assertIn("CC BY 4.0", (root / "credits.json").read_text())

    def test_fission_keeps_online_download_and_transition_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.touch()
            args = cli.build_parser().parse_args(["montage", "--source", str(source), "--fission", "2"])
            with mock.patch.object(montage_cmd, "self_check"), \
                 mock.patch.object(montage_cmd, "install_signal_handlers"), \
                 mock.patch.object(montage_pipeline, "run_pipeline", return_value={
                     "final": "ok", "source_duration": 5, "segments": 2,
                     "clips_used": 2, "final_duration": 5}) as run:
                self.assertEqual(montage_cmd.run(args), 0)
            options = [call.args[0] for call in run.call_args_list]
            self.assertTrue(all(o.online_stickers and o.seam_fade > 0 for o in options))
            self.assertNotEqual(options[0].seed, options[1].seed)


if __name__ == "__main__":
    unittest.main()
