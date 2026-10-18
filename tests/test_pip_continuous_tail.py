"""连续画中画的真实末帧必须有画中画，不能按间隔镜头阈值丢弃短尾。"""
from pathlib import Path
import shutil

import cv2
import pytest

from pycut import config
from pycut.commands.montage_pipeline import _make_continuous
from pycut.core import montage_pip
from pycut.core.montage_pip import PipItem
from pycut.core.utils import run_cmd


def pool(root, count=32):
    return [PipItem(root / f'{i}.mp4', 2.2 if i == 0 else 2.5) for i in range(count)]


def test_77_seconds_uses_full_last_shot_in_longer_track(tmp_path):
    target = 2318 / 30
    planned = montage_pip.plan_pip_timeline(pool(tmp_path), target, seed=1, continuous=True)
    assert len(planned) == 32
    assert planned[-1]['duration'] == 2.5
    actual = planned
    assert len(actual) == len(montage_pip._clamp_entries(actual)) == 32
    assert sum(row['duration'] for row in actual) > target
    assert not actual[-1]['reached_end']
    assert len({row['item'].path for row in actual}) == 32


def test_clipping_marks_short_tail_and_does_not_drop_last_frame(tmp_path):
    item = PipItem(tmp_path / 'pip.mp4', 3)
    for target, expected in ((2.4, .4), (2.033333333333, .033)):
        rows = [{'item': item, 'start': 0, 'duration': 2., 'offset': 0},
                {'item': item, 'start': 2., 'duration': 2., 'offset': .25}]
        actual = _make_continuous(rows, target)
        assert len(actual) == 2
        assert actual[-1]['duration'] == expected
        assert actual[-1]['allow_short']
        assert len(montage_pip._clamp_entries(actual)) == 2
    # Rounding noise cannot create an unrenderable 0ms third shot.
    assert len(_make_continuous(rows, 2. + 1e-14)) == 1
    # Gap mode retains its existing short-shot filtering.
    assert montage_pip._clamp_entries([dict(rows[0], duration=.4)]) == []
    clipped = montage_pip._clamp_entries([
        dict(rows[0], duration=.067, offset=2.96, allow_short=True)])
    assert clipped[0]['duration'] == pytest.approx(.04)


def test_truly_insufficient_pool_is_not_extended(tmp_path):
    target = 2318 / 30
    planned = montage_pip.plan_pip_timeline(pool(tmp_path, 31), target,
                                           seed=1, continuous=True)
    actual = planned
    assert planned[-1]['reached_end']
    assert len(actual) == 31
    assert sum(row['duration'] for row in actual) == pytest.approx(77.2)
    assert target - sum(row['duration'] for row in actual) > 1/30 + .01


def encode(*args):
    rc, _, error = run_cmd([config.FFMPEG, '-v', 'error', '-y', *map(str, args)], 60)
    assert rc == 0, error


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires FFmpeg')
def test_real_32_shots_cover_final_two_frames(tmp_path):
    target = 2318 / 30
    base = tmp_path / 'base.mp4'
    red = tmp_path / 'red.mp4'
    blue = tmp_path / 'blue.mp4'
    encode('-f', 'lavfi', '-i', 'color=black:size=160x284:rate=30',
           '-frames:v', '2318', '-c:v', 'libx264', base)
    for path, color in ((red, 'red'), (blue, 'blue')):
        encode('-f', 'lavfi', '-i', f'color={color}:size=64x114:rate=30',
               '-frames:v', '75', '-c:v', 'libx264', path)
    items = pool(tmp_path)
    planned = montage_pip.plan_pip_timeline(items, target, seed=1, continuous=True)
    for item in items:
        shutil.copyfile(blue if item is planned[-1]['item'] else red, item.path)
    actual = planned
    out = tmp_path / 'out.mp4'
    command = montage_pip.build_pip_cmd(base, actual, out, canvas=(160, 284), fps=30)
    assert command[command.index('-frames:v') + 1] == '2318'
    cap = cv2.VideoCapture(str(out))
    count = 0
    tail = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        assert frame.shape[:2] == (284, 160)
        if count >= 2315:
            # Centre of the fixed right-top PIP is opaque, well inside feathering.
            blue_value, _, red_value = frame[60, 124]
            tail.append((int(blue_value), int(red_value)))
        count += 1
    cap.release()
    assert count == 2318
    assert tail[0][1] > 180  # preceding shot is red
    assert all(b > 180 and r < 40 for b, r in tail[-2:])  # true tail is blue
