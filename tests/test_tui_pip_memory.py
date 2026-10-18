"""PiP bounded preparation, real-frame progress and atomic failure protection."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import os
import shutil
import subprocess

import pytest

from pycut import config
from pycut.core import montage_pip as pip
from pycut.core import montage_pip_track as track


def entries(path, count=3, frames=90):
    return [dict(item=pip.PipItem(path, 4.0), start=round(i * frames / count) / 30,
                 duration=(round((i + 1) * frames / count) - round(i * frames / count)) / 30,
                 offset=0.1, allow_short=True) for i in range(count)]


def test_bounded_track_reuses_effects_and_only_final_encode_is_full_size(tmp_path):
    from pycut.core.dedup import montage_variation
    from pycut.core.dedup_presets import derive_seed
    commands = []
    manifests = []
    events = []

    def encode(command, timeout):
        commands.append(command)
        if '-f' in command and 'concat' in command:
            manifest = Path(command[command.index('concat') + 6])
            manifests.append(manifest)
            assert manifest.is_file()
            assert 'file clip_00001.nut' in manifest.read_text()
        Path(command[-1]).write_bytes(b'video' * 500)
        return 0, '', 'pycut.complete=1\n'

    info = SimpleNamespace(width=720, height=1280, duration=3.0)
    def record(stage, completed, total, **fields):
        events.append(dict(stage=stage, completed=completed, total=total, **fields))
    with (mock.patch.object(pip, 'probe_video', return_value=info),
          mock.patch.object(pip, 'run_cmd', side_effect=encode),
          mock.patch.object(pip, 'progress', side_effect=record),
          mock.patch.object(track, 'progress', side_effect=record)):
        result = pip.build_pip_cmd(tmp_path / 'base.mp4', entries(tmp_path / "中文 ' source.mp4"),
                                   tmp_path / 'output.mp4', seed=2026)
    assert len(commands) == 4
    for i, command in enumerate(commands[:-1]):
        assert command.count('-i') == 1
        assert 'ffv1' in command and 'libx264' not in command
        assert command[command.index('-fps_mode') + 1] == 'passthrough'
        graph = command[command.index('-vf') + 1]
        _, variation = montage_variation(216, 384, derive_seed(2026, 'pip', i))
        assert variation in graph
        assert "enable='eq(n,29)'" in graph
        assert 'tpad=' not in graph and 'nullsink' not in graph
    assert result == commands[-1]
    assert result.count('-i') == 2
    assert result[result.index('-frames:v') + 1] == '90'
    graph = result[result.index('-filter_complex') + 1]
    assert graph.count('[bg][styledmerged]overlay=') == 1
    assert graph.count('geq=lum=') == 1 and graph.count('boxblur=') == 1
    assert "enable='eq(n,89)'" in graph
    assert 'nullsink' not in graph and '[pipcheck]' not in graph
    assert 'tpad=' not in graph
    assert manifests and not manifests[0].parent.exists()
    measured = [e for e in events if e.get('detail', '').startswith('小窗预处理：已完成')]
    assert [e['completed'] for e in measured] == [30, 60, 90]
    assert all(e['total'] == 90 and not e.get('success') for e in measured)
    assert events[-1].get('success') == 1


@pytest.mark.parametrize('failure', ['encode', 'short', 'interrupt', 'final'])
def test_failure_or_interrupt_keeps_old_output_and_cleans_small_clips(tmp_path, failure):
    output = tmp_path / 'existing.mp4'
    output.write_bytes(b'previous result')
    temporary_dirs = []
    calls = 0
    def encode(command, timeout):
        nonlocal calls
        calls += 1
        target = Path(command[-1])
        target.write_bytes(b'video' * 500)
        if 'ffv1' in command:
            temporary_dirs.append(target.parent)
        if calls == 2:
            if failure == 'interrupt':
                raise KeyboardInterrupt
            if failure == 'encode':
                return 187, '', 'decoder: Cannot allocate memory\nNothing was written'
            if failure == 'short':
                return 0, '', ''
        if failure == 'final' and 'libx264' in command:
            return 187, '', 'filter error: original diagnostic'
        return 0, '', 'pycut.complete=1\n'
    info = SimpleNamespace(width=720, height=1280, duration=3)
    with (mock.patch.object(pip, 'probe_video', return_value=info),
          mock.patch.object(pip, 'run_cmd', side_effect=encode)):
        with pytest.raises(KeyboardInterrupt if failure == 'interrupt' else RuntimeError) as error:
            pip.build_pip_cmd(tmp_path / 'base.mp4', entries(tmp_path / 'clip.mp4'), output)
    if failure == 'encode':
        assert 'Cannot allocate memory' in str(error.value) and 'rc=187' in str(error.value)
        assert '第 2/3 段' in str(error.value)
    elif failure == 'short':
        assert '真实帧不足' in str(error.value)
    elif failure == 'final':
        assert 'original diagnostic' in str(error.value)
    assert output.read_bytes() == b'previous result'
    assert not pip._tmp_path(output).exists()
    assert temporary_dirs and all(not path.exists() for path in temporary_dirs)


def test_single_inline_shot_has_no_eager_last_frame_branch(tmp_path):
    def encode(command, timeout):
        Path(command[-1]).write_bytes(b'video' * 500)
        return 0, '', 'pycut.complete=1\n'
    with (mock.patch.object(pip, 'probe_video', return_value=SimpleNamespace(width=720, height=1280, duration=3)),
          mock.patch.object(pip, 'run_cmd', side_effect=encode)):
        cmd = pip.build_pip_cmd(tmp_path / 'base.mp4', entries(tmp_path / 'clip.mp4', count=1), tmp_path / 'out.mp4')
    graph = cmd[cmd.index('-filter_complex') + 1]
    assert 'concat=n=1' in graph
    assert '[piptrack]metadata=mode=add' in graph
    assert "enable='eq(n,89)'" in graph
    assert 'nullsink' not in graph and '[pipcheck]' not in graph


@pytest.mark.parametrize('count', [2, 16, 21, 32, 33, 61])
def test_every_multishot_track_bounds_original_inputs_without_threshold_override(tmp_path, count):
    commands = []
    def encode(command, timeout):
        commands.append(command)
        Path(command[-1]).write_bytes(b'video' * 500)
        return 0, '', 'pycut.complete=1\n'
    with (mock.patch.object(pip, 'probe_video', return_value=SimpleNamespace(width=720, height=1280, duration=3)),
          mock.patch.object(pip, 'run_cmd', side_effect=encode)):
        result = pip.build_pip_cmd(tmp_path / 'base.mp4',
                                   entries(tmp_path / 'clip.mp4', count=count),
                                   tmp_path / 'out.mp4', seed=3691282568)
    assert len(commands) == count + 1
    assert all(command.count('-i') == 1 and 'ffv1' in command for command in commands[:-1])
    assert result.count('-i') == 2 and 'concat' in result
    assert result[result.index('-frames:v') + 1] == '90'


HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def make_video(path, source, frames):
    result = subprocess.run([config.FFMPEG, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi',
                             '-i', source, '-frames:v', str(frames), '-c:v', 'libx264',
                             '-threads', '1', '-pix_fmt', 'yuv420p', str(path)],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def decoded_frames(path):
    import cv2
    video = cv2.VideoCapture(str(path))
    frames = []
    try:
        while True:
            ok, frame = video.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        video.release()
    return frames


@pytest.mark.skipif(not HAS_FFMPEG, reason='real FFmpeg required')
@pytest.mark.parametrize('count', [3, 21])
def test_real_bounded_and_inline_seeded_outputs_match_and_keep_subtitles(tmp_path, count):
    import numpy as np
    base, clip = tmp_path / 'base.mp4', tmp_path / 'clip.mp4'
    make_video(base, 'color=c=gray:s=160x284:r=30', 90)
    make_video(clip, 'testsrc2=s=96x170:r=30', 120)
    ass = tmp_path / 'caption.ass'
    ass.write_text('''[Script Info]
ScriptType: v4.00+
PlayResX: 160
PlayResY: 284
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,18,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,3,1,0,2,5,5,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,SAMPLE
''', encoding='utf-8')
    expected, actual = tmp_path / 'inline.mp4', tmp_path / 'bounded.mp4'
    # Explicitly force the old path only for the control; production must use
    # bounded preparation even with 21 shots, without a test-only override.
    with mock.patch.object(pip, 'PIP_INLINE_LIMIT', count):
        pip.build_pip_cmd(base, entries(clip, count=count), expected, seed=2026, ass_path=ass)
    pip.build_pip_cmd(base, entries(clip, count=count), actual, seed=2026, ass_path=ass)
    left, right = decoded_frames(expected), decoded_frames(actual)
    assert len(left) == len(right) == 90
    assert all(frame.shape == (284, 160, 3) for frame in right)
    # Lossless transport must not visibly change the existing seeded effects.
    # Compare the PiP region, not a full-frame average dominated by background.
    differences = [np.abs(a[15:115, 95:155].astype(float) - b[15:115, 95:155])
                   for a, b in zip(left, right)]
    assert max(float(delta.mean()) for delta in differences) < 2.0
    assert all(float(frame[240:280, 20:140].std()) > 10 for frame in right)
    # Tail still contains moving PiP, not just the base colour or a frozen pad.
    assert float(right[-1][30:90, 108:138].std()) > 10
    assert not np.array_equal(right[-1][30:90, 108:138], right[-2][30:90, 108:138])


@pytest.mark.skipif(not HAS_FFMPEG, reason='real FFmpeg required')
def test_real_source_shortfall_is_not_filled_with_cloned_tail(tmp_path):
    base, clip = tmp_path / 'base.mp4', tmp_path / 'short.mp4'
    make_video(base, 'color=c=gray:s=160x284:r=30', 90)
    make_video(clip, 'testsrc2=s=96x170:r=30', 5)
    output = tmp_path / 'out.mp4'
    output.write_bytes(b'keep this successful previous output')
    with pytest.raises(RuntimeError, match='真实帧不足'):
        pip.build_pip_cmd(base, entries(clip), output, seed=2026)
    assert output.read_bytes() == b'keep this successful previous output'
    assert not pip._tmp_path(output).exists()


@pytest.mark.skipif(not HAS_FFMPEG, reason='real FFmpeg required')
def test_real_mixed_frame_rates_and_one_frame_tail_keep_all_21_shots(tmp_path):
    base = tmp_path / 'base.mp4'
    make_video(base, 'color=c=gray:s=160x284:r=30', 181)
    sources = []
    for rate in (25, 30, 60):
        source = tmp_path / f'clip-{rate}.mp4'
        make_video(source, f'testsrc2=s=96x170:r={rate}', rate * 2)
        sources.append(source)
    timeline = []
    for i in range(21):
        frames = 9 if i < 20 else 1
        timeline.append(dict(item=pip.PipItem(sources[i % 3], 2),
                             start=i * 9 / 30, duration=frames / 30,
                             offset=.1, allow_short=True))
    output = tmp_path / 'out.mp4'
    pip.build_pip_cmd(base, timeline, output, seed=3691282568)
    frames = decoded_frames(output)
    assert len(frames) == 181
    assert float(frames[-1][30:90, 108:138].std()) > 10


@pytest.mark.skipif(os.environ.get('PYCUT_TEST_LONG_PIP') != '1' or not HAS_FFMPEG,
                    reason='opt-in long media QA')
@pytest.mark.parametrize('count,frame_count', [(21, 1520), (32, 2318), (61, 4433)])
def test_real_hd_multishot_tracks_have_bounded_inputs_and_complete_frames(tmp_path, count, frame_count):
    base, clip = tmp_path / 'base.mp4', tmp_path / 'hd.mp4'
    make_video(base, 'color=c=gray:s=720x1280:r=30', frame_count)
    make_video(clip, 'testsrc2=s=1080x1920:r=30', 120)
    timeline = []
    for i in range(count):
        path = tmp_path / f'source-{i}.mp4'
        shutil.copyfile(clip, path)
        start = round(i * frame_count / count)
        end = round((i + 1) * frame_count / count)
        timeline.append(dict(item=pip.PipItem(path, 4), start=start / 30,
                             duration=(end - start) / 30, offset=.1, allow_short=True))
    output = tmp_path / 'out.mp4'
    command = pip.build_pip_cmd(base, timeline, output, seed=3691282568)
    assert command.count('-i') == 2
    # Stream the full decode: retaining thousands of 720p frames would itself
    # make a memory-bound compositor test consume gigabytes in the test runner.
    import cv2
    video = cv2.VideoCapture(str(output))
    decoded = 0
    try:
        while True:
            ok, frame = video.read()
            if not ok:
                break
            assert frame.shape == (1280, 720, 3)
            if decoded in (0, frame_count // 2, frame_count - 1):
                assert float(frame[160:400, 500:600].std()) > 10
            decoded += 1
    finally:
        video.release()
    assert decoded == frame_count
