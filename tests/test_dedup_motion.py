"""Optional Ken Burns planning, graphs, and rollback without real video."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pycut import cli, config
from pycut.commands import dedup_cmd
from pycut.core import dedup, dedup_presets


class DedupMotionTests(unittest.TestCase):
    @staticmethod
    def _args(*argv):
        return cli.build_parser().parse_args(["dedup", *argv])

    @staticmethod
    def _info(source=None, duration=3.0, has_audio=True):
        return SimpleNamespace(
            path=source or Path("source.mp4"), duration=duration,
            width=320, height=240, has_audio=has_audio,
            bitrate=config.BITRATE_LOW,
        )

    def test_flag_is_off_by_default(self):
        self.assertFalse(self._args().ken_burns)
        self.assertTrue(self._args("--ken-burns").ken_burns)

    def test_motion_plan_determinism_and_independent_rng(self):
        first = dedup_presets.build_params("standard", 721, width=320, height=240)
        plans = []
        for seed in range(30):
            a = dedup_presets.ken_burns_for_seed(seed, 10.0)
            b = dedup_presets.ken_burns_for_seed(seed, 10.0)
            self.assertEqual(a, b)
            self.assertIn(a["ken_burns_direction"], ("lr", "rl", "tb", "bt"))
            self.assertGreater(a["ken_burns_zoom_start"], 1.0)
            self.assertLess(a["ken_burns_zoom_start"] +
                            a["ken_burns_zoom_delta"], 1.1)
            self.assertGreaterEqual(a["ken_burns_period"], 3.0)
            self.assertLessEqual(a["ken_burns_period"], 5.0)
            plans.append(tuple(a.items()))
        self.assertGreater(len(set(plans)), 4)
        self.assertEqual(
            first,
            dedup_presets.build_params("standard", 721, width=320, height=240),
        )
        self.assertNotIn("ken_burns_direction", first)

    def test_short_clip_clamps_motion_period_without_division_by_zero(self):
        for duration in (0.03, 0.1, 1.5):
            plan = dedup_presets.ken_burns_for_seed(5, duration)
            self.assertGreaterEqual(plan["ken_burns_period"], 0.05)
            self.assertLessEqual(plan["ken_burns_period"],
                                 max(0.05, duration) + 0.0005)
            chain = dedup._ken_burns_chain(plan, self._info(duration=duration))
            self.assertEqual(len(chain), 2)
            self.assertIn("eval=frame", chain[0])

    def test_invalid_duration_and_bad_motion_fail_explicitly(self):
        for duration in (0, -1, float("nan"), float("inf")):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "时长"):
                    dedup_presets.ken_burns_for_seed(5, duration)
        plan = dedup_presets.ken_burns_for_seed(5, 3)
        for key, value in (
            ("ken_burns_direction", "unknown"),
            ("ken_burns_zoom_start", 0.8),
            ("ken_burns_zoom_delta", 0),
            ("ken_burns_period", 0),
        ):
            bad = dict(plan, **{key: value})
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "Ken Burns"):
                    dedup._ken_burns_chain(bad, self._info())

    def test_default_chain_does_not_add_motion_or_perspective(self):
        params = dedup_presets.build_params(
            "standard", 20261010, width=320, height=240)
        with mock.patch.object(dedup, "probe_scalar", return_value="30/1"):
            unchanged = dedup._build_chain(params, self._info())
            explicit = dedup._build_chain(dict(params), self._info())
        self.assertEqual(unchanged, explicit)
        self.assertFalse(any("eval=frame" in x for x in unchanged))
        self.assertFalse(any("perspective" in x for x in unchanged))

    def test_motion_after_static_geometry_normalizes_output_size(self):
        params = dedup_presets.build_params(
            "light", 27, width=320, height=240)
        params.update(dedup_presets.ken_burns_for_seed(27, 3))
        with mock.patch.object(dedup, "probe_scalar", return_value="30/1"):
            filters = dedup._build_chain(params, self._info())
        motion_idx = next(i for i, f in enumerate(filters) if "eval=frame" in f)
        self.assertIn("crop=320:240", filters[motion_idx + 1])
        self.assertFalse(any("zoompan" in f for f in filters))
        self.assertFalse(any("perspective" in f for f in filters))
        self.assertIn("scale=320:240", filters[:motion_idx])

    def test_panning_direction_and_coordinate_bounds(self):
        for direction in ("lr", "rl", "tb", "bt"):
            params = dedup_presets.ken_burns_for_seed(7, 3)
            params["ken_burns_direction"] = direction
            scale, crop = dedup._ken_burns_chain(params, self._info())
            self.assertIn("trunc(iw", scale)
            self.assertIn("trunc(ih", scale)
            self.assertIn("PI*t/", scale)
            self.assertIn("eval=frame", scale)
            self.assertIn("0.45*cos", crop)
            self.assertIn("crop=320:240", crop)

    def test_complex_filter_with_border_retains_motion_and_original_output(self):
        params = dedup_presets.build_params(
            "standard", 11, width=320, height=240)
        params.update(dedup_presets.ken_burns_for_seed(11, 3))
        params["border"] = True
        with mock.patch.object(dedup, "probe_scalar", return_value="30/1"):
            graph = dedup.build_filter_complex(params, self._info())
        self.assertIsNotNone(graph)
        self.assertIn("eval=frame", graph)
        self.assertIn("crop=320:240", graph)
        self.assertIn("split=2", graph)
        self.assertIn("[vout]", graph)

    def test_command_maps_audio_and_video_and_preserves_crf(self):
        params = dedup_presets.build_params(
            "standard", 42, width=320, height=240)
        params.update(dedup_presets.ken_burns_for_seed(42, 3))
        with mock.patch.object(dedup, "probe_scalar", return_value="30/1"):
            cmd, _ = dedup._build_cmd(
                Path("input.mp4"), Path("output.mp4"), params,
                self._info(), dedup.DedupOptions(ken_burns=True), "")
        self.assertIn("-crf", cmd)
        self.assertNotIn("-b:v", cmd)
        self.assertIn("0:v:0", cmd)
        self.assertIn("0:a?", cmd)
        self.assertIn("-c:a", cmd)
        self.assertIn("eval=frame", cmd[cmd.index("-vf") + 1])

    def test_ken_burns_can_coexist_with_opt_in_abr(self):
        params = dedup_presets.build_params(
            "standard", 42, width=320, height=240)
        params.update(dedup_presets.ken_burns_for_seed(42, 3))
        params["video_bitrate"] = "5000k"
        with mock.patch.object(dedup, "probe_scalar", return_value="30/1"):
            cmd, _ = dedup._build_cmd(
                Path("input.mp4"), Path("out.mp4"), params, self._info(),
                dedup.DedupOptions(ken_burns=True, vary_bitrate=True), "")
        self.assertIn("-b:v", cmd)
        self.assertNotIn("-crf", cmd)
        self.assertIn("0:a?", cmd)
        self.assertIn("eval=frame", cmd[cmd.index("-vf") + 1])

    def _process(self, *, fail=False, invalid_info=False, preexisting=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, out = root / "src.mp4", root / "out.mp4"
            src.write_bytes(b"source" * 400)
            if preexisting:
                out.write_bytes(b"original" * 300)
            info = None if invalid_info else self._info(src)
            commands = []

            def encode(cmd, timeout):
                commands.append(cmd)
                Path(cmd[-1]).write_bytes(b"partial" if fail else b"encoded" * 500)
                return (1, "", "simulated motion encode error") if fail else (0, "", "")

            def accept(item, source, target, options):
                self.assertEqual(source, src)
                self.assertNotEqual(target, out)
                self.assertEqual(target.parent, out.parent)
                self.assertTrue(target.is_file())
                item.ok = True
                item.dur_dst = item.dur_src
                item.phash_dist = 2.0
                item.psnr = 25.0
                item.gates = {"phash_ok": True, "psnr_ok": True, "dur_ok": True}

            with mock.patch.object(dedup, "probe_video", return_value=info), \
                 mock.patch.object(dedup, "_video_duration", return_value=3.0), \
                 mock.patch.object(dedup, "probe_scalar", return_value="30/1"), \
                 mock.patch.object(dedup, "_probe_size", return_value=(320, 240)), \
                 mock.patch.object(dedup, "run_cmd", side_effect=encode), \
                 mock.patch.object(dedup, "_self_check", side_effect=accept) as gate, \
                 mock.patch.object(dedup, "log"):
                row = dedup.process_one(
                    src, out, dedup.DedupOptions(
                        ken_burns=True, seed=52, force=preexisting))
            report = dedup.write_report([row], report_dir=root)
            saved = json.loads(report.read_text(encoding="utf-8-sig"))
            out_data = out.read_bytes() if out.is_file() else None
            tmp = list(root.glob("*.dedup_tmp*.mp4"))
            return row, saved, commands, out_data, tmp, gate.call_count

    def test_successful_processing_records_motion_in_json(self):
        row, report, commands, data, tmp, checks = self._process()
        self.assertTrue(row["ok"])
        self.assertEqual(checks, 1)
        self.assertEqual(len(commands), 1)
        self.assertIn("eval=frame", commands[0][commands[0].index("-vf") + 1])
        self.assertEqual(
            report["items"][0]["params"]["ken_burns_direction"],
            row["params"]["ken_burns_direction"])
        self.assertIn("Ken Burns", row["params"]["summary"])
        self.assertTrue(data.startswith(b"encoded"))
        self.assertFalse(tmp)

    def test_encoding_failure_never_overwrites_existing_output(self):
        row, _report, commands, data, tmp, checks = self._process(
            fail=True, preexisting=True)
        self.assertFalse(row["ok"])
        self.assertEqual(checks, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("simulated motion encode error", row["error"])
        self.assertTrue(data.startswith(b"original"))
        self.assertFalse(tmp)

    def test_missing_video_stream_fails_without_touching_output(self):
        row, _report, commands, data, tmp, checks = self._process(
            invalid_info=True, preexisting=True)
        self.assertFalse(row["ok"])
        self.assertIn("probe 失败", row["error"])
        self.assertEqual(commands, [])
        self.assertEqual(checks, 0)
        self.assertTrue(data.startswith(b"original"))
        self.assertFalse(tmp)

    def test_cli_forwards_motion_option_without_losing_profile_validation(self):
        args = self._args("--file", "sample.mp4", "--ken-burns", "--no-report")
        with mock.patch.object(dedup_cmd, "apply_account_profile", return_value=None) as profile, \
             mock.patch.object(dedup_cmd, "install_signal_handlers"), \
             mock.patch.object(dedup_cmd, "reset_stop"), \
             mock.patch.object(dedup_cmd, "self_check"), \
             mock.patch.object(dedup_cmd, "_pick_srcs",
                               return_value=[Path("sample.mp4")]), \
             mock.patch.object(dedup_cmd, "process_batch",
                               return_value=[{"ok": True, "seconds": 0}]) as batch, \
             mock.patch.object(dedup_cmd, "log"):
            self.assertEqual(dedup_cmd.run(args), 0)
        profile.assert_called_once()
        opts = batch.call_args.args[1]
        self.assertTrue(opts.ken_burns)
        self.assertFalse(opts.vary_bitrate)


if __name__ == "__main__":
    unittest.main()
