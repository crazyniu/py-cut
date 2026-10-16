"""Long PiP encoding emits honest heartbeats without modifying FFmpeg commands."""
import time
from unittest import mock

import pytest
from pycut.core import montage_pip


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
