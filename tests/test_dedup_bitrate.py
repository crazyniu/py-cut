"""Issue #21：M1 可选 ABR 码率扰动；仅临时文件与模拟编码器。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pycut import cli, config
from pycut.commands import dedup_cmd
from pycut.core import dedup, dedup_presets


class DedupBitrateTests(unittest.TestCase):
    @staticmethod
    def _parse(*options):
        return cli.build_parser().parse_args(["dedup", *options])

    def test_cli_flag_is_explicitly_opt_in(self):
        self.assertFalse(self._parse().vary_bitrate)
        self.assertTrue(self._parse("--vary-bitrate").vary_bitrate)

    def test_seeded_bitrate_deterministic_and_bounded(self):
        # 基线直接复用已标定的视频编码目标，不修改 config。
        for baseline, base_kbps in (
            (config.BITRATE_LOW, int(config.BITRATE_LOW[:-1]) * 1000),
            (config.BITRATE_HIGH, int(config.BITRATE_HIGH[:-1]) * 1000),
        ):
            targets = set()
            for seed in range(40):
                value, shift = dedup_presets.video_bitrate_for_seed(seed, baseline)
                self.assertEqual(
                    (value, shift),
                    dedup_presets.video_bitrate_for_seed(seed, baseline),
                )
                kbps = int(value[:-1])
                self.assertIn(shift, (-6, -4, -2, 2, 4, 6))
                self.assertGreaterEqual(kbps, base_kbps * 0.94)
                self.assertLessEqual(kbps, base_kbps * 1.06)
                targets.add(value)
            self.assertGreater(len(targets), 1)

    def test_bitrate_rng_is_isolated_from_existing_28_preset_parameters(self):
        original = dedup_presets.build_params("standard", 20261010)
        dedup_presets.video_bitrate_for_seed(20261010, config.BITRATE_HIGH)
        repeated = dedup_presets.build_params("standard", 20261010)
        self.assertEqual(original, repeated)
        self.assertNotIn("video_bitrate", original)

    def test_invalid_baseline_rejected(self):
        for baseline in ("", "0M", "invalid", "-1k"):
            with self.subTest(baseline=baseline):
                with self.assertRaisesRegex(ValueError, "码率"):
                    dedup_presets.video_bitrate_for_seed(10, baseline)

    def _build(self, *, varying=False, has_audio=True, target="7600k"):
        info = SimpleNamespace(has_audio=has_audio, width=720, height=1280)
        params = {"crf": 24, "gop": 50, "abitrate": "112k"}
        if varying:
            params["video_bitrate"] = target
        with mock.patch.object(dedup, "build_filter_complex", return_value=""), \
             mock.patch.object(dedup, "build_video_chain", return_value=[]), \
             mock.patch.object(dedup, "build_audio_chain", return_value=[]):
            return dedup._build_cmd(
                Path("source.mp4"), Path("out.mp4"), params, info,
                dedup.DedupOptions(vary_bitrate=varying), "",
            )

    def test_default_uses_original_crf_not_abr_and_preserves_audio(self):
        command, used = self._build()
        self.assertEqual(command[command.index("-crf") + 1], "24")
        self.assertNotIn("-b:v", command)
        self.assertIn("-map", command)
        self.assertIn("0:v:0", command)
        self.assertIn("0:a?", command)
        self.assertEqual(command[command.index("-b:a") + 1], "112k")
        self.assertFalse(any("ABR" in s for s in used))

    def test_opt_in_uses_abr_not_crf_and_preserves_audio(self):
        command, used = self._build(varying=True)
        self.assertEqual(command[command.index("-b:v") + 1], "7600k")
        self.assertNotIn("-crf", command)
        self.assertIn("0:v:0", command)
        self.assertIn("0:a?", command)
        self.assertEqual(command[command.index("-b:a") + 1], "112k")
        self.assertTrue(any("ABR" in s for s in used))

    def test_opt_in_keeps_silent_inputs_silent(self):
        command, _ = self._build(varying=True, has_audio=False)
        self.assertIn("-an", command)
        self.assertNotIn("-c:a", command)
        self.assertNotIn("-crf", command)

    def test_invalid_abr_target_is_not_silently_accepted(self):
        for bad_target in ("", "5000", "0k", "nanK", "xxxk"):
            with self.subTest(target=bad_target):
                with self.assertRaisesRegex(ValueError, "ABR"):
                    self._build(varying=True, target=bad_target)

    def _process(self, *, varying: bool, fail: bool = False,
                 tiny: bool = False, old_destination: bool = False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "input.mp4"
            dst = root / "output.mp4"
            src.write_bytes(b"source media" * 200)
            if old_destination:
                dst.write_bytes(b"old original output" * 140)
            info = SimpleNamespace(
                path=src, stem="input", duration=5.0,
                width=720, height=1280, has_audio=True,
                bitrate=config.BITRATE_HIGH,
            )
            commands = []

            def encode(command, timeout):
                commands.append(command)
                # ffmpeg 失败 / 没帧时可能留下一个损坏的临时文件。
                Path(command[-1]).write_bytes(b"partial" if tiny else b"encoded" * 400)
                return (1, "", "unsupported bitrate mode") if fail else (0, "", "")

            def accepted(item, source, candidate, options):
                self.assertEqual(source, src)
                self.assertEqual(candidate, dst)
                item.ok = True
                item.phash_dist = 2.0
                item.psnr = 27.0
                item.dur_dst = 5.0
                item.gates = {"phash_ok": True, "psnr_ok": True, "dur_ok": True}

            with mock.patch.object(dedup, "probe_video", return_value=info), \
                 mock.patch.object(dedup, "_video_duration", return_value=5.0), \
                 mock.patch.object(dedup, "build_filter_complex", return_value=""), \
                 mock.patch.object(dedup, "build_video_chain", return_value=[]), \
                 mock.patch.object(dedup, "build_audio_chain", return_value=[]), \
                 mock.patch.object(dedup, "run_cmd", side_effect=encode), \
                 mock.patch.object(dedup, "_self_check", side_effect=accepted) as gate, \
                 mock.patch.object(dedup, "_probe_size", return_value=(720, 1280)), \
                 mock.patch.object(dedup, "log"):
                row = dedup.process_one(
                    src, dst, dedup.DedupOptions(
                        seed=235, vary_bitrate=varying, force=old_destination))
            report = dedup.write_report([row], report_dir=root)
            payload = json.loads(report.read_text(encoding="utf-8-sig"))
            result_bytes = dst.read_bytes() if dst.exists() else None
            remaining_tmp = list(root.glob("*.dedup_tmp*.mp4"))
            return row, commands, payload, result_bytes, remaining_tmp, gate.call_count

    def test_opt_in_report_serializes_actual_target_and_mode(self):
        row, commands, report, dest, tmp, calls = self._process(varying=True)
        self.assertTrue(row["ok"])
        self.assertEqual(calls, 1)
        self.assertEqual(len(commands), 1)
        target = commands[0][commands[0].index("-b:v") + 1]
        self.assertEqual(report["items"][0]["params"]["video_bitrate"], target)
        self.assertEqual(report["items"][0]["params"]["video_rate_control"], "abr")
        self.assertEqual(report["items"][0]["params"]["video_bitrate_shift_pct"],
                         row["params"]["video_bitrate_shift_pct"])
        self.assertIn("ABR", row["params"]["summary"])
        self.assertNotIn("重编码 CRF=", row["params"]["summary"])
        self.assertTrue(dest.startswith(b"encoded"))
        self.assertEqual(tmp, [])

    def test_default_processing_keeps_original_crf_and_28_param_contract(self):
        row, commands, report, _dest, tmp, calls = self._process(varying=False)
        self.assertTrue(row["ok"])
        self.assertEqual(calls, 1)
        self.assertIn("-crf", commands[0])
        self.assertNotIn("-b:v", commands[0])
        self.assertNotIn("video_bitrate", report["items"][0]["params"])
        self.assertNotIn("video_rate_control", row["params"])
        self.assertIn("重编码 CRF=", row["params"]["summary"])
        self.assertEqual(tmp, [])

    def test_ffmpeg_failure_does_not_publish_partial_file_or_overwrite_old(self):
        row, commands, report, dest, tmp, calls = self._process(
            varying=True, fail=True, old_destination=True)
        self.assertFalse(row["ok"])
        self.assertEqual(calls, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("ffmpeg 失败 rc=1", row["error"])
        self.assertIn("unsupported bitrate mode", row["error"])
        self.assertTrue(dest.startswith(b"old original output"))
        self.assertEqual(tmp, [])
        self.assertEqual(report["items"][0]["params"]["video_rate_control"], "abr")

    def test_ffmpeg_zero_but_tiny_output_does_not_publish(self):
        row, _commands, _report, dest, tmp, calls = self._process(
            varying=True, tiny=True, old_destination=True)
        self.assertFalse(row["ok"])
        self.assertEqual(calls, 0)
        self.assertTrue(dest.startswith(b"old original output"))
        self.assertEqual(tmp, [])

    def test_cli_forwards_opt_in_switch_to_core_options(self):
        args = self._parse("--file", "sample.mp4", "--vary-bitrate",
                           "--no-report")
        with mock.patch.object(dedup_cmd, "install_signal_handlers"), \
             mock.patch.object(dedup_cmd, "reset_stop"), \
             mock.patch.object(dedup_cmd, "self_check"), \
             mock.patch.object(dedup_cmd, "_pick_srcs",
                               return_value=[Path("sample.mp4")]), \
             mock.patch.object(dedup_cmd, "process_batch",
                               return_value=[{"ok": True, "seconds": 0}]) as batch, \
             mock.patch.object(dedup_cmd, "log"):
            self.assertEqual(dedup_cmd.run(args), 0)
        options = batch.call_args.args[1]
        self.assertTrue(options.vary_bitrate)


if __name__ == "__main__":
    unittest.main()
