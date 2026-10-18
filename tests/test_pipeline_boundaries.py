"""Exercise real orchestration boundaries, not a mocked whole pipeline."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import threading

import pytest

from pycut import cli, config
from pycut.commands import cover_cmd, montage_pipeline as pipeline
from pycut.core import dedup, montage_clips, montage_intro, montage_pip
from pycut.core.runtime import STOP


@pytest.fixture(autouse=True)
def restore_stop():
    previous = STOP.flag
    STOP.flag = False
    yield
    STOP.flag = previous


@pytest.fixture
def montage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MONTAGE_TMP', tmp_path / 'temp')
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original source')
    output = tmp_path / 'out'
    output.mkdir()
    final = output / 'source_1.mp4'
    final.write_bytes(b'previous good output')
    profile = SimpleNamespace(duration=10, width=720, height=1280,
                              has_audio=True, segments=[], speech_total=10)
    opts = pipeline.MontageOptions(source=source, out_dir=output, seed=47,
        keep_audio=False, use_pip=False, srt='', online_stickers=False,
        prepared={'source': profile, 'background': [object()], 'stickers': []})
    def background(rows, destination, **kwargs):
        destination.write_bytes(b'background' * 300)
        return {'ok': True}
    def publish(command, stage, destination):
        destination.write_bytes(b'rendered' * 300)
        return True
    monkeypatch.setattr(montage_clips, 'pick_clips', lambda *a, **kw: [{'item': object(), 'take': 10}])
    monkeypatch.setattr(pipeline, '_apply_variation', lambda *a: None)
    monkeypatch.setattr(pipeline, '_publish_stage', publish)
    monkeypatch.setattr(pipeline, '_duration', lambda path: 10)
    return opts, final, profile, background


@pytest.mark.parametrize('failure', ['missing_audio', 'invalid_subtitles', 'pip_shortfall'])
def test_impossible_jobs_fail_before_background_encoding(montage, tmp_path, failure):
    opts, final, profile, background = montage
    if failure == 'missing_audio':
        opts.keep_audio = True
        profile.has_audio = False
    elif failure == 'invalid_subtitles':
        srt = tmp_path / 'invalid.srt'
        srt.write_text('not valid subtitles')
        opts.srt = str(srt)
    else:
        opts.use_pip = True
        opts.prepared['pip'] = [montage_pip.PipItem(tmp_path / 'short.mp4', 2)]
    with mock.patch.object(montage_clips, 'concat_clips', side_effect=background) as encode:
        result = pipeline.run_pipeline(opts)
    assert result.get('error') and not result['final']
    encode.assert_not_called()
    assert final.read_bytes() == b'previous good output'
    assert not list((tmp_path / 'temp').rglob('*.mp4'))


@pytest.mark.parametrize('phase', ['background', 'pip', 'subtitles', 'audio', 'validation', 'intro'])
def test_montage_cancel_at_stage_boundary_never_publishes(montage, tmp_path, monkeypatch, phase):
    opts, final, profile, background = montage
    def stop_if(name):
        if phase == name:
            STOP.flag = True
    def encode(rows, destination, **kwargs):
        result = background(rows, destination, **kwargs)
        stop_if('background')
        return result
    def render(base, timeline, destination, **kwargs):
        destination.write_bytes(b'composed' * 300)
        stop_if('pip')
        return []
    def burn(*args, **kwargs):
        stop_if('subtitles')
        return True
    def audio(*args, **kwargs):
        stop_if('audio')
        return True
    def duration(path):
        if '.working_' in Path(path).name:
            stop_if('validation')
        return 10
    def intro(*args, **kwargs):
        stop_if('intro')
        return None
    opts.keep_audio = phase == 'audio'
    if phase == 'pip':
        opts.use_pip = True
        opts.prepared['pip'] = [montage_pip.PipItem(tmp_path / 'clip.mp4', 10)]
    if phase == 'subtitles':
        srt = tmp_path / 'source.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nTEXT\n')
        opts.srt = str(srt)
    if phase == 'intro':
        opts.intro_dir = tmp_path / 'intro'
    monkeypatch.setattr(montage_clips, 'concat_clips', encode)
    monkeypatch.setattr(montage_pip, 'build_pip_cmd', render)
    monkeypatch.setattr(pipeline, '_burn_subtitles', burn)
    monkeypatch.setattr(pipeline, '_attach_audio', audio)
    monkeypatch.setattr(pipeline, '_duration', duration)
    monkeypatch.setattr(montage_intro, 'prepend', intro)
    result = pipeline.run_pipeline(opts)
    assert not result.get('final') and result.get('error')
    assert final.read_bytes() == b'previous good output'
    assert not list(opts.out_dir.glob('*.working_*.mp4'))
    assert not list(opts.out_dir.glob('*.stage_*.mp4'))
    assert not list((tmp_path / 'temp').rglob('*.mp4'))


def test_same_seed_parallel_jobs_have_distinct_backgrounds(montage):
    opts, _, _, _ = montage
    opts.step = 'b'
    barrier = threading.Barrier(2)
    def encode(rows, destination, **kwargs):
        destination.write_bytes(threading.current_thread().name.encode() * 300)
        barrier.wait(timeout=3)
        return {'ok': True}
    with mock.patch.object(montage_clips, 'concat_clips', side_effect=encode):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: pipeline.run_pipeline(opts), range(2)))
    paths = [Path(result['final']) for result in results]
    try:
        assert paths[0] != paths[1]
        assert all(path.exists() for path in paths)  # step=b must retain output
        assert paths[0].read_bytes() != paths[1].read_bytes()
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


@pytest.mark.parametrize('in_place', [False, True])
@pytest.mark.parametrize('phase', ['encode', 'self_check'])
def test_dedup_cancel_does_not_publish_or_replace_source(tmp_path, monkeypatch, in_place, phase):
    source, output = tmp_path / 'source.mp4', tmp_path / 'out.mp4'
    source.write_bytes(b'original source')
    output.write_bytes(b'previous output')
    destination = source if in_place else output
    info = SimpleNamespace(width=720, height=1280, duration=10, has_audio=False, bitrate=400000)
    def encode(command, timeout):
        Path(command[-1]).write_bytes(b'new video' * 300)
        if phase == 'encode':
            STOP.flag = True
        return 0, '', ''
    def check(item, src, dst, opts):
        item.ok = True
        if phase == 'self_check':
            STOP.flag = True
    monkeypatch.setattr(dedup, 'probe_video', lambda p: info)
    monkeypatch.setattr(dedup, '_video_duration', lambda p: 10)
    monkeypatch.setattr(dedup, '_probe_size', lambda p: (720, 1280))
    monkeypatch.setattr(dedup, 'build_params', lambda *a, **kw: {})
    monkeypatch.setattr(dedup, '_build_cmd', lambda src, dst, *args: (['ffmpeg', str(dst)], []))
    monkeypatch.setattr(dedup, 'run_cmd', encode)
    monkeypatch.setattr(dedup, '_self_check', check)
    result = dedup.process_one(source, destination, dedup.DedupOptions(in_place=in_place, force=True))
    assert not result['ok'] and '中断' in result['error']
    assert source.read_bytes() == b'original source'
    assert output.read_bytes() == b'previous output'
    assert not list(tmp_path.glob('*.dedup_tmp*'))


def test_cover_cancel_after_processing_preserves_source_and_existing_output(tmp_path, monkeypatch):
    source, output = tmp_path / 'source.mp4', tmp_path / 'out'
    source.write_bytes(b'original source')
    output.mkdir()
    destination = output / 'source_1.mp4'
    destination.write_bytes(b'previous output')
    args = cli.build_parser().parse_args(['cover', '--file', str(source), '--out-dir', str(output), '--workers', '1'])
    def process(llm, path, **kwargs):
        Path(path).write_bytes(b'new covered video')
        STOP.flag = True
        return 'covered'
    monkeypatch.setattr(cover_cmd, 'install_signal_handlers', lambda: None)
    monkeypatch.setattr(cover_cmd, 'self_check', lambda **kw: None)
    monkeypatch.setattr(cover_cmd, 'make_llm', lambda *a, **kw: SimpleNamespace(calls=0, failures=0, switched_to=None))
    monkeypatch.setattr(cover_cmd, 'process_clip', process)
    assert cover_cmd.run(args) == 130
    assert source.read_bytes() == b'original source'
    assert destination.read_bytes() == b'previous output'
    assert not list(output.glob('.cover-*'))


def test_cancelled_background_stops_before_next_segment_or_concat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MONTAGE_TMP', tmp_path / 'temp')
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    item = montage_clips.ClipItem(source, 3, 160, 284, False)
    rows = [dict(item=item, offset=0, take=1) for _ in range(3)]
    output = tmp_path / 'out.mp4'
    output.write_bytes(b'previous video')
    calls = []
    def encode(command, timeout):
        calls.append(command)
        Path(command[-1]).write_bytes(b'encoded' * 300)
        STOP.flag = True
        return 0, '', ''
    monkeypatch.setattr(montage_clips, 'run_cmd', encode)
    result = montage_clips.concat_clips(rows, output, canvas=(160, 284))
    assert not result['ok'] and len(calls) == 1
    assert output.read_bytes() == b'previous video'
    assert not list((tmp_path / 'temp').rglob('*.mp4'))


def test_cancelled_pip_stops_before_next_preparation_or_final_encode(tmp_path, monkeypatch):
    output = tmp_path / 'out.mp4'
    output.write_bytes(b'previous video')
    monkeypatch.setattr(montage_pip, 'probe_video', lambda p: SimpleNamespace(width=160, height=284, duration=3))
    timeline = [dict(item=montage_pip.PipItem(tmp_path / 'clip.mp4', 2),
                     start=i * 2, duration=2, offset=0) for i in range(2)]
    calls = []
    def encode(command, timeout):
        calls.append(command)
        Path(command[-1]).write_bytes(b'encoded' * 300)
        STOP.flag = True
        return 0, '', 'pycut.complete=1\n'
    monkeypatch.setattr(montage_pip, 'run_cmd', encode)
    with pytest.raises(InterruptedError):
        montage_pip.build_pip_cmd(tmp_path / 'base.mp4', timeline, output)
    assert len(calls) == 1
    assert not Path(calls[0][-1]).parent.exists()
    assert output.read_bytes() == b'previous video'
