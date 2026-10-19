"""Measured per-item telemetry, concurrent ownership, and cleanup boundaries."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pycut.core import desktop_progress as telemetry
from pycut.core.progress import parse


class DesktopProgressTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"PYCUT_DESKTOP_JOB": "1", "PYCUT_PROGRESS": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(telemetry.bind_item, None)
        self.command = ["ffmpeg", "-i", "source.mp4", "target.mp4"]

    @staticmethod
    def events(output):
        return [value for line in output.getvalue().splitlines() if (value := parse(line)) is not None]

    def test_requires_both_flags_and_bound_item(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(telemetry.tempfile, "mkstemp") as create:
            for desktop, progress, item in [("0", "1", "video"), ("1", "0", "video"), ("1", "1", None)]:
                with self.subTest(desktop=desktop, progress=progress, item=item), patch.dict(
                        os.environ, {"PYCUT_DESKTOP_JOB": desktop, "PYCUT_PROGRESS": progress}):
                    telemetry.bind_item(item)
                    telemetry.activity("识别中")
                    with telemetry.encoding_progress(self.command, 4) as command:
                        self.assertIs(command, self.command)
            create.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_activity_and_explicit_report_use_real_units(self):
        output = io.StringIO()
        telemetry.bind_item("video-1")
        self.assertEqual(telemetry.current_item(), "video-1")
        with contextlib.redirect_stdout(output):
            telemetry.activity("识别中")
            telemetry.report(17, 50, item_id="video-2")
        self.assertEqual(self.events(output), [
            {"stage": "识别中", "completed": 0, "total": 0, "item_progress": True, "item_id": "video-1"},
            {"stage": "生成中", "completed": 17, "total": 50, "item_progress": True, "item_id": "video-2"}])

    def test_progress_is_measured_before_exit_and_partial_records_are_ignored(self):
        output = io.StringIO()
        telemetry.bind_item("video-1")
        emitted = threading.Event()
        original_report = telemetry.report
        def observe(completed, total, *args, **kwargs):
            original_report(completed, total, *args, **kwargs)
            if completed == 750000:
                emitted.set()
        with contextlib.redirect_stdout(output), patch.object(telemetry, "report", side_effect=observe):
            with telemetry.encoding_progress(self.command, 4) as command:
                self.assertEqual(command[:-5], self.command[:-1])
                self.assertEqual(command[-1], self.command[-1])
                self.assertEqual(command[-5:-2], ["-stats_period", "0.5", "-progress"])
                path = Path(command[-2])
                path.write_text("out_time_us=750000\nprogress=continue\nout_time_us=999")
                self.assertTrue(emitted.wait(2), "No measured progress emitted while encoding was active")
                self.assertTrue(path.exists())
        self.assertFalse(path.exists())
        measured = [row for row in self.events(output) if row["total"]]
        self.assertEqual([(row["completed"],row["total"],row["item_id"]) for row in measured],
                         [(750000,4000000,"video-1")])

    def test_concurrent_contexts_capture_their_worker_item_and_cleanup_on_cancel(self):
        output = io.StringIO()
        barrier = threading.Barrier(2)
        paths, failures = [], []
        def encode(identifier, measured, cancelled):
            try:
                telemetry.bind_item(identifier)
                try:
                    with telemetry.encoding_progress(self.command, 2) as command:
                        path = Path(command[-2])
                        paths.append(path)
                        path.write_text(f"out_time_us={measured}\nprogress=continue\n")
                        barrier.wait(timeout=2)
                        # Reporter identity must survive later worker rebinding.
                        telemetry.bind_item("later-item")
                        if cancelled:
                            raise InterruptedError("cancelled")
                except InterruptedError:
                    if not cancelled:
                        raise
            except Exception as error:
                failures.append(error)
        with contextlib.redirect_stdout(output):
            threads = [threading.Thread(target=encode, args=("first",250000,False)),
                       threading.Thread(target=encode, args=("second",1250000,True))]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))
        measured = {(row["item_id"], row["completed"], row["total"])
                    for row in self.events(output) if row["total"]}
        self.assertEqual(measured, {("first",250000,2000000),("second",1250000,2000000)})

    def test_setup_failure_preserves_command_and_processing_exception(self):
        telemetry.bind_item("video-1")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
                telemetry.tempfile, "mkstemp", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                with telemetry.encoding_progress(self.command, 4) as command:
                    self.assertIs(command, self.command)
                    raise RuntimeError("encoder failed")
        self.assertEqual([(row["completed"],row["total"]) for row in self.events(output)], [(0,0)])

    def test_exception_reaps_reporter_and_removes_file(self):
        telemetry.bind_item("video-1")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "failed"):
            with telemetry.encoding_progress(self.command, 1) as command:
                path = Path(command[-2])
                raise ValueError("failed")
        self.assertFalse(path.exists())
        self.assertFalse(any(thread.name == "pycut-encoding-progress" for thread in threading.enumerate()))

    def test_reporter_start_failure_removes_telemetry_and_preserves_command(self):
        telemetry.bind_item("video-1")
        real_create = tempfile.mkstemp
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), patch.object(
                telemetry.tempfile, "mkstemp", side_effect=lambda **values: real_create(dir=folder, **values)), patch.object(
                threading.Thread, "start", side_effect=RuntimeError("unavailable")):
            with telemetry.encoding_progress(self.command, 1) as command:
                self.assertIs(command, self.command)
                self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
