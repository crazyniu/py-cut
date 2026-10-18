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
        return 0,'','pycut.complete=1\n'
    with mock.patch.object(pip,'probe_video',return_value=SimpleNamespace(width=720,height=1280,duration=6)),mock.patch.object(pip,'run_cmd',side_effect=encode):
        command=pip.build_pip_cmd(tmp_path/'base.mp4',timeline(tmp_path/'clip.mp4'),out)
    inputs=[i for i,arg in enumerate(command) if arg=='-i']
    assert len(inputs)==2  # base + one lazily-read lossless small-window track
    assert all(command[i-2:i]==['-threads','1'] for i in inputs)
    assert command[command.index('-filter_complex_threads')+1]=='1'
    assert command[command.index('libx264')+1:command.index('libx264')+3]==['-threads',str(pip.PIP_ENCODER_THREADS)]
    graph=command[command.index('-filter_complex')+1]
    # Exactly one full-canvas overlay; the other is a small padded PiP/shadow overlay.
    assert graph.count('overlay=') == 2
    assert graph.count('[bg][styledmerged]overlay=') == 1
    assert 'concat' in command and '[1:v]setpts=' in graph
    assert 'nullsink' not in graph  # inline marker must not consume PiP ahead of overlay
    # Static mask/shadow generation happens *after* concatenating the 60 shots.
    assert graph.count('geq=lum=') == 1
    assert graph.count('boxblur=') == 1
    assert graph.count('drawbox=') == 1
    assert graph.count('loop=loop=179:size=1') == 2
    assert all(name not in graph for name in ('tpad=', 'noise=', 'colorchannelmixer='))
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
    # Two intervals: each gets one small compositing overlay and one main overlay.
    assert graph.count('overlay=') == 4 and 'concat=' not in graph
    assert graph.count('boxblur=') == 2
    assert graph.count('geq=lum=') == 2
    assert 'hflip' in graph and 'between(t,3.000' in graph

# Regression DEV-467: PiP standard geometry/pixel variation was removed in PR #115.
# The effects must be applied to each *small* PiP stream without reinstating
# sixty full-frame overlay/shadow stages.
@pytest.mark.parametrize("continuous", [True, False])
def test_restored_per_segment_standard_variation_is_deterministic(tmp_path, continuous):
    from pycut.core.dedup import montage_variation
    from pycut.core.dedup_presets import derive_seed

    input_file = tmp_path / "source.mp4"
    output_file = tmp_path / "rendered.mp4"
    entries = [
        dict(item=pip.PipItem(input_file, 2.0), start=0.0,
             duration=0.5, offset=0, allow_short=True),
        dict(item=pip.PipItem(input_file, 2.0),
             start=0.5 if continuous else 1.0,
             duration=0.5, offset=0.5, allow_short=True),
    ]
    duration = 1.0 if continuous else 2.0
    info = SimpleNamespace(width=160, height=284, duration=duration)

    commands = []
    def render(command, timeout):
        commands.append(command)
        Path(command[-1]).write_bytes(b"generated" * 500)
        return 0, "", "pycut.complete=1\n"

    def command(seed):
        commands.clear()
        with (mock.patch.object(pip, "probe_video", return_value=info),
              mock.patch.object(pip, "run_cmd", side_effect=render)):
            pip.build_pip_cmd(tmp_path / "base.mp4", entries,
                              output_file, seed=seed)
        return list(commands)

    def filtergraph(commands):
        # Seeded effects are now in the lossless small-window preparation;
        # the final two-input graph contains only styling/overlay/subtitles.
        return ";".join(cmd[cmd.index(flag) + 1] for cmd in commands
                        for flag in ('-vf', '-filter_complex') if flag in cmd)

    first = filtergraph(command(2026))
    assert first == filtergraph(command(2026))
    assert first != filtergraph(command(2027))
    # Static feather uses geq=lum; require an actual dedup pixel effect, not the mask.
    assert any(name in first for name in (",eq=", ",noise=", ",colorchannelmixer="))
    # Continuous mode: only one full-size overlay; static styling adds
    # one small compositing overlay. Gap mode has one pair per visible segment.
    assert first.count("overlay=") == (2 if continuous else 4)
    assert first.count("geq=lum=") == (1 if continuous else 2)
    assert first.count("boxblur=") == (1 if continuous else 2)
    assert "tpad=" not in first
    small_w = pip._even(info.width * config.MONTAGE_PIP_SCALE)
    small_h = pip._even(small_w * 16 / 9)
    for i in range(len(entries)):
        _params, variation = montage_variation(
            small_w, small_h, derive_seed(2026, "pip", i))
        assert variation
        assert variation in first
    # The old public API with no seed still doesn't inject extra perturbation.
    plain = filtergraph(command(None))
    assert all(name not in plain for name in
               (",eq=", ",noise=", ",colorchannelmixer=", ",unsharp="))


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="real PiP visual QA needs FFmpeg",
)
def test_restored_pip_variation_visible_in_decoded_short_video(tmp_path):
    """Run actual build_pip_cmd and decode, rather than trusting filter text."""
    import subprocess
    import numpy as np

    ffmpeg = shutil.which("ffmpeg")
    base = tmp_path / "base.mp4"
    pip_src = tmp_path / "pip.mp4"

    def make_video(path, filter_str):
        process = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", filter_str, "-an",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, timeout=45,
        )
        assert process.returncode == 0, process.stderr.decode("utf-8", "replace")

    make_video(base, "color=c=gray:s=160x284:r=30:d=2")
    make_video(pip_src, "testsrc2=s=96x170:r=30:d=2")
    entries = [
        dict(item=pip.PipItem(pip_src, 2), start=0, duration=1, offset=0,
             allow_short=True),
        dict(item=pip.PipItem(pip_src, 2), start=1, duration=1, offset=0.4,
             allow_short=True),
    ]
    controls = tmp_path / "control.mp4"
    changed = tmp_path / "changed.mp4"
    pip.build_pip_cmd(base, entries, controls, canvas=(160, 284), fps=30, seed=None)
    pip.build_pip_cmd(base, entries, changed, canvas=(160, 284), fps=30, seed=2026)

    def read_frames(path):
        cap = cv2.VideoCapture(str(path))
        samples = {}
        frames = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                assert frame.shape[:2] == (284, 160)
                if frames in (8, 35, 59):
                    samples[frames] = frame.copy()
                frames += 1
        finally:
            cap.release()
        assert frames == 60, f"{path}: expected 60 real frames, got {frames}"
        return samples

    original, affected = read_frames(controls), read_frames(changed)
    for frame_num in original:
        # Standard variation must change real decoded PiP pixels, not only a
        # random-plan flag or an FFmpeg command line. Check both PiP segments.
        control = original[frame_num][25:85, 99:146]
        restored = affected[frame_num][25:85, 99:146]
        assert np.count_nonzero(cv2.absdiff(control, restored)) > 0


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="real feather and shadow QA needs FFmpeg",
)
@pytest.mark.parametrize("continuous", [True, False])
def test_feathered_pip_has_real_soft_edges_and_shadow(tmp_path, continuous):
    """Encode and fully decode the real compositor; check corners, halo and gaps."""
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    background = tmp_path / "background.mp4"
    shot = tmp_path / "small.mp4"
    final = tmp_path / "composed.mp4"

    def make(path, color):
        run = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"color=c={color}:s=160x284:r=30:d=2",
             "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, timeout=45,
        )
        assert run.returncode == 0, run.stderr.decode("utf-8", "replace")

    make(background, "gray")
    make(shot, "red")
    if continuous:
        entries = [
            dict(item=pip.PipItem(shot, 2), start=0, duration=1, offset=0, allow_short=True),
            dict(item=pip.PipItem(shot, 2), start=1, duration=1, offset=0.4, allow_short=True),
        ]
    else:
        entries = [
            dict(item=pip.PipItem(shot, 2), start=0.3, duration=0.6, offset=0, allow_short=True),
            dict(item=pip.PipItem(shot, 2), start=1.2, duration=0.6, offset=0.4, allow_short=True),
        ]

    command = pip.build_pip_cmd(background, entries, final,
                                canvas=(160, 284), fps=30, seed=None)
    graph = command[command.index("-filter_complex") + 1]
    assert graph.count("boxblur=") == (1 if continuous else 2)
    if continuous:
        assert graph.count("[bg][styledmerged]overlay=") == 1
    else:
        assert "[bg][styledseg0]overlay=" in graph
        assert "[v0][styledseg1]overlay=" in graph
    video = cv2.VideoCapture(str(final))
    frames = []
    try:
        while True:
            ok, frame = video.read()
            if not ok:
                break
            assert frame.shape[:2] == (284, 160)
            frames.append(frame)
    finally:
        video.release()
    assert len(frames) == 60
    # 160 wide / pip48 wide / 12px right margin -> original PiP starts at x=100.
    # Pixel x=99 is just outside the PiP and should receive the soft shadow.
    active = frames[15]
    bg = active[65, 85].astype(float).mean()
    halo = active[65, 99].astype(float).mean()
    assert bg - halo >= 2, (bg, halo)
    # Center of PiP is red; feathered leftmost edge is visibly softer.
    center = active[65, 124]
    edge = active[65, 100]
    assert int(center[2]) > int(center[1]) + 45
    assert int(edge[2]) < int(center[2]) - 20
    if not continuous:
        # Gap is truly empty: no lingering PiP or shadow outside enable windows.
        gap = frames[32]
        assert abs(gap[65, 124].astype(float).mean() -
                   gap[65, 85].astype(float).mean()) <= 3
