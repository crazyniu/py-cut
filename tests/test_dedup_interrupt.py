"""dedup 收到 Ctrl+C/SIGTERM 后始终报告非零状态的无媒体回归。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import cli
from pycut.commands import dedup_cmd
from pycut.core.runtime import STOP


class DedupInterruptTests(unittest.TestCase):
    def setUp(self):
        self.previous_stop = STOP.flag
        STOP.flag = False

    def tearDown(self):
        STOP.flag = self.previous_stop

    def _run_batch(self, interrupted: bool) -> int:
        args = cli.build_parser().parse_args([
            "dedup", "--src-dir", "/no-real-videos", "--no-report"
        ])

        def finished_batch(_srcs, _opts, workers=0):
            STOP.flag = interrupted
            return [{"src": "/fake/a.mp4", "ok": True, "skipped": False,
                     "seconds": 0.1}]

        with mock.patch.object(dedup_cmd, "install_signal_handlers"), \
             mock.patch.object(dedup_cmd, "self_check"), \
             mock.patch.object(dedup_cmd, "_pick_srcs",
                               return_value=[Path("/fake/a.mp4")]), \
             mock.patch.object(dedup_cmd, "process_batch",
                               side_effect=finished_batch) as batch, \
             mock.patch.object(dedup_cmd, "log"):
            code = dedup_cmd.run(args)
        batch.assert_called_once()
        return code

    def test_batch_returns_130_even_if_all_items_report_success(self):
        self.assertEqual(self._run_batch(interrupted=True), 130)

    def test_batch_without_interrupt_retains_zero_exit_code(self):
        self.assertEqual(self._run_batch(interrupted=False), 0)

    def _check_with_interrupt(self, sources_count: int, *, interrupted: bool):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = [root / f"source{i}.mp4" for i in range(sources_count)]
            for src in sources:
                src.write_bytes(b"source")
                (root / ("output-" + src.name)).write_bytes(b"output")

            args = cli.build_parser().parse_args([
                "dedup", "--check", "--src-dir", str(root)
            ])

            def resolve(src, _opts):
                return root / ("output-" + src.name)

            def compare(_source, _output, _samples):
                if interrupted:
                    STOP.flag = True
                return {"phash_dist": 30.0, "psnr": 60.0}

            with mock.patch.object(dedup_cmd, "resolve_output_path",
                                   side_effect=resolve), \
                 mock.patch("pycut.core.fingerprint.compare",
                            side_effect=compare) as compared, \
                 mock.patch.object(dedup_cmd, "log"):
                result = dedup_cmd._check_only(sources, args)
            return result, compared.call_count

    def test_check_mode_does_not_check_second_file_after_interrupt(self):
        result, calls = self._check_with_interrupt(2, interrupted=True)
        self.assertEqual((result, calls), (130, 1))

    def test_check_mode_interrupt_during_last_file_returns_130(self):
        result, calls = self._check_with_interrupt(1, interrupted=True)
        self.assertEqual((result, calls), (130, 1))

    def test_check_mode_uninterrupted_completion_retains_success(self):
        result, calls = self._check_with_interrupt(2, interrupted=False)
        self.assertEqual((result, calls), (0, 2))

    def test_check_mode_early_stop_skips_all_comparisons(self):
        STOP.flag = True
        args = cli.build_parser().parse_args(["dedup", "--check"])
        with mock.patch("pycut.core.fingerprint.compare") as compared, \
             mock.patch.object(dedup_cmd, "log"):
            result = dedup_cmd._check_only([Path("/fake/a.mp4")], args)
        self.assertEqual(result, 130)
        compared.assert_not_called()


if __name__ == "__main__":
    unittest.main()
