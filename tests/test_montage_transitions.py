"""段间淡化回归：默认关闭、时长边界及 CLI 参数校验。"""
from __future__ import annotations

import argparse
import math
import unittest
from pathlib import Path
from unittest import mock

from pycut.commands import montage_cmd, montage_pipeline
from pycut.core import montage_transitions


class SeamFadeTests(unittest.TestCase):
    def test_first_segment_only_fades_out(self):
        cmd = montage_transitions.build_seam_fade_cmd(
            Path("a.mp4"), Path("out.mp4"), duration=3.0,
            seconds=0.4, first=True)
        chain = cmd[cmd.index("-vf") + 1]
        self.assertIn("fade=t=out:st=2.600000:d=0.400000", chain)
        self.assertNotIn("fade=t=in", chain)
        self.assertIn("-an", cmd)

    def test_last_segment_only_fades_in(self):
        cmd = montage_transitions.build_seam_fade_cmd(
            Path("b.mp4"), Path("out.mp4"), duration=3.0,
            seconds=0.4, last=True)
        chain = cmd[cmd.index("-vf") + 1]
        self.assertIn("fade=t=in:st=0:d=0.400000", chain)
        self.assertNotIn("fade=t=out", chain)

    def test_single_segment_has_no_filter(self):
        self.assertEqual(montage_transitions.build_seam_fade_cmd(
            Path("a"), Path("b"), duration=2.0,
            first=True, last=True), [])

    def test_short_segments_clamp_fade_to_half(self):
        self.assertEqual(montage_transitions.fade_duration(0.2, 1.0), 0.1)

    def test_invalid_duration_rejected(self):
        for value in (-1, 0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                montage_transitions.fade_duration(2.0, value)

    def test_defaults_disable_transition(self):
        self.assertEqual(montage_pipeline.MontageOptions().seam_fade, 0.0)

    def test_cli_rejects_negative_without_starting_check(self):
        args = argparse.Namespace(seam_fade=-0.1, fission=1)
        with mock.patch.object(montage_cmd, "self_check") as checker:
            self.assertEqual(montage_cmd.run(args), 2)
        checker.assert_not_called()

    def test_cli_rejects_nan_without_starting_check(self):
        args = argparse.Namespace(seam_fade=math.nan, fission=1)
        with mock.patch.object(montage_cmd, "self_check") as checker:
            self.assertEqual(montage_cmd.run(args), 2)
        checker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
