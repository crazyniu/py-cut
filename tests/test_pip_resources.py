"""Many-input PiP uses explicit thread budgets and keeps useful FFmpeg errors."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import os
import shutil

import cv2
import pytest
from pycut import config
from pycut.core import montage_pip as pip
from pycut.core.utils import run_cmd


def timeline(path, total=6):
    slot=total/60
    return [dict(item=pip.PipItem(path,3),start=i*slot,duration=slot,
                 offset=0,allow_short=True) for i in range(60)]


def test_all_decoders_and_filter_encoder_threads_are_bounded(tmp_path):
    out=tmp_path/'out.mp4'
    def encode(command, timeout):
        Path(command[-1]).write_bytes(b'video'*500)
        return 0,'',''
    with mock.patch.object(pip,'probe_video',return_value=SimpleNamespace(width=720,height=1280,duration=6)),mock.patch.object(pip,'run_cmd',side_effect=encode):
        command=pip.build_pip_cmd(tmp_path/'base.mp4',timeline(tmp_path/'clip.mp4'),out)
    inputs=[i for i,arg in enumerate(command) if arg=='-i']
    assert len(inputs)==61
    assert all(command[i-2:i]==['-threads','1'] for i in inputs)
    assert command[command.index('-filter_complex_threads')+1]=='1'
    assert command[command.index('libx264')+1:command.index('libx264')+3]==['-threads',str(pip.PIP_ENCODER_THREADS)]
    graph=command[command.index('-filter_complex')+1]
    assert graph.count('overlay=')==1
    assert 'concat=n=60:v=1:a=0' in graph
    assert all(name not in graph for name in ('geq=', 'boxblur=', 'colorchannelmixer=', 'format=rgba', 'noise=', 'eq='))
    assert command.count('libx264')==1  # still one final encode
    assert command[command.index('-frames:v')+1]=='180'


def test_full_failure_cause_is_not_replaced_by_muxer_summary(tmp_path):
    target=tmp_path/'old.mp4'
    target.write_bytes(b'previous valid output')
    error='[decoder] Cannot allocate memory\n[filter] Resource temporarily unavailable\n[out] Nothing was written into output file'
    def fail(command,timeout):
        Path(command[-1]).write_bytes(b'partial output')
        return 187,'',error
    with mock.patch.object(pip,'probe_video',return_value=SimpleNamespace(width=720,height=1280,duration=6)),mock.patch.object(pip,'run_cmd',side_effect=fail):
        with pytest.raises(RuntimeError) as failure:
            pip.build_pip_cmd(tmp_path/'base.mp4',timeline(tmp_path/'clip.mp4'),target)
    assert 'rc=187' in str(failure.value)
    assert error in str(failure.value)
    assert target.read_bytes()==b'previous valid output'
    assert not pip._tmp_path(target).exists()
    many='\n'.join('failure line '+str(i) for i in range(100))
    excerpt=pip._ffmpeg_error(many)
    assert 'failure line 0' in excerpt and 'failure line 99' in excerpt
    assert len(excerpt.splitlines())<=34


@pytest.mark.skipif(os.environ.get('PYCUT_TEST_LONG_PIP')!='1' or not shutil.which('ffmpeg'),reason='opt-in long media QA')
def test_real_60_hd_inputs_147_seconds_and_short_tail(tmp_path):
    frames=4433
    total=frames/30
    def encode(*args):
        rc,_,err=run_cmd([config.FFMPEG,'-v','error','-y',*map(str,args)],90)
        assert rc==0,err
    base=tmp_path/'base.mp4'
    red=tmp_path/'red.mp4'; blue=tmp_path/'blue.mp4'
    encode('-f','lavfi','-i','color=black:s=160x284:r=30','-frames:v',frames,'-c:v','libx264',base)
    for path,color in ((red,'red'),(blue,'blue')):
        encode('-f','lavfi','-i',f'color={color}:s=720x1280:r=30','-frames:v',75,'-c:v','libx264',path)
    entries=[dict(item=pip.PipItem(blue if i==59 else red,2.5),start=i*2.5,
                  duration=min(2.5,total-i*2.5),offset=0,allow_short=True) for i in range(60)]
    for iteration in range(2):
        out=tmp_path/f'result{iteration}.mp4'
        pip.build_pip_cmd(base,entries,out,canvas=(160,284),fps=30,seed=459)
        encode('-i',out,'-f','null','-')
        cap=cv2.VideoCapture(str(out)); tail=[]; count=0
        try:
            while True:
                ok,frame=cap.read()
                if not ok: break
                assert frame.shape[:2]==(284,160)
                if count in (5,4431,4432):
                    tail.append(frame.copy())
                count+=1
        finally: cap.release()
        assert count==frames
        assert int(tail[0][60,124,2])>180
        assert all(int(frame[60,124,0])>180 for frame in tail[1:])
        cv2.imwrite(str(tmp_path/f'first{iteration}.png'),tail[0])
        cv2.imwrite(str(tmp_path/f'last{iteration}.png'),tail[-1])
        print(f'60 HD inputs, {total:.6f}s, {count} frames, full decode, red first/blue short tail: pass {iteration+1}',flush=True)


def test_gap_mode_preserves_intervals_and_light_mirror_is_seeded(tmp_path):
    out=tmp_path/'out.mp4'
    entries=timeline(tmp_path/'clip.mp4')[:2]
    entries[1]['start']=3
    def encode(command,timeout):
        Path(command[-1]).write_bytes(b'video'*500)
        return 0,'',''
    with mock.patch.object(pip,'probe_video',return_value=SimpleNamespace(width=720,height=1280,duration=6)),mock.patch.object(pip,'run_cmd',side_effect=encode):
        cmd=pip.build_pip_cmd(tmp_path/'base.mp4',entries,out,seed=1)
        same=pip.build_pip_cmd(tmp_path/'base.mp4',entries,out,seed=1)
    graph=cmd[cmd.index('-filter_complex')+1]
    assert graph==same[same.index('-filter_complex')+1]
    assert graph.count('overlay=')==2 and 'concat=' not in graph
    assert 'hflip' in graph and 'between(t,3.000' in graph
