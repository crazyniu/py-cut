"""TUI 切片预览与 CLI 执行范围的一致性回归：真实临时文件树。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import cli, config, tui
from pycut.commands import slice_cmd
from pycut.core import scan


class SliceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
        args = cli.build_parser().parse_args(argv)
        # _pick_files 是 slice_cmd.run 设置 config.SRC_DIR 后执行的逻辑。
        with mock.patch.object(config, "SRC_DIR", self.root):
            return slice_cmd._pick_files(args)

    def test_default_tui_plan_matches_actual_slice_candidate_paths(self):
        argv = ["slice", "--src-dir", str(self.root), "--max-depth", "3"]
        planned = [
            p for p in scan.list_videos_recursive(self.root, 3)
            if p.suffix.lower() == ".mp4"
        ]
        executed = self._selected_by_cli(argv)
        self.assertEqual(planned, executed)
        self.assertIn(self.root / "a.mp4", planned)
        self.assertIn(self.root / "d1/d2/d3/b.MP4", planned)
        self.assertNotIn(self.root / "d1/d2/d3/d4/too-deep.mp4", planned)
        self.assertNotIn(self.root / "d1/d2/d3/ignored.mov", planned)
        self.assertNotIn(self.root / ".hidden/secret.mp4", planned)
        self.assertNotIn(self.root / "System/hidden.mp4", planned)
        self.assertNotIn(self.root / "node_modules/dependency.mp4", planned)
        if self.link_created:
            self.assertIn(self.root / "alias.mp4", planned)

    def test_confirm_preview_count_equals_real_cli_pick_for_same_scope(self):
        planned = [
            p for p in scan.list_videos_recursive(self.root, 3)
            if p.suffix.lower() == ".mp4"
        ]

        def confirm(_screen, lines, toggles, out_vals):
            self.assertEqual(dict(lines)["视频数量"], f"{len(planned)} 个")
            out_vals[:] = [False, False]
            return True

        with mock.patch.object(config, "SRC_DIR", self.root), \
             mock.patch.object(tui, "_confirm", side_effect=confirm):
            cmds = tui._wizard_slice(mock.Mock(), 100)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][:5], [
            "slice", "--src-dir", str(self.root), "--max-depth", "3",
        ])
        self.assertIn("--no-sub", cmds[0])
        self.assertEqual(
            self._selected_by_cli(cmds[0]),
            planned,
        )

    def test_unbounded_direct_cli_behavior_is_preserved(self):
        args = cli.build_parser().parse_args(["slice"])
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
            with mock.patch.object(tui, "_pick_dir", side_effect=[str(directory), None]), \
                 mock.patch.object(tui, "_flash") as flash, \
                 mock.patch.object(tui, "_confirm") as confirm:
                self.assertIsNone(tui._wizard_slice(mock.Mock(), 100))
                flash.assert_called_once()
                confirm.assert_not_called()
            with mock.patch.object(config, "SRC_DIR", directory):
                self.assertEqual(
                    slice_cmd._pick_files(cli.build_parser().parse_args(
                        ["slice", "--max-depth", "3", "--src-dir", str(directory)
                         ])), []
                )


if __name__ == "__main__":
    unittest.main()
