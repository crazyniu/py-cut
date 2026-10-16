"""仅验证本次视频处理性能修复的行为边界（DEV-462）。"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from pycut import config
from pycut.commands import montage_cmd
from pycut.core import fingerprint, montage_clips, subtitle


def test_probe_library_uses_single_scan_and_cached_statistics(tmp_path):
    sample = montage_clips.ClipItem(tmp_path / "a.mp4", 5, 720, 1280, False)

    def scan(_root, **kwargs):
        kwargs["stats"].update(files=3, portrait=2, landscape=1)
        return [sample]

    with mock.patch.object(montage_clips, "scan_library", side_effect=scan) as scanner, \
         mock.patch.object(montage_cmd, "_is_portrait", side_effect=AssertionError("duplicate probe")), \
         mock.patch.object(montage_cmd, "log") as logger:
        assert montage_cmd._probe_library(tmp_path, False, 0) == 0
    scanner.assert_called_once()
    lines = "\n".join(call.args[0] for call in logger.call_args_list)
    assert "3" in lines and "2" in lines and "1" in lines


def test_source_end_is_trimmed_not_repeated(tmp_path):
    item = montage_clips.ClipItem(tmp_path / "source.mp4", 1.05, 180, 320, False)
    item.path.write_bytes(b"test")
    row = {"item": item, "offset": 0.2, "take": 1.0, "speed": 1.2}
    commands = []

    def encode(args, *, timeout):
        commands.append(args)
        Path(args[-1]).write_bytes(b"video" * 300)
        return 0, "", ""

    with mock.patch.object(config, "MONTAGE_TMP", tmp_path / "work"), \
         mock.patch.object(montage_clips, "run_cmd", side_effect=encode):
        result = montage_clips._normalize_segments([row], (180, 320), 30, "20")
    assert len(result) == 1
    assert abs(row["take"] - 0.708) <= .001
    assert float(commands[0][commands[0].index("-t") + 1]) <= 0.85
    assert "tpad=" not in " ".join(commands[0])
    assert commands[0][commands[0].index("-frames:v") + 1] == str(round(row["take"] * 30))


def test_large_batch_encodes_with_bounded_parallelism_but_ordered_outputs(tmp_path):
    item = montage_clips.ClipItem(tmp_path / "source.mp4", 5, 180, 320, False)
    item.path.write_bytes(b"video")
    rows = [{"item": item, "offset": 0.0, "take": 0.4} for _ in range(6)]
    active, peak = 0, 0
    lock = threading.Lock()

    def encode(args, *, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.025)
        Path(args[-1]).write_bytes(b"video" * 300)
        with lock:
            active -= 1
        return 0, "", ""

    with mock.patch.object(config, "MONTAGE_TMP", tmp_path / "work"), \
         mock.patch.object(montage_clips, "run_cmd", side_effect=encode):
        paths = montage_clips._normalize_segments(rows, (180, 320), 30, "20")
    assert 1 < peak <= 2
    assert [p.name for p in paths] == [f"{i:04d}.mp4" for i in range(6)]


def test_sample_frames_skips_retrieval_of_unselected_indices(monkeypatch):
    total = 96
    class Capture:
        pos = -1
        retrieved = []
        def isOpened(self): return True
        def get(self, key): return total
        def grab(self):
            self.pos += 1
            return self.pos < total
        def retrieve(self):
            self.retrieved.append(self.pos)
            return True, np.full((2, 2, 3), self.pos, np.uint8)
        def read(self): raise AssertionError("unselected frame was materialized")
        def release(self): pass

    cap = Capture()
    monkeypatch.setattr(fingerprint.cv2, "VideoCapture", lambda _: cap)
    frames = fingerprint.sample_frames("test.mp4", n=12)
    selected = fingerprint._pick_indices(total, 12)
    assert cap.retrieved == selected
    assert [int(f[0, 0, 0]) for f in frames] == selected
    assert fingerprint._pick_indices(96, 1) == [0]


def test_subtitle_reuses_full_probe_if_available(tmp_path):
    from pycut.core import montage_clips
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake clip")
    model = SimpleNamespace(enabled=True, ask_vision=lambda frames, prompt, deadline=None: {
        "frames": [{"frame": i, "has_subtitle": False} for i in range(len(frames))]
    })
    info = montage_clips.ClipItem(path, 4, 720, 1280, False)
    frame = np.zeros((16, 16, 3), np.uint8)
    with mock.patch.object(montage_clips, "_probe_one", return_value=info) as probed, \
         mock.patch.object(subtitle, "probe_scalar", side_effect=AssertionError("redundant probe")), \
         mock.patch.object(subtitle, "_grab_frame", return_value=True), \
         mock.patch.object(subtitle.cv2, "imread", return_value=frame):
        assert subtitle.detect_region(model, path, samples=3) is None
    probed.assert_called_once_with(path)
