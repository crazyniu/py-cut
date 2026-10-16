"""slice --file 的显式路径 / 历史名称匹配回归，无需 FFmpeg 或真实素材。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import cli, config
from pycut.commands import slice_cmd


class SliceFileArgumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _file(self, name: str, data: bytes = b"fake mp4") -> Path:
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        return file

    @staticmethod
    def _args(*flags):
        import argparse
        parser = argparse.ArgumentParser()
        slice_cmd.register(parser.add_subparsers(dest='command'))
        return parser.parse_args(["slice", *flags])

    def test_absolute_file_outside_missing_default_source(self):
        chosen = self._file("elsewhere/授权视频.MP4")
        with mock.patch.object(config, "SRC_DIR", self.root / "no-default-src"):
            self.assertEqual(
                slice_cmd._pick_files(self._args("--file", str(chosen))),
                [chosen.resolve()],
            )

    def test_relative_explicit_path_resolves_against_current_directory(self):
        chosen = self._file("sub/relative.mp4")
        relative = os.path.relpath(chosen, Path.cwd())
        self.assertIn("/", relative)  # 将其识别为路径，而不是文件名
        with mock.patch.object(config, "SRC_DIR", self.root / "missing"):
            self.assertEqual(
                slice_cmd._pick_files(self._args("--file", relative)),
                [chosen.resolve()],
            )

    def test_home_expansion_for_explicit_file(self):
        chosen = self._file("clip.mp4")
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}), \
             mock.patch.object(config, "SRC_DIR", self.root / "missing"):
            self.assertEqual(
                slice_cmd._pick_files(self._args("--file", "~/clip.mp4")),
                [chosen.resolve()],
            )

    def test_explicit_path_disambiguates_duplicate_basenames(self):
        inside = self._file("source/clip.mp4", b"one")
        outside = self._file("outside/clip.mp4", b"two")
        with mock.patch.object(config, "SRC_DIR", inside.parent):
            selected = slice_cmd._pick_files(
                self._args("--file", str(outside), "--max-depth", "0"))
        self.assertEqual(selected, [outside.resolve()])
        self.assertNotEqual(selected[0], inside.resolve())

    def test_missing_explicit_path_must_not_fall_back_to_matching_name(self):
        self._file("source/clip.mp4")
        missing = self.root / "missing" / "clip.mp4"
        with mock.patch.object(config, "SRC_DIR", self.root / "source"):
            with self.assertRaisesRegex(SystemExit, "--file 文件不存在"):
                slice_cmd._pick_files(self._args("--file", str(missing)))

    def test_explicit_non_mp4_and_directory_are_rejected(self):
        mov = self._file("outside/movie.mov")
        with self.assertRaisesRegex(SystemExit, "只支持 MP4"):
            slice_cmd._pick_files(self._args("--file", str(mov)))
        with self.assertRaisesRegex(SystemExit, "不是普通文件"):
            slice_cmd._pick_files(self._args("--file", str(mov.parent)))

    def test_negative_depth_still_rejected_for_direct_file(self):
        path = self._file("movie.mp4")
        with self.assertRaisesRegex(SystemExit, "--max-depth"):
            slice_cmd._pick_files(
                self._args("--file", str(path), "--max-depth", "-1"))

    def test_plain_keyword_keeps_existing_depth_and_case_insensitive_scan(self):
        chosen = self._file("source/INTRO.MP4")
        self._file("source/deep/intro-other.mp4")
        with mock.patch.object(config, "SRC_DIR", self.root / "source"):
            selected = slice_cmd._pick_files(
                self._args("--file", "intro", "--max-depth", "0"))
        self.assertEqual(selected, [chosen])
        with mock.patch.object(config, "SRC_DIR", self.root / "source"):
            selected = slice_cmd._pick_files(
                self._args("--file", "INTRO.MP4", "--max-depth", "0"))
        self.assertEqual(selected, [chosen])

    def test_run_direct_path_does_not_require_default_source(self):
        chosen = self._file("outside/real-file.mp4")
        args = self._args("--file", str(chosen), "--dry-run", "--no-llm")
        with mock.patch.object(config, "SRC_DIR", self.root / "missing"), \
             mock.patch.object(slice_cmd, "install_signal_handlers"), \
             mock.patch.object(slice_cmd, "reset_stop"), \
             mock.patch.object(slice_cmd, "self_check", return_value=object()) as check, \
             mock.patch.object(slice_cmd, "load_cascades", return_value=[]) as cascades, \
             mock.patch.object(slice_cmd, "bind_cascades"), \
             mock.patch.object(slice_cmd, "make_llm"), \
             mock.patch.object(slice_cmd, "Manifest"), \
             mock.patch.object(slice_cmd, "probe_video", return_value=None) as probe, \
             mock.patch.object(slice_cmd, "_stub", return_value=object()), \
             mock.patch.object(slice_cmd, "_summary", return_value=2), \
             mock.patch.object(slice_cmd, "log"):
            result = slice_cmd.run(args)
        self.assertEqual(result, 2)  # 只模拟探测失败，不伪造处理成功
        check.assert_called_once_with(require_src=False)
        cascades.assert_called_once()
        probe.assert_called_once_with(chosen.resolve())

    def test_run_filename_mode_keeps_original_self_check(self):
        chosen = self._file("source/clip.mp4")
        args = self._args("--file", "clip", "--max-depth", "0")
        with mock.patch.object(config, "SRC_DIR", chosen.parent), \
             mock.patch.object(slice_cmd, "install_signal_handlers"), \
             mock.patch.object(slice_cmd, "reset_stop"), \
             mock.patch.object(slice_cmd, "self_check", return_value=object()) as check, \
             mock.patch.object(slice_cmd, "load_cascades", return_value=[]), \
             mock.patch.object(slice_cmd, "bind_cascades"), \
             mock.patch.object(slice_cmd, "make_llm"), \
             mock.patch.object(slice_cmd, "Manifest"), \
             mock.patch.object(slice_cmd, "probe_video", return_value=None) as probe, \
             mock.patch.object(slice_cmd, "_stub", return_value=object()), \
             mock.patch.object(slice_cmd, "_summary", return_value=2), \
             mock.patch.object(slice_cmd, "log"):
            self.assertEqual(slice_cmd.run(args), 2)
        check.assert_called_once_with()
        probe.assert_called_once_with(chosen)


if __name__ == "__main__":
    unittest.main()
