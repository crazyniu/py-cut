"""DEV-462 remaining media regressions; synthetic inputs, no local/remote model."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import pytest

from pycut import config
from pycut.core import frame_sampling, montage_clips as clips
from pycut.core import montage_intro, montage_pip as pip, montage_source, subtitle
from pycut.core.runtime import STOP
from pycut.core.utils import run_cmd


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MONTAGE_TMP", tmp_path / "cache")
    monkeypatch.setattr(STOP, "flag", False)


def encode(*args):
    rc, _, error = run_cmd([config.FFMPEG, "-v", "error", "-y", *map(str, args)], 90)
    assert rc == 0, error


def decode(path):
    capture = cv2.VideoCapture(str(path))
    frames = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    assert frames
    return frames


@pytest.fixture
def media(tmp_path):
    if not shutil.which(config.FFMPEG) or not shutil.which(config.FFPROBE):
        pytest.skip("requires FFmpeg and FFprobe")
    video = tmp_path / "source.mp4"
    encode("-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=5",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", video)
    return video


def test_fully_available_accelerated_segments_do_not_lose_milliseconds(tmp_path):
    item = clips.ClipItem(tmp_path / "source.mp4", 10, 180, 320, False)
    rows = [dict(item=item, offset=.2, take=2.5, speed=1.2345) for _ in range(60)]
    commands = []
    def execute(cmd, timeout):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"video" * 300)
        return 0, "", ""
    with mock.patch.object(clips, "run_cmd", side_effect=execute):
        clips._normalize_segments(rows, (180, 320), 30, "20")
    assert sum(r["take"] for r in rows) == 150
    assert sum(int(c[c.index("-frames:v") + 1]) for c in commands) == 4500
    assert all("tpad=" not in " ".join(c) for c in commands)


def test_real_shortage_is_trimmed_not_misclassified(tmp_path):
    item = clips.ClipItem(tmp_path / "source.mp4", 1.05, 180, 320, False)
    rows = [dict(item=item, offset=.2, take=1, speed=1.2)]
    def execute(cmd, timeout):
        Path(cmd[-1]).write_bytes(b"video" * 300)
        return 0, "", ""
    with mock.patch.object(clips, "run_cmd", side_effect=execute):
        clips._normalize_segments(rows, (180, 320), 30, "20")
    assert rows[0]["take"] == pytest.approx(.85 / 1.2)


def test_unused_material_fills_short_slots_without_repeating_paths(tmp_path):
    pool = [clips.ClipItem(tmp_path / f"{i}.mp4", 2, 180, 320, False) for i in range(5)]
    rows = clips.pick_clips(pool, [(0, 7)], seed=42, min_dur=1.2, max_dur=12)
    real = [r for r in rows if r["item"] is not None]
    assert sum(r["take"] for r in real) == 7
    assert len(real) == len({r["item"].path for r in real}) == 4
    assert not any(r["reused"] for r in real)
    assert all(r["shortfall"] == 0 for r in rows)
    insufficient = clips.pick_clips(pool[:2], [(0, 7)], seed=42, min_dur=1.2, max_dur=12)
    assert insufficient[0]["shortfall"] == 3


def test_reference_uses_one_probe_and_video_not_audio_duration(media):
    with mock.patch.object(clips, "run_cmd", wraps=run_cmd) as probes, \
         mock.patch.object(montage_source, "run_cmd", wraps=run_cmd) as analysis:
        profile = montage_source.probe_source(media)
    assert profile.duration == 5  # audio lasts six seconds
    assert (profile.width, profile.height, profile.fps, profile.has_audio) == (180, 320, 30, True)
    assert probes.call_count == 1 and analysis.call_count == 1
    assert "silencedetect=" in " ".join(analysis.call_args.args[0])


def test_reference_does_not_fall_back_to_container_duration(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    def probe(_path, *, details):
        details.update(duration="N/A", avg_frame_rate="30/1")
        return clips.ClipItem(source, 120, 720, 1280, True)
    with mock.patch.object(montage_source, "_probe_one", side_effect=probe), \
         mock.patch.object(montage_source, "detect_speech_segments") as speech:
        assert montage_source.probe_source(source) is None
    speech.assert_not_called()


def test_batch_sampling_uses_one_process_and_identical_seek_images(media, tmp_path):
    times = [.5, 1.5, 2.5, 3.5, 4.5]
    with mock.patch.object(frame_sampling.subprocess, "Popen", wraps=subprocess.Popen) as processes:
        paths = frame_sampling.grab_frames(media, times, tmp_path / "batch", ffmpeg=config.FFMPEG)
    assert processes.call_count == 1
    for i, t in enumerate(times):
        old = tmp_path / f"old{i}.jpg"
        assert subtitle._grab_frame(media, t, old)
        assert np.array_equal(cv2.imread(str(paths[i])), cv2.imread(str(old)))


def test_failed_sampling_never_marks_clean(tmp_path):
    source = tmp_path / "bad.mp4"
    source.write_bytes(b"invalid video")
    client = SimpleNamespace(enabled=True, ask_vision=mock.Mock())
    with mock.patch.object(subtitle, "probe_scalar", side_effect=lambda _, key: {
        "format=duration": "3", "stream=width": "720", "stream=height": "1280"}[key]):
        assert subtitle.process_clip(client, source) == "review"
    assert not subtitle._marker(source).exists()
    client.ask_vision.assert_not_called()


def test_sampling_cancel_never_starts_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(STOP, "flag", True)
    with mock.patch.object(frame_sampling.subprocess, "Popen") as launch:
        with pytest.raises(frame_sampling.FrameExtractionError, match="中断"):
            frame_sampling.grab_frames(tmp_path / "source", [1], tmp_path / "frames")
    launch.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture only")
def test_sampling_timeout_reaps_the_decoder(tmp_path):
    executable = tmp_path / "slow-ffmpeg"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
    executable.chmod(0o755)
    children = []
    real_popen = subprocess.Popen
    def launch(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child
    with mock.patch.object(frame_sampling.subprocess, "Popen", side_effect=launch):
        with pytest.raises(TimeoutError):
            frame_sampling.grab_frames(tmp_path / "source", [1], tmp_path / "frames",
                                       timeout=.1, ffmpeg=str(executable))
    assert children and all(child.poll() is not None for child in children)


def test_cover_keeps_five_detection_and_three_recheck_images(media):
    counts = []
    def ask(frames, prompt, deadline):
        counts.append(len(frames))
        positive = len(counts) == 1
        return {"frames": [dict(frame=i, has_subtitle=positive,
                                 box=[.1, .7, .9, .9] if positive else None)
                           for i in range(len(frames))]}
    client = SimpleNamespace(enabled=True, ask_vision=ask)
    assert subtitle.process_clip(client, media) == "covered"
    assert counts == [5, 3]
    assert subtitle.verified_clip(media)
    info = clips._probe_one(media)
    assert (info.width, info.height, info.duration, info.has_audio) == (180, 320, 5, True)


def test_real_segment_effects_speed_seek_and_frame_budget(media, tmp_path):
    sticker = tmp_path / "sticker.png"
    cv2.imwrite(str(sticker), np.full((64, 64, 3), (0, 255, 255), np.uint8))
    item = clips._probe_one(media)
    rows = [dict(item=item, take=.4, offset=offset, speed=speed, dedup_seed=i,
                 sticker=str(sticker), mirror=bool(i % 2))
            for i, (offset, speed) in enumerate([
                (.2, 1.2345), (1.334, 1.5), (2.999, 1.001),
                (3.1, 1.2), (4.4, 1.1), (4., 1.5)])]
    out = tmp_path / "background.mp4"
    result = clips.concat_clips(rows, out, canvas=(180, 320), fps=30, crf="20", seam_fade=.1)
    assert result["ok"], result["error"]
    assert len(decode(out)) == 72 and result["duration"] == 2.4
    assert all(r["sticker_applied"] for r in rows)
    metadata = montage_intro._video_signature(out)
    assert metadata["sample_aspect_ratio"] == "1:1"


def test_accelerated_output_seek_starts_at_selected_source_frame(tmp_path):
    if not shutil.which(config.FFMPEG): pytest.skip("requires FFmpeg")
    source = tmp_path / "ramp.mp4"
    # Wide binary stripes identify frames through lossy encoding. A one-level
    # grey ramp confuses quantization with an actual one/two-frame seek error.
    frames = []
    for number in range(150):
        frame = np.empty((64, 64, 3), np.uint8)
        for bit in range(8):
            frame[:, bit * 8:(bit + 1) * 8] = 220 if number & (1 << bit) else 30
        frames.append(frame)
    raw = b"".join(frame.tobytes() for frame in frames)
    command = [config.FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "bgr24",
               "-video_size", "64x64", "-framerate", "30", "-i", "pipe:0", "-c:v", "libx264",
               "-crf", "0", str(source)]
    result = subprocess.run(command, input=raw, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = tmp_path / "cut.mp4"
    item = clips._probe_one(source)
    report = clips.concat_clips([dict(item=item, offset=2.2, take=1., speed=1.5)],
                                 out, canvas=(64, 64), fps=30)
    assert report["ok"], report["error"]
    actual = decode(out)
    numbers = [sum(1 << bit for bit in range(8)
                   if frame[16:48, bit * 8 + 2:(bit + 1) * 8 - 2].mean() > 125)
               for frame in actual]
    assert numbers[0] == 66  # not frame 81 from applying unscaled output seek
    assert numbers[-1] == 110
    assert all(right - left in (1, 2) for left, right in zip(numbers, numbers[1:]))
    assert len(actual) == 30


def test_pip_missing_real_frames_is_rejected_without_tail_clone(media, tmp_path):
    short = tmp_path / "short.mp4"
    encode("-f", "lavfi", "-i", "testsrc2=s=64x114:r=30:d=0.2", "-c:v", "libx264", short)
    out = tmp_path / "out.mp4"
    out.write_bytes(b"previous good output")
    entries = [dict(item=pip.PipItem(short, 5), start=0, duration=5, offset=0)]
    with pytest.raises(RuntimeError, match="真实帧不足"):
        pip.build_pip_cmd(media, entries, out, canvas=(180, 320), fps=30)
    assert out.read_bytes() == b"previous good output"
    assert not pip._tmp_path(out).exists()


def test_pip_32_shots_preserve_real_short_tail(tmp_path):
    if not shutil.which(config.FFMPEG): pytest.skip("requires FFmpeg")
    total = 2318 / 30
    base = tmp_path / "base.mp4"
    encode("-f", "lavfi", "-i", "color=black:s=160x284:r=30", "-frames:v", 2318,
           "-c:v", "libx264", base)
    red, blue = tmp_path / "red.mp4", tmp_path / "blue.mp4"
    for path, color in ((red, "red"), (blue, "blue")):
        encode("-f", "lavfi", "-i", f"color={color}:s=64x114:r=30", "-frames:v", 75,
               "-c:v", "libx264", path)
    items = [pip.PipItem(tmp_path / f"{i}.mp4", 2.2 if i == 0 else 2.5) for i in range(32)]
    entries = pip.plan_pip_timeline(items, total, seed=1, continuous=True)
    for item in items:
        shutil.copyfile(blue if item is entries[-1]["item"] else red, item.path)
    out = tmp_path / "out.mp4"
    command = pip.build_pip_cmd(base, entries, out, canvas=(160, 284), fps=30)
    frames = decode(out)
    assert "tpad=" not in " ".join(command)
    assert len(frames) == 2318
    assert int(frames[-3][60, 124, 2]) > 180
    assert all(int(f[60, 124, 0]) > 180 and int(f[60, 124, 2]) < 40 for f in frames[-2:])


@pytest.mark.parametrize("audio", [True, False])
@pytest.mark.parametrize("timescale", [15360, 90000])
def test_intro_longer_than_body_preserves_all_body_frames(media, tmp_path, audio, timescale):
    folder = tmp_path / "prefixes"
    folder.mkdir()
    source = folder / "front.mp4"
    encode("-f", "lavfi", "-i", "testsrc2=s=180x320:r=30", "-frames:v", 181,
           "-c:v", "libx264", "-preset", "veryfast", source)
    body = tmp_path / "body.mp4"
    args = ["-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.3",
            "-c:v", "libx264", "-preset", "slow", "-profile:v", "baseline", "-level:v", "3.1",
            "-video_track_timescale", timescale, "-crf", "20", "-c:a", "aac"]
    if not audio: args.append("-an")
    encode(*args, body)
    before = decode(body)
    with mock.patch.object(montage_intro, "run_cmd", wraps=run_cmd) as calls:
        report = montage_intro.prepend(body, folder, seed=23, canvas=(180, 320),
                                        stickers=[], crf="20", keep_audio=audio)
    after = decode(body)
    assert report["duration"] == pytest.approx(181 / 30, abs=1e-6)
    assert len(after) == 241
    assert all(np.array_equal(a, b) for a, b in zip(before, after[-60:]))
    # Other modules encode the prefix. This module may only remux prefix/body.
    commands = [c.args[0] for c in calls.call_args_list if "-c:v" in c.args[0]]
    assert commands and all(c[c.index("-c:v") + 1] == "copy" for c in commands)
    assert montage_intro._video_signature(body)["has_audio"] is audio


def test_intro_mux_failure_preserves_completed_body(media, tmp_path):
    folder = tmp_path / "prefixes"
    folder.mkdir()
    source = folder / "front.mp4"
    shutil.copyfile(media, source)
    original = media.read_bytes()
    def execute(command, timeout):
        if "concat" in command:
            Path(command[-1]).write_bytes(b"partial")
            return 1, "", "injected mux failure"
        return run_cmd(command, timeout)
    with mock.patch.object(montage_intro, "run_cmd", side_effect=execute):
        with pytest.raises(RuntimeError, match="保持不变"):
            montage_intro.prepend(media, folder, seed=23, canvas=(180, 320),
                                   stickers=[], crf="20", keep_audio=True)
    assert media.read_bytes() == original
    assert not list(tmp_path.glob("intro_*"))


def test_unknown_frame_count_keeps_original_sampling_without_unused_retrieval(monkeypatch):
    from pycut.core import fingerprint
    class Capture:
        pos = -1
        retrieved = 0
        released = False
        def isOpened(self): return True
        def get(self, key): return 0
        def grab(self):
            self.pos += 1
            return self.pos < 100
        def retrieve(self):
            self.retrieved += 1
            return True, np.full((2, 2, 3), self.pos, np.uint8)
        def read(self): raise AssertionError("unselected frame copied to Python")
        def release(self): self.released = True
    capture = Capture()
    monkeypatch.setattr(fingerprint.cv2, "VideoCapture", lambda _: capture)
    frames = fingerprint.sample_frames("unknown-count.mp4", n=12)
    assert [int(f[0, 0, 0]) for f in frames] == [0, 8, 16, 24, 32, 40, 56, 64, 72, 80, 88, 96]
    assert capture.retrieved < 100 and capture.released


def test_actual_fingerprint_frames_match_full_decode(media):
    from pycut.core import fingerprint
    all_frames = decode(media)
    selected = fingerprint.sample_frames(media, 12)
    expected = [all_frames[i] for i in fingerprint._pick_indices(len(all_frames), 12)]
    assert all(np.array_equal(a, b) for a, b in zip(selected, expected))
    metrics = fingerprint.compare(media, media, 12)
    assert metrics["phash_dist"] == 0 and metrics["psnr"] == 99 and metrics["samples"] == 12
