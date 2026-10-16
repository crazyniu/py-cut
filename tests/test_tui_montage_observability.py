"""Montage TUI session logs are durable, redacted and reflect published files."""
from __future__ import annotations

from collections import deque
import io
import json
import time
import pytest
from pathlib import Path
from unittest import mock

from pycut import tui, tui_runner
from pycut.core import montage_pip
from pycut.core.progress import PREFIX
from test_tui_layout import Screen


def _jobs(folder: Path) -> list[list[str]]:
    return [["montage", "--source", "/reference/1.mp4", "--fission", "1",
             "--out-dir", str(folder)]]


def test_session_log_created_outside_video_root_without_overwrite(tmp_path):
    jobs = _jobs(tmp_path / "结果目录")
    first = tui_runner._montage_plan(jobs)
    tui_runner._init_montage_log(first, jobs)
    filename = Path(first["log_path"])
    assert filename.is_file()
    assert filename.parent == tmp_path / "结果目录_logs"
    assert not list(Path(jobs[0][-1]).glob("*.log"))
    assert "会话开始" in filename.read_text(encoding="utf-8")
    tui_runner._append_run_log(first, "processing line")
    assert "processing line" in filename.read_text(encoding="utf-8")

    second = tui_runner._montage_plan(jobs)
    tui_runner._init_montage_log(second, jobs)
    assert second["log_path"] != first["log_path"]
    assert "processing line" in filename.read_text(encoding="utf-8")


def test_streaming_child_log_is_redacted_and_collects_only_verified_files(tmp_path):
    jobs = _jobs(tmp_path)
    batch = tui_runner._montage_plan(jobs)
    tui_runner._init_montage_log(batch, jobs)
    key = "sk-test-secret-token"
    video = tmp_path / "中文 空格" / "1_1.mp4"
    event = dict(stage="混剪合成", completed=5, total=5, success=1,
                 file=str(video), output=str(video.parent))
    stream = io.StringIO("开始合成\n" + PREFIX + json.dumps(event, ensure_ascii=False)
                         + "\nFFmpeg 输出 " + key + " 已结束\n")
    fake = mock.Mock(stdout=stream)
    fake.poll.return_value = 0
    screen = mock.Mock()
    screen.getch.return_value = -1
    history = deque(maxlen=600)
    with mock.patch.object(tui_runner.subprocess, "Popen", return_value=fake), \
         mock.patch.object(tui_runner, "_draw"):
        assert tui_runner._run_one(screen, [*jobs[0], "--llm-key", key], 1, 1,
                                   history, batch=batch) == (0, False)

    disk_log = Path(batch["log_path"]).read_text(encoding="utf-8")
    assert "开始合成" in disk_log
    assert 'pycut-progress' in disk_log and "中文 空格" in disk_log
    assert key not in disk_log and "********" in disk_log
    assert key not in "\n".join(history)
    assert batch["files"] == [str(video)]
    assert not any(PREFIX in line for line in history if not line.startswith(PREFIX))


def test_log_write_failure_reports_warning_and_keeps_media_flow(tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("file", encoding="utf-8")
    jobs = _jobs(parent)
    batch = tui_runner._montage_plan(jobs)
    tui_runner._init_montage_log(batch, jobs)
    assert "log_path" not in batch
    assert "无法在日志目录写入日志" in batch["log_error"]
    tui_runner._append_run_log(batch, "no crash")


def test_log_retains_long_errors_beyond_the_screen_cap(tmp_path):
    jobs = _jobs(tmp_path)
    batch = tui_runner._montage_plan(jobs)
    tui_runner._init_montage_log(batch, jobs)
    data = "x" * 10000
    stream = io.StringIO("[失败] " + data + "\n")
    fake = mock.Mock(stdout=stream)
    fake.poll.return_value = 2
    history = deque(maxlen=600)
    screen = mock.Mock()
    screen.getch.return_value = -1
    with mock.patch.object(tui_runner.subprocess, "Popen", return_value=fake), \
         mock.patch.object(tui_runner, "_draw"):
        assert tui_runner._run_one(screen, jobs[0], 1, 1, history, batch=batch) == (2, False)
    saved = Path(batch["log_path"]).read_text(encoding="utf-8")
    assert data in saved
    assert any("日志行已截断" in line for line in history)


def test_montage_dashboard_displays_scrollable_logs_and_finished_directory(tmp_path):
    jobs = _jobs(tmp_path)
    batch = tui_runner._montage_plan(jobs)
    video = str(tmp_path / "子目录" / "1_1.mp4")
    batch.update(current_planned=1, log_path=str(tmp_path / "session.log"),
                 files=[video], completed=1)
    screen = Screen(size=(30, 125))
    with mock.patch.object(tui, "_attr", return_value=0):
        tui_runner._draw(screen, jobs[0], 1, 1,
                         deque(["[画中画] FFmpeg 正在合成画面 · 已等待 10秒", "其他详细日志"]),
                         0, "running", batch=batch, show_details=True)
    running = "\n".join(t for _, _, t in screen.printed)
    assert "实时日志" in running and "其他详细日志" in running
    assert "日志文件：" in running and "session.log" in running
    screen = Screen(size=(30, 125))
    with mock.patch.object(tui, "_attr", return_value=0):
        tui_runner._draw(screen, jobs[0], 1, 1, deque(), 0,
                         "success", batch=batch, results=[{}])
    ended = "\n".join(t for _, _, t in screen.printed)
    assert "输出目录：" + str(tmp_path / "子目录") in ended
    assert "最新成片：" not in ended and "成片文件：" not in ended
    assert "1_1.mp4" not in ended
    assert "100%" in ended


def test_montage_result_deduplicates_folders_and_preserves_log_file(tmp_path):
    jobs = _jobs(tmp_path)
    first = tmp_path / "话术 甲"
    second = tmp_path / "话术 乙"
    files = [str(first / "1_1.mp4"), str(first / "1_2.mp4"),
             str(second / "1_1.mp4")]
    batch = tui_runner._montage_plan(jobs)
    batch.update(completed=3, files=files,
                 log_path=str(tmp_path / "pycut-run.log"))
    screen = Screen(size=(32, 160))
    with mock.patch.object(tui, "_attr", return_value=0):
        tui_runner._draw(screen, jobs[0], 1, 1, deque(["[记录] 处理完成"]),
                         0, "success", batch=batch, results=[{}],
                         show_details=True)
    rendered = "\n".join(text for _, _, text in screen.printed)
    assert rendered.count("输出目录：" + str(first)) == 1
    assert rendered.count("输出目录：" + str(second)) == 1
    assert "最新成片：" not in rendered and "成片文件：" not in rendered
    assert "1_1.mp4" not in rendered and "1_2.mp4" not in rendered
    assert "日志文件：" + str(tmp_path / "pycut-run.log") in rendered
    assert batch["files"] == files  # Only presentation changes, real files are retained.
    log_y = next(y for y, _, text in screen.printed if text.startswith("日志文件："))
    heading_y = next(y for y, _, text in screen.printed
                     if text.startswith("其他输出目录与日志"))
    assert heading_y > log_y  # Do not overwrite the end of the log-file path.


def test_montage_result_uses_one_folder_and_keeps_log_heading_below(tmp_path):
    jobs = _jobs(tmp_path)
    directory = tmp_path / "目录 空格"
    batch = tui_runner._montage_plan(jobs)
    batch.update(completed=2, files=[str(directory / "1_1.mp4"),
                                      str(directory / "1_2.mp4")],
                 log_path=str(tmp_path / "run.log"))
    screen = Screen(size=(30, 160))
    with mock.patch.object(tui, "_attr", return_value=0):
        tui_runner._draw(screen, jobs[0], 1, 1, deque(["普通日志"]),
                         0, "success", batch=batch, results=[{}],
                         show_details=True)
    rendered = "\n".join(text for _, _, text in screen.printed)
    assert rendered.count("输出目录：" + str(directory)) == 1
    assert "1_1.mp4" not in rendered and "1_2.mp4" not in rendered
    log_y = next(y for y, _, text in screen.printed if text.startswith("日志文件："))
    title_y = next(y for y, _, text in screen.printed if text == "详细日志")
    assert title_y > log_y


def test_montage_failure_without_published_file_has_no_output_directory(tmp_path):
    jobs = _jobs(tmp_path)
    batch = tui_runner._montage_plan(jobs)
    batch.update(completed=0, files=[], log_path=str(tmp_path / "run.log"))
    screen = Screen(size=(28, 120))
    with mock.patch.object(tui, "_attr", return_value=0):
        tui_runner._draw(screen, jobs[0], 1, 1, deque(["[失败] 没有完成成片"]),
                         0, "error", batch=batch, results=[{}])
    rendered = "\n".join(text for _, _, text in screen.printed)
    assert "输出目录：" not in rendered
    assert "错误日志" in rendered and "没有完成成片" in rendered
    assert "日志文件：" + str(tmp_path / "run.log") in rendered


def test_run_jobs_records_task_boundaries_and_interrupt_outcome(tmp_path):
    jobs = _jobs(tmp_path)
    screen = mock.Mock()
    screen.getch.return_value = 10
    def fake_run(_screen, _argv, _index, _total, history, batch):
        history.append("[中断] FFmpeg 被取消")
        return 130, True
    with mock.patch.object(tui_runner, "_run_one", side_effect=fake_run), \
         mock.patch.object(tui_runner, "_draw"), \
         mock.patch("curses.flushinp"):
        assert tui_runner.run_jobs(screen, jobs) == (130, False)
    saved = list((tmp_path.parent / (tmp_path.name + "_logs")).glob("*.log"))
    assert len(saved) == 1
    txt = saved[0].read_text(encoding="utf-8")
    assert "任务 1/1" in txt and "退出码 130" in txt and "[执行结果] 已中断" in txt




def test_video_output_log_is_sibling_of_out_tree(tmp_path):
    out_root = tmp_path / "剪辑" / "out" / "混剪"
    jobs = _jobs(out_root)
    batch = tui_runner._montage_plan(jobs)
    tui_runner._init_montage_log(batch, jobs)
    log_file = Path(batch["log_path"])
    assert log_file.parent == tmp_path / "剪辑" / "logs" / "混剪"
    assert log_file.exists()
    assert not list(out_root.rglob("*.log"))


def test_montage_stage_uses_latest_real_measurement_and_does_not_guess():
    events = {"混剪合成": {"stage": "混剪合成", "completed": 1, "total": 5},
              "背景素材扫描": {"stage": "背景素材扫描", "completed": 140, "total": 509},
              "画中画素材扫描": {"stage": "画中画素材扫描", "completed": 12, "total": 25}}
    assert tui_runner._current_montage_stage(events)["stage"] == "画中画素材扫描"
    events["背景片段编码"] = {"stage": "背景片段编码", "completed": 8, "total": 32}
    assert tui_runner._current_montage_stage(events)["completed"] == 8
    events["混剪合成"] = {"stage": "混剪合成", "completed": 2, "total": 5}
    assert tui_runner._current_montage_stage(events) is None
    events["画中画编码"] = {"stage": "画中画编码", "completed": 900, "total": 3600}
    assert tui_runner._current_montage_stage(events)["completed"] == 900


@pytest.mark.parametrize("stage,number,total", [
    ("背景素材扫描", 140, 509),
    ("背景片段编码", 8, 32),
    ("画中画编码", 900, 3600),
])
def test_montage_stage_has_its_own_real_progress_bar(stage, number, total):
    screen = Screen(size=(29, 112))
    jobs = _jobs(Path("/tmp/out"))
    batch = tui_runner._montage_plan(jobs)
    batch.update(current_planned=1)
    phase = 2 if stage == "画中画编码" else 1
    event = {"stage": stage, "completed": number, "total": total,
             "detail": "实际完成计数"}
    statuses = {"混剪合成": {"stage": "混剪合成", "completed": phase, "total": 5},
                stage: event}
    with mock.patch.object(tui, "_attr", return_value=0):
        tui_runner._draw(screen, jobs[0], 1, 1, deque(["普通日志"]), 0,
                         "running", statuses, batch=batch, show_details=True)
    lines = "\n".join(text for _, _, text in screen.printed)
    assert "已生成 0 个（成片完成率）" in lines
    assert f"{stage} {number}/{total}" in lines
    assert f"{number * 100 // total}% · 本阶段" in lines
    assert sum(text.startswith("[") and ("█" in text or "░" in text)
               for _, _, text in screen.printed) == 2

# PiP: long FFmpeg calls report elapsed time and preserve the original command.
def test_monitored_ffmpeg_heartbeat_and_exact_command_unchanged():
    command = ["ffmpeg", "-hide_banner", "-i", "video.mp4", "output.mp4"]
    def fake_run(args, timeout):
        time.sleep(0.09)
        return 0, "", ""
    with mock.patch.object(montage_pip, "run_cmd", side_effect=fake_run) as run, \
         mock.patch.object(montage_pip, "log") as log:
        result = montage_pip._run_with_activity(command, timeout=30, interval=0.015)
    assert result == (0, "", "")
    run.assert_called_once_with(command, timeout=30)
    messages = [str(call.args[0]) for call in log.call_args_list]
    assert any("编码器尚未返回" in m and "已等待" in m for m in messages)
    assert "调用结束" in messages[-1]
    assert not any("进度=" in m or "100%" in m for m in messages)


def test_monitored_ffmpeg_failure_always_stops_reporter():
    with mock.patch.object(montage_pip, "run_cmd", side_effect=OSError("ffmpeg missing")), \
         mock.patch.object(montage_pip, "log") as log:
        with pytest.raises(OSError, match="ffmpeg missing"):
            montage_pip._run_with_activity(["ffmpeg"], timeout=2, interval=0.01)
        calls_at_exit = log.call_count
        time.sleep(0.04)
        assert log.call_count == calls_at_exit
