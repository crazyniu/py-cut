"""dedup --file must reject non-MP4 inputs before processing."""
import argparse
import tempfile
import unittest
from pathlib import Path

from pycut.commands.dedup_cmd import _pick_srcs


class DedupFileValidationTests(unittest.TestCase):
    def test_accepts_mp4_and_uppercase_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "one.mp4", Path(directory) / "two.MP4"]
            for path in paths:
                path.write_bytes(b"test")
            self.assertEqual(_pick_srcs(argparse.Namespace(file=list(map(str, paths)))), paths)

    def test_rejects_non_mp4_and_mixed_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "ok.mp4"
            good.write_bytes(b"test")
            for suffix in (".txt", ".mov", ".jpg"):
                bad = Path(directory) / ("invalid" + suffix)
                bad.write_bytes(b"test")
                with self.subTest(suffix=suffix), self.assertRaisesRegex(SystemExit, "仅支持 MP4"):
                    _pick_srcs(argparse.Namespace(file=[str(good), str(bad)]))

    def test_rejects_missing_file_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SystemExit, "文件不存在"):
                _pick_srcs(argparse.Namespace(file=[str(Path(directory) / "missing.mp4")]))
            with self.assertRaisesRegex(SystemExit, "文件不存在"):
                _pick_srcs(argparse.Namespace(file=[directory]))


if __name__ == "__main__":
    unittest.main()
