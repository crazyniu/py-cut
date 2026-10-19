"""Empty-timeline copy failure cleans temporary PIP files and preserves previous output."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from pycut.core import montage_pip


class PipEmptyTimelineTests(unittest.TestCase):
    def test_copy_and_fallback_fail_keep_existing_final_and_clean_temp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "base.mp4"
            src.write_bytes(b"base")
            out = root / "previous.mp4"
            out.write_bytes(b"known good" * 200)
            tmp = montage_pip._tmp_path(out)

            def fail_encoder(_cmd, timeout):
                tmp.write_bytes(b"partial" * 250)
                return 1, "", "encoder failed"
            info = SimpleNamespace(width=720, height=1280, duration=7.0)
            with mock.patch.object(montage_pip, "probe_video", return_value=info), \
                 mock.patch.object(montage_pip, "run_cmd",
                                   side_effect=fail_encoder) as run, \
                 mock.patch.object(montage_pip, "log"):
                with self.assertRaisesRegex(RuntimeError, "转存 base 失败"):
                    montage_pip.build_pip_cmd(src, [], out)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(tmp.exists())
            self.assertEqual(out.read_bytes(), b"known good" * 200)

    def test_copy_failure_fallback_success_replaces_final_without_temp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "base.mp4"
            src.write_bytes(b"base")
            out = root / "previous.mp4"
            out.write_bytes(b"known good" * 200)
            tmp = montage_pip._tmp_path(out)
            attempts = 0
            def encode(_cmd, timeout):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    tmp.write_bytes(b"broken" * 200)
                    return 1, "", "copy error"
                self.assertFalse(tmp.exists(), "fallback must start without stale copy output")
                tmp.write_bytes(b"new clean" * 200)
                return 0, "", ""
            info = SimpleNamespace(width=720, height=1280, duration=7.0)
            with mock.patch.object(montage_pip, "probe_video", return_value=info), \
                 mock.patch.object(montage_pip, "run_cmd",
                                   side_effect=encode) as run, \
                 mock.patch.object(montage_pip, "log"):
                montage_pip.build_pip_cmd(src, [], out)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(tmp.exists())
            self.assertEqual(out.read_bytes(), b"new clean" * 200)


if __name__ == "__main__":
    unittest.main()
