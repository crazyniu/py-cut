"""Per-video counts, final confirmation, backtracking, and fourth-menu routing."""
import curses
from unittest import mock

import pytest

from pycut import cli, config, tui
from test_tui_integration import FakeScreen


@pytest.mark.parametrize("size", [(14, 60), (24, 100)])
def test_individual_counts_skip_and_backtracking_keep_generated_commands(tmp_path, size):
    root = tmp_path / "原视频"
    for name in ("a.mp4", "nested/b.mp4", "skip.mov"):
        source = root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
    # Sorted scanner order is a.mp4, skip.mov, nested/b.mp4.
    terminal = FakeScreen([10, ord("2"), 10, 10, ord("0"), 10, 10, ord("3"), 10,
                           10, 27, 10, curses.KEY_RIGHT, 10], size=size)
    with mock.patch.object(config, "SRC_DIR", root), mock.patch.object(tui, "_attr", return_value=0):
        commands = tui._wizard_fission(terminal, size[1])
    parsed = [cli.build_parser().parse_args(command) for command in commands]
    assert [arg.copies for arg in parsed] == [2, 3]
    assert [arg.file for arg in parsed] == [str(root / "a.mp4"), str(root / "nested/b.mp4")]
    assert [arg.out_dir for arg in parsed] == [str(tmp_path / "抖音裂变"), str(tmp_path / "抖音裂变/nested")]
    drawn = "\n".join(text for _, _, text in terminal.printed)
    assert "共生成 5 个副本" in drawn
    assert "未设置" in drawn
    assert terminal.keys == []


def test_every_video_needs_a_count_and_empty_selection_does_not_execute(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"video")
    terminal = FakeScreen([curses.KEY_DOWN, 10, curses.KEY_UP, 10, ord("0"), 10, 10, 27])
    with mock.patch.object(config, "SRC_DIR", tmp_path), \
         mock.patch.object(tui, "_attr", return_value=0), \
         mock.patch.object(tui, "_flash") as flash, \
         mock.patch.object(tui, "_confirm") as confirm:
        assert tui._wizard_fission(terminal, 100) is None
    assert flash.call_count == 2
    confirm.assert_not_called()


def test_fourth_shortcut_opens_fission_and_missing_source_opens_settings():
    with mock.patch.object(tui.curses, "curs_set"), \
         mock.patch.object(tui, "_attr", return_value=0), \
         mock.patch.object(tui, "_run_feature_wizard", return_value=[["fission"]]) as wizard:
        assert tui._loop(FakeScreen([ord("4")])) == ("run", [["fission"]])
    assert wizard.call_args.args[1] == "fission"
    with mock.patch.object(config, "refresh"), \
         mock.patch("pycut.settings.effective", return_value={}), \
         mock.patch("pycut.web_config.start", return_value="http://127.0.0.1:1234/"), \
         mock.patch.object(tui, "_flash") as flash, \
         mock.patch.object(tui, "_wizard_fission") as task:
        assert tui._run_feature_wizard(None, "fission", 100) is None
    assert "原视频目录尚未配置" in flash.call_args.args[1]
    task.assert_not_called()
