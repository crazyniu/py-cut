"""Recognition uncertainty continues the batch without claiming verified removal."""
import io
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from pycut import cli, config, tui, tui_runner
from pycut.commands import cover_cmd, slice_cmd
from pycut.core import llm, progress, subtitle
from pycut.core.runtime import STOP
from pycut.core.slice_pipeline import VideoResult


def test_review_can_continue_to_dedup_but_real_failure_stops():
    manifest = SimpleNamespace(csv_path='manifest')
    model = SimpleNamespace(calls=0, failures=0, switched_to=None)
    with mock.patch.object(STOP, 'flag', False), mock.patch.object(slice_cmd, 'log'):
        assert slice_cmd._summary([VideoResult('a', segs=1, sub_review=1)], model, manifest, 0, 0, time.time()) == 0
        assert slice_cmd._summary([VideoResult('a', segs=1, sub_fail=1)], model, manifest, 0, 0, time.time()) == 2
    screen = mock.Mock()
    screen.getch.return_value = 10
    with mock.patch.object(tui_runner, '_run_one', side_effect=[(0, False), (0, False)]) as run, mock.patch.object(tui_runner, '_draw'):
        assert tui_runner.run_jobs(screen, [['slice'], ['dedup']]) == (0, False)
        assert run.call_count == 2


def test_cover_review_publishes_valid_clip_without_marker_and_preserves_old_output(tmp_path):
    source = tmp_path/'src'
    source.mkdir()
    (source/'a.mp4').write_bytes(b'valid video')
    (source/'b.mp4').write_bytes(b'valid video')
    output = tmp_path/'out'
    model = SimpleNamespace(calls=0, failures=0, switched_to=None)
    args = cli.build_parser().parse_args(['cover', '--src-dir', str(source), '--out-dir', str(output), '--workers', '1', '--force'])
    def process(_, clip, **kwargs):
        if clip.stem.startswith('a'):
            return 'review'
        subtitle._marker(clip).write_text('{"version":2,"subtitle":false}')
        return 'nosub'
    with mock.patch.object(cover_cmd, 'self_check'), mock.patch.object(cover_cmd, 'make_llm', return_value=model), mock.patch.object(cover_cmd, 'process_clip', side_effect=process), mock.patch.object(cover_cmd, 'install_signal_handlers'):
        assert cover_cmd.run(args) == 0
        assert (output/'a_1.mp4').read_bytes() == b'valid video'
        assert not subtitle.verified_clip(output/'a_1.mp4')
        assert subtitle.verified_clip(output/'b_1.mp4')
        (output/'a_1.mp4').write_bytes(b'old successful video')
        assert cover_cmd.run(args) == 0
        assert (output/'a_1.mp4').read_bytes() == b'old successful video'


def test_actual_http_timeout_uses_shared_deadline():
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            time.sleep(.3)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b'{"choices":[]}')
            except BrokenPipeError:
                pass
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = llm.LLMClient(f'http://127.0.0.1:{server.server_port}/v1', 'test', 'model')
    try:
        started = time.monotonic()
        with mock.patch.object(STOP, 'flag', False):
            assert client.ask_vision(np.zeros((8,8,3), np.uint8), 'subtitle', deadline=started+.1) is None
        assert time.monotonic()-started < .5
        assert client.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_detection_budget_is_shared_across_samples(tmp_path):
    values = {'format=duration':'4', 'stream=width':'720', 'stream=height':'1280'}
    deadlines = []
    def ask(frame, prompt, *, deadline):
        deadlines.append(deadline)
        return {'frames':[{'frame':i,'has_subtitle':False} for i in range(len(frame))]}
    model = SimpleNamespace(enabled=True, ask_vision=ask)
    with mock.patch.object(subtitle, 'probe_scalar', side_effect=lambda _, key:values[key]), mock.patch.object(subtitle, '_grab_frame', return_value=True), mock.patch.object(subtitle.cv2, 'imread', return_value=object()):
        assert subtitle.process_clip(model, tmp_path/'clip.mp4') == 'nosub'
    assert len(deadlines) == 1
    assert len(set(deadlines)) == 1


def test_heartbeat_during_pending_worker_and_review_display():
    ticks = []
    gate = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(gate.wait, 3)
        def tick():
            ticks.append(True)
            gate.set()
        assert list(progress.completed_futures([task], tick)) == [task]
    assert ticks
    calls = []
    screen = mock.Mock()
    screen.getmaxyx.return_value=(24,120)
    event = dict(stage='字幕擦除', completed=2, total=2, success=1, failed=0, review=1, detail='待复查 1')
    with mock.patch.object(tui, '_safe_add', side_effect=lambda *a: calls.append(a)), mock.patch.object(tui, '_frame', return_value=9), mock.patch.object(tui, '_footer'):
        tui_runner._draw(screen, ['slice'], 1,1, deque([progress.PREFIX+json.dumps(event), '[待复查] 字幕残留']),0,'success')
    assert any('处理完成 · 有待复查项' in c[3] and c[5]=='warn' for c in calls)
    assert any('待复查 1' in c[3] and c[5]=='warn' for c in calls)
    assert not any(c[5]=='bad' for c in calls)


def test_cancel_during_retry_exits_without_next_http_attempt():
    client = llm.LLMClient('http://example.invalid/v1','test','model')
    def stop_sleep(_): STOP.flag = True
    with mock.patch.object(STOP, 'flag', False), mock.patch.object(client, '_post', return_value=(-1,'timeout')) as post, mock.patch.object(llm.time, 'sleep', side_effect=stop_sleep):
        assert client.ask_vision(np.zeros((8,8,3),np.uint8),'subtitle',deadline=time.monotonic()+30) is None
        assert post.call_count == 1


def test_dripping_http_body_cannot_extend_recognition_budget_forever():
    class Drip(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    time.sleep(.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = llm.LLMClient(f'http://127.0.0.1:{server.server_port}/v1', 'test', 'model')
    try:
        start = time.monotonic()
        with mock.patch.object(STOP, 'flag', False):
            assert client.ask_vision(np.zeros((8,8,3),np.uint8),'subtitle',deadline=start+.1) is None
        assert time.monotonic()-start < .4
        assert client.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
