"""Montage interruption must stop subsequent variants and return interrupted status."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import cli
from pycut.commands import montage_cmd
from pycut.core.runtime import STOP


_GOOD = {
    "final": "/fake/out.mp4",
    "source_duration": 10.0,
    "segments": 2,
    "clips_used": 3,
    "final_duration": 9.5,
}


class MontageInterruptTests(unittest.TestCase):
    def setUp(self):
        self.old_stop = STOP.flag
        STOP.flag = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "source.mp4"
        self.src.write_bytes(b"fake MP4 for filename validation only")

    def tearDown(self):
        STOP.flag = self.old_stop

    def _run(self, *, extra=(), pipeline=None, before_first=None):
        args = cli.build_parser().parse_args([
            "montage", "--source", str(self.src), "--no-sub", "--seed", "42", *extra
        ])
        if pipeline is None:
            pipeline = lambda _opts: dict(_GOOD)

        def fake_srt(_args, _src):
            if before_first is not None:
                before_first()
            return ""

        with mock.patch.object(montage_cmd, "install_signal_handlers"), \
             mock.patch.object(montage_cmd, "self_check"), \
             mock.patch.object(montage_cmd, "_resolve_srt", side_effect=fake_srt), \
             mock.patch("pycut.commands.montage_pipeline.run_pipeline",
                        side_effect=pipeline) as calls, \
             mock.patch.object(montage_cmd, "log"):
            code = montage_cmd.run(args)
        return code, calls.call_count

    def test_cancel_during_first_job_skips_all_fissions(self):
        def cancelled(_opts):
            STOP.flag = True
            return dict(_GOOD)

        code, calls = self._run(extra=["--fission", "4"], pipeline=cancelled)
        self.assertEqual((code, calls), (130, 1))

    def test_cancel_during_second_job_skips_third_and_later_jobs(self):
        processed = 0

        def cancelled_second(_opts):
            nonlocal processed
            processed += 1
            if processed == 2:
                STOP.flag = True
            return dict(_GOOD)

        code, calls = self._run(extra=["--fission", "4"],
                                pipeline=cancelled_second)
        self.assertEqual((code, calls), (130, 2))

    def test_cancel_before_first_job_does_not_start_pipeline(self):
        code, calls = self._run(before_first=lambda: setattr(STOP, "flag", True))
        self.assertEqual((code, calls), (130, 0))

    def test_cancel_during_probe_overrides_success(self):
        args = cli.build_parser().parse_args(["montage", "--probe"])

        def interrupted_probe(*_args):
            STOP.flag = True
            return 0

        with mock.patch.object(montage_cmd, "install_signal_handlers"), \
             mock.patch.object(montage_cmd, "self_check"), \
             mock.patch.object(montage_cmd, "_probe_library",
                               side_effect=interrupted_probe) as probe, \
             mock.patch.object(montage_cmd, "log"):
            code = montage_cmd.run(args)
        self.assertEqual(code, 130)
        probe.assert_called_once()

    def test_cancel_before_probe_does_not_scan(self):
        args = cli.build_parser().parse_args(["montage", "--probe"])

        def interrupted_check(*_args, **_kwargs):
            STOP.flag = True

        with mock.patch.object(montage_cmd, "install_signal_handlers"), \
             mock.patch.object(montage_cmd, "self_check",
                               side_effect=interrupted_check), \
             mock.patch.object(montage_cmd, "_probe_library") as probe, \
             mock.patch.object(montage_cmd, "log"):
            code = montage_cmd.run(args)
        self.assertEqual(code, 130)
        probe.assert_not_called()

    def test_normal_fission_retains_success_exit_code(self):
        code, calls = self._run(extra=["--fission", "2"])
        self.assertEqual((code, calls), (0, 2))

    def test_regular_pipeline_failure_still_returns_two(self):
        code, calls = self._run(extra=["--fission", "3"],
                                pipeline=lambda _opts: {"error": "test failure"})
        self.assertEqual((code, calls), (2, 1))


if __name__ == "__main__":
    unittest.main()
