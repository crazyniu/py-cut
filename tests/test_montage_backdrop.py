"""Bound backdrop composition and preserve the body on failures/cancellation."""
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess

import pytest

from pycut.core import montage_backdrop as backdrop
from pycut import config


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    body = tmp_path/'body.mp4'
    body.write_bytes(b'original')
    pool = [SimpleNamespace(path=tmp_path/f'{i}.mp4',duration=3) for i in range(3)]
    commands = []
    def encode(command, timeout):
        commands.append(command)
        Path(command[-1]).write_bytes(b'encoded'*300)
        return 0,'',''
    monkeypatch.setattr(backdrop,'check_stop',lambda: None)
    monkeypatch.setattr(backdrop,'run_cmd',encode)
    monkeypatch.setattr(backdrop,'_video_duration',lambda path: 4)
    return body,pool,commands


def test_unused_source_and_finite_frame_budget(fixture):
    body,pool,commands = fixture
    result = backdrop.compose(body,pool,seed=42,duration=4,
                              excluded={pool[0].path,pool[1].path},crf='20')
    assert result['source'] == str(pool[2].path)
    cmd = commands[0]
    assert cmd[cmd.index('-frames:v')+1] == '120'
    assert '-an' in cmd and '-stream_loop' in cmd
    assert 'trim=end_frame=122' in cmd[cmd.index('-filter_complex')+1]
    assert body.read_bytes() != b'original'
    assert not list(body.parent.glob('backdrop_*.mp4'))


@pytest.mark.parametrize('kind',['encode','duration','cancel'])
def test_failure_preserves_body_and_cleans_temporary_file(fixture,monkeypatch,kind):
    body,pool,_ = fixture
    if kind == 'encode':
        monkeypatch.setattr(backdrop,'run_cmd',lambda *a,**k:(1,'','actual encode failure'))
    elif kind == 'duration':
        monkeypatch.setattr(backdrop,'_video_duration',lambda path: 1)
    else:
        def cancel():
            raise InterruptedError('cancelled')
        monkeypatch.setattr(backdrop,'check_stop',cancel)
    with pytest.raises((RuntimeError,InterruptedError)):
        backdrop.compose(body,pool,seed=42,duration=4,excluded=set(),crf='20')
    assert body.read_bytes() == b'original'
    assert not list(body.parent.glob('backdrop_*.mp4'))


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='requires FFmpeg')
def test_real_launcher_backdrop_keeps_the_entire_body_frame_budget(tmp_path):
    clips = tmp_path/'clips'
    clips.mkdir()
    reference = tmp_path/'reference.mp4'
    subprocess.run([config.FFMPEG,'-v','error','-y','-f','lavfi','-i',
        'testsrc2=size=160x284:rate=30:duration=3.2','-c:v','libx264',str(reference)],check=True)
    # Exercise a higher-rate library source without extending the 30 fps body.
    subprocess.run([config.FFMPEG,'-v','error','-y','-f','lavfi','-i',
        'testsrc2=size=160x284:rate=60:duration=3.2','-c:v','libx264',str(clips/'clip.mp4')],check=True)
    invocation=subprocess.run(['./run.sh','montage','--source',str(reference),
        '--clip-dir',str(clips),'--out-dir',str(tmp_path/'out'),
        '--intro-dir','','--no-pip','--no-sub','--no-sticker','--no-audio',
        '--seed','7117926330837781456'],capture_output=True,text=True,timeout=120)
    assert invocation.returncode==0,invocation.stdout+'\n'+invocation.stderr
    output=next((tmp_path/'out').rglob('*.mp4'))
    info=json.loads(subprocess.check_output([config.FFPROBE,'-v','error',
        '-select_streams','v:0','-count_frames','-show_streams','-of','json',str(output)]))['streams'][0]
    assert info['nb_read_frames']=='96'
    assert info['sample_aspect_ratio']=='1:1'
    assert float(info['duration'])==3.2
