"""Supplied-clip preview and internal slicing scope over real temporary file trees."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import cli, config, tui
from pycut.commands import slice_cmd
from pycut.core import scan


def slice_parser():
    import argparse
    parser = argparse.ArgumentParser()
    slice_cmd.register(parser.add_subparsers(dest="command"))
    return parser


class SliceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self._touch("a.mp4")
        self._touch("d1/d2/d3/b.MP4")
        self._touch("d1/d2/d3/d4/too-deep.mp4")
        self._touch("d1/d2/d3/ignored.mov")
        self._touch(".hidden/secret.mp4")
        self._touch("System/hidden.mp4")
        self._touch("node_modules/dependency.mp4")
        try:
            (self.root / "alias.mp4").symlink_to(self.root / "a.mp4")
            self.link_created = True
        except (OSError, NotImplementedError):
            self.link_created = False

    def _touch(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"dummy content")
        return path

    def _selected_by_cli(self, argv):
        args = slice_parser().parse_args(argv)
        # The real command updates config.SRC_DIR before calling _pick_files.
        with mock.patch.object(config, "SRC_DIR", self.root):
            return slice_cmd._pick_files(args)

    def test_internal_slice_parser_matches_bounded_scanner_paths(self):
        argv = ["slice", "--src-dir", str(self.root), "--max-depth", "3"]
        planned = [
            p for p in scan.list_videos_recursive(self.root, 3)
            if p.suffix.lower() == ".mp4"
        ]
        executed = self._selected_by_cli(argv)
        self.assertEqual(planned, executed)
        self.assertIn((self.root / "a.mp4").resolve(), {p.resolve() for p in planned})
        self.assertIn(self.root / "d1/d2/d3/b.MP4", planned)
        self.assertNotIn(self.root / "d1/d2/d3/d4/too-deep.mp4", planned)
        self.assertNotIn(self.root / "d1/d2/d3/ignored.mov", planned)
        self.assertNotIn(self.root / ".hidden/secret.mp4", planned)
        self.assertNotIn(self.root / "System/hidden.mp4", planned)
        self.assertNotIn(self.root / "node_modules/dependency.mp4", planned)
        if self.link_created:
            self.assertEqual(sum(p.resolve() == (self.root / "a.mp4").resolve() for p in planned), 1)

    def test_user_clip_preview_count_matches_recursive_mp4_scope(self):
        planned = [p for p in scan.list_videos_recursive(self.root, max_depth=None) if p.suffix.lower() == '.mp4']
        with mock.patch.object(config, 'CLIP_DIRS', [self.root]), \
             mock.patch.object(tui, '_confirm', return_value=True) as confirm:
            commands = tui._wizard_cover(mock.Mock(), 100)
        self.assertEqual(dict(confirm.call_args.args[1])['MP4 数量'], str(len(planned)))
        self.assertEqual(commands[0][0], 'cover')

    def test_unbounded_direct_cli_behavior_is_preserved(self):
        args = slice_parser().parse_args(["slice"])
        self.assertIsNone(args.max_depth)
        selected = self._selected_by_cli(["slice"])
        self.assertIn(self.root / "d1/d2/d3/d4/too-deep.mp4", selected)

    def test_same_stem_from_different_dirs_is_rejected_preflight(self):
        other = self._touch("d1/A.MP4")
        files = [self.root / "a.mp4", other]
        collisions = slice_cmd._colliding_stems(files)
        self.assertEqual(collisions, [files])

    def test_mov_only_scope_never_claims_processable_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "only.mov").write_bytes(b"dummy")
            with mock.patch.object(config, "CLIP_DIRS", [directory]), \
                 mock.patch.object(tui, "_flash") as flash, \
                 mock.patch.object(tui, "_confirm") as confirm:
                self.assertIsNone(tui._wizard_cover(mock.Mock(), 100))
                flash.assert_called_once()
                confirm.assert_not_called()
            with mock.patch.object(config, "SRC_DIR", directory):
                self.assertEqual(
                    slice_cmd._pick_files(slice_parser().parse_args(
                        ["slice", "--max-depth", "3", "--src-dir", str(directory)
                         ])), []
                )


if __name__ == "__main__":
    unittest.main()
