"""不需要真实视频、ffmpeg 或交互终端的 TUI/CLI 回归测试。

运行：python3 -m unittest discover -s tests -p 'test_tui_regression.py' -v
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import cli, config, tui, tui_runner
from pycut.commands import slice_cmd
from pycut.core import scan


class Screen:
    """仅实现这些用例所用的 curses stdscr 协议。"""

    def __init__(self, keys=(), size=(24, 100)):
        self.keys = list(keys)
        self.size = size
        self.printed = []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, value, attr=0):
        assert 0 <= y < self.size[0]
        assert 0 <= x < self.size[1]
        assert x + tui._disp_width(value) < self.size[1]
        self.printed.append((y, x, value))

    def getch(self):
        return self.keys.pop(0) if self.keys else 27

    def get_wch(self):
        return self.getch()

    def erase(self):
        pass

    def move(self, y, x):
        pass

    def clrtoeol(self):
        pass

    def refresh(self):
        pass

    def keypad(self, enabled):
        pass

    def timeout(self, delay):
        pass


class TUIRegression(unittest.TestCase):
    def setUp(self):
        self.attr = mock.patch.object(tui, "_attr", return_value=0)
        self.attr.start()

    def tearDown(self):
        self.attr.stop()

    def test_unicode_text_input_and_backspace(self):
        s = Screen(["婚", "礼", "a", "\b", "片", "\n"])
        self.assertEqual(tui._prompt_text(s, 4, 2, 100, "关键词"), "婚礼片")
        self.assertIsNone(tui._prompt_text(Screen(["\x1b"]), 4, 2, 100, "关键词"))

    def test_number_input_can_replace_default_and_reject_out_of_range(self):
        self.assertEqual(tui._prompt_int(Screen([ord("1"), ord("2"), 10]),
                                         5, 2, 100, "裂变", 1, 1, 99), 12)
        self.assertEqual(tui._prompt_int(
            Screen([ord("0"), 10, 127, ord("3"), 10]),
            5, 2, 100, "裂变", 1, 1, 99), 3)
        self.assertIsNone(tui._prompt_int(Screen([27]), 5, 2, 100, "裂变", 1, 1, 99))

    def test_multi_select_supports_two_digit_fission_and_empty_input(self):
        files = [Path("/tmp/婚礼.mp4"), Path("/tmp/采访.mp4")]
        s = Screen([ord(" "), ord("e"), ord("1"), ord("2"), 10, 10])
        chosen, counts = tui._multi_select(s, "混剪", files)
        self.assertEqual(chosen, [files[0]])
        self.assertEqual(counts[str(files[0])], 12)
        self.assertIsNone(tui._multi_select(Screen(), "混剪", []))

    def test_montage_escape_returns_without_unpack_error(self):
        files = [Path("/tmp/a.mp4")]
        with mock.patch.object(tui, "_pick_dir", side_effect=["/tmp", "/tmp", "/tmp", None, None, None]), \
             mock.patch("pycut.core.montage_clips._collect_files", return_value=[Path("candidate.mp4")] * 2), \
             mock.patch.object(tui, "list_videos_flat", return_value=files), \
             mock.patch.object(tui, "_multi_select", return_value=None), \
             mock.patch.object(tui, "_confirm") as confirm:
            self.assertIsNone(tui._wizard_montage(Screen(), 100))
            confirm.assert_not_called()

    def test_slice_preview_matches_cli_max_depth_and_mp4_only(self):
        files = [Path("/tmp/a.mp4"), Path("/tmp/a.mov"), Path("/tmp/b.MP4")]
        def confirm(scr, lines, toggles, output):
            self.assertEqual(lines[-1][1], "2 个")
            output[:] = [False, True]
            return True
        with mock.patch.object(config, "SRC_DIR", Path("/tmp")), \
             mock.patch.object(scan, "list_videos_recursive", return_value=files), \
             mock.patch.object(tui, "_confirm", side_effect=confirm):
            commands = tui._wizard_slice(Screen(), 100)
        slice_args = cli.build_parser().parse_args(commands[0])
        dedup_args = cli.build_parser().parse_args(commands[1])
        self.assertEqual(slice_args.max_depth, 3)
        self.assertTrue(slice_args.no_sub)
        self.assertTrue(slice_args.out_dir)
        self.assertEqual(dedup_args.src_dir, slice_args.out_dir)
        self.assertFalse(dedup_args.all)
        self.assertTrue(dedup_args.force)

    def test_scan_max_depth_ignores_deep_and_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Volumes").mkdir()
            (root / "System").mkdir()
            (root / ".hidden").mkdir()
            self.assertIn(root / "Volumes", scan.list_dirs(root))
            self.assertIn(root / "System", scan.list_dirs(root))
            self.assertNotIn(root / ".hidden", scan.list_dirs(root))
            # Browser listing does not change processing's recursive scope.
            hidden_video = root / ".hidden" / "hidden.mp4"
            system_video = root / "System" / "hidden.mp4"
            hidden_video.touch()
            system_video.touch()
            self.assertNotIn(hidden_video, scan.list_videos_recursive(root))
            self.assertNotIn(system_video, scan.list_videos_recursive(root))
            near = root / "d1" / "d2" / "d3"
            far = near / "d4"
            far.mkdir(parents=True)
            (near / "near.mp4").write_bytes(b"")
            (far / "far.mp4").write_bytes(b"")
            (near / "other.mov").write_bytes(b"")
            opts = cli.build_parser().parse_args([
                "slice", "--src-dir", str(root), "--max-depth", "3",
            ])
            with mock.patch.object(config, "SRC_DIR", root):
                self.assertEqual(slice_cmd._pick_files(opts), [near / "near.mp4"])
            self.assertEqual(
                cli.build_parser().parse_args(["slice"]).max_depth, None,
            )

    def test_safe_render_does_not_split_wide_characters(self):
        s = Screen(size=(20, 10))
        tui._safe_add(s, 2, 2, "中文abcdefghijklmnop", 10)
        self.assertEqual(s.printed[0][2], "中文abc")
        self.assertLessEqual(tui._disp_width(s.printed[0][2]), 7)
        s2 = Screen(size=(10, 20))
        tui._safe_add(s2, 40, 2, "should not display", 20)
        self.assertFalse(s2.printed)

    def test_terminal_size_has_clear_fallback(self):
        s = Screen(size=(10, 40))
        self.assertTrue(tui._small_screen(s))
        self.assertTrue(any("终端窗口太小" in t for _, _, t in s.printed))

    def test_slice_summary_fails_on_encoder_or_subtitle_error(self):
        from pycut.core.slice_pipeline import VideoResult
        llm = mock.Mock(failures=0, switched_to=None, calls=0)
        manifest = mock.Mock(csv_path=Path("/tmp/manifest.csv"))
        with mock.patch.object(slice_cmd, "log"):
            self.assertEqual(slice_cmd._summary(
                [VideoResult(stem="a", failed=1)], llm, manifest, 0, 0, 0), 2)
            self.assertEqual(slice_cmd._summary(
                [VideoResult(stem="a", sub_fail=1)], llm, manifest, 0, 0, 0), 2)
            self.assertEqual(slice_cmd._summary(
                [VideoResult(stem="a", segs=1)], llm, manifest, 0, 0, 0), 0)

    def test_runner_sanitizes_logs_and_redacts_secrets(self):
        self.assertEqual(tui_runner._sanitize("a\x1b[31m红\x1b[0m"), "a红")
        self.assertNotIn("secret", tui_runner._display_command(
            ["slice", "--llm-key", "secret", "--llm-key=another"]
        ))
        self.assertEqual(tui_runner.run_jobs.__name__, "run_jobs")

    def test_home_number_shortcut_dispatches_selected_wizard(self):
        s = Screen(keys=[ord("4")])
        with mock.patch.object(tui.curses, "curs_set"), \
             mock.patch.object(tui, "_run_feature_wizard",
                               return_value=[["montage"]]) as wizard:
            result = tui._loop(s)
        self.assertEqual(result, ("run", [["montage"]]))
        self.assertEqual(wizard.call_args.args[1], "montage")

    def test_runner_aborts_followup_command_when_first_fails(self):
        s = Screen(keys=[10])
        with mock.patch.object(tui_runner, "_run_one", return_value=(2, False)) as one:
            code, should_quit = tui_runner.run_jobs(
                s, [["slice", "--no-sub"], ["dedup", "--all"]]
            )
        self.assertEqual((code, should_quit), (2, False))
        self.assertEqual(one.call_count, 1)

    def test_runner_preserves_user_cancellation(self):
        s = Screen(keys=[10])
        with mock.patch.object(tui_runner, "_run_one", return_value=(130, True)):
            code, should_quit = tui_runner.run_jobs(s, [["slice"]])
        self.assertEqual((code, should_quit), (130, False))

    def test_montage_returns_failure_if_pipeline_errors(self):
        from pycut.commands import montage_cmd
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source.mp4"
            src.write_bytes(b"not a real video")
            args = cli.build_parser().parse_args([
                "montage", "--source", str(src), "--no-sub"
            ])
            with mock.patch.object(montage_cmd, "self_check"), \
                 mock.patch.object(montage_cmd, "install_signal_handlers"), \
                 mock.patch.object(montage_cmd, "reset_stop"), \
                 mock.patch.object(montage_cmd, "_resolve_srt", return_value=""), \
                 mock.patch("pycut.commands.montage_pipeline.run_pipeline",
                            return_value={"error": "fake failure"}), \
                 mock.patch.object(montage_cmd, "log"):
                self.assertEqual(montage_cmd.run(args), 2)

    def test_cli_runner_launches_python_without_media(self):
        # 仅查询版本号；没有 ffmpeg、素材或 LLM 调用。
        s = Screen(keys=[-1] * 20, size=(24, 100))
        with mock.patch.object(tui, "_attr", return_value=0):
            code, cancelled = tui_runner._run_one(
                s, ["--version"], 1, 1, deque(maxlen=100),
            )
        self.assertEqual(code, 0)
        self.assertFalse(cancelled)


if __name__ == "__main__":
    unittest.main()
