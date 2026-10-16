"""Progress is real stage work and failures never become verified successes."""
import io
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import tempfile

from pycut import tui, tui_runner
from pycut.core import progress, subtitle


def test_progress_payload_round_trip_and_invalid_fields():
    with mock.patch.dict(progress.os.environ, {'PYCUT_PROGRESS': '1'}), mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
        progress.progress('字幕擦除', 4, 10, success=3, failed=1)
    event = progress.parse(stdout.getvalue().strip())
    assert event['completed'] == 4 and event['failed'] == 1
    assert progress.parse(progress.PREFIX + '{"stage":"x","total":null}') is None
    assert progress.parse('normal log') is None


def test_dashboard_prioritizes_progress_results_and_hides_command():
    class Screen:
        def __init__(self, size): self.size, self.lines = size, []
        def getmaxyx(self): return self.size
        def erase(self): pass
        def refresh(self): pass
        def addstr(self, y, x, text, attr):
            assert y < self.size[0] and x + tui._disp_width(text) < self.size[1]
            self.lines.append(text)
    event = dict(stage='去重', completed=8, total=10, success=7, failed=1, detail='pHash 4.2 · PSNR 21.0 dB', output='/data/去重')
    for size in ((24, 100), (14, 60)):
        screen = Screen(size)
        with mock.patch.object(tui, '_attr', return_value=0):
            tui_runner._draw(screen, ['dedup', '--src-dir', '/long/input'], 2, 2, deque(['log']), 0, 'running', {'去重': event})
        text = '\n'.join(screen.lines)
        assert '80%' in text and '8/10' in text and '失败 1' in text
        assert 'pHash' in text and '/data/去重' in text
        assert '--src-dir' not in text and '/long/input' not in text


def test_moving_subtitle_boxes_are_unioned():
    model = SimpleNamespace(ask_vision=mock.Mock(return_value={"frames":[
        {'frame':0,'has_subtitle': True, 'box': [.2, .2, .5, .3]},
        {'frame':1,'has_subtitle': True, 'box': [.1, .8, .8, .9]},
        {'frame':2,'has_subtitle': True, 'box': [.2, .2, .5, .3]}]}))
    values = {'format=duration':'4', 'stream=width':'720', 'stream=height':'1280'}
    with mock.patch.object(subtitle, 'probe_scalar', side_effect=lambda _, key: values[key]), mock.patch.object(subtitle, 'grab_frames', side_effect=lambda clip,times,folder,**kw:[folder/f'f{i}.jpg' for i in range(len(times))]), mock.patch.object(subtitle.cv2, 'imread', return_value=object()):
        x, y, width, height, _ = subtitle.detect_region(model, Path('clip.mp4'), 3)
    assert x <= 72 and x + width >= 576
    assert y <= 256 and y + height >= 1152


def test_residual_subtitle_or_failed_verification_does_not_mark_complete():
    region = (0, 100, 300, 100, ['字幕'])
    for after in (region, RuntimeError('gateway failed')):
        with tempfile.TemporaryDirectory() as temp:
            clip = Path(temp) / 'clip.mp4'
            clip.write_bytes(b'source')
            subtitle._marker(clip).write_text('{}')
            with mock.patch.object(subtitle, 'detect_region', side_effect=[region, after]), mock.patch.object(subtitle, 'cover_clip', return_value=True):
                assert subtitle.process_clip(SimpleNamespace(enabled=True), clip) == 'review'
            assert not subtitle._marker(clip).exists()


def test_verified_erase_can_resume_but_legacy_marker_cannot():
    with tempfile.TemporaryDirectory() as temp:
        clip = Path(temp) / 'clip.mp4'
        clip.write_bytes(b'source')
        marker = subtitle._marker(clip)
        marker.write_text('{"subtitle":true}')
        assert not subtitle.verified_clip(clip)
        with mock.patch.object(subtitle, 'detect_region', side_effect=[(0,100,300,100,['字幕']), None]), mock.patch.object(subtitle, 'cover_clip', return_value=True):
            assert subtitle.process_clip(SimpleNamespace(enabled=True), clip) == 'covered'
        assert json.loads(marker.read_text())['verified']
        with mock.patch.object(subtitle, 'detect_region') as detect:
            assert subtitle.process_clip(SimpleNamespace(enabled=True), clip) == 'skip'
            detect.assert_not_called()


def test_runner_consumes_structured_events_and_retains_final_counts():
    event = dict(stage='字幕擦除', completed=3, total=3, success=2, failed=1)
    child = mock.Mock()
    child.stdout = io.StringIO(progress.PREFIX + json.dumps(event) + '\n')
    child.poll.return_value = 0
    screen = mock.Mock()
    screen.getch.return_value = -1
    history = deque(maxlen=600)
    with mock.patch.object(tui_runner.subprocess, 'Popen', return_value=child) as launch, mock.patch.object(tui_runner, '_draw'):
        assert tui_runner._run_one(screen, ['slice'], 1, 1, history) == (0, False)
    assert launch.call_args.kwargs['env']['PYCUT_PROGRESS'] == '1'
    assert [progress.parse(line) for line in history] == [event]
