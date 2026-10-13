"""Optional intro and original reference-audio copying contracts."""
import json
import os
from pathlib import Path
import shutil
from unittest import mock

import pytest

from pycut import cli, config, settings, tui, web_config
from pycut.commands import montage_cmd, montage_pipeline
from pycut.core import montage_intro, scan
from pycut.core.utils import run_cmd


def test_intro_not_required_and_web_labels_it_optional(tmp_path):
    values = {key: str(tmp_path) for key in settings.REQUIRED}
    assert 'PYCUT_INTRO_DIR' not in settings.REQUIRED
    assert settings.problems(values) == []
    assert '可选' in web_config.page().split('for="PYCUT_INTRO_DIR"')[1].split('</label>')[0]


def test_empty_intro_preflight_and_late_scan_continue_but_scan_errors_fail(tmp_path):
    source = tmp_path / 'source.mp4'
    source.touch()
    intro = tmp_path / 'intro'
    intro.mkdir()
    args = cli.build_parser().parse_args(['montage', '--source', str(source),
                                         '--intro-dir', str(intro)])
    result = {'final': 'ok', 'source_duration': 5, 'segments': 2,
              'clips_used': 2, 'final_duration': 5}
    with mock.patch.object(montage_cmd, 'self_check'), \
         mock.patch.object(montage_cmd, 'install_signal_handlers'), \
         mock.patch.object(montage_pipeline, 'run_pipeline', return_value=result) as run:
        assert montage_cmd.run(args) == 0
        assert run.call_args.args[0].intro_dir == intro
        run.reset_mock()
        with mock.patch.object(scan.os, 'walk', side_effect=PermissionError('no access')):
            assert montage_cmd.run(args) == 2
        run.assert_not_called()
    # Directory existed at preflight; files are removed before the actual prepend.
    candidate = intro / 'prefix.mp4'
    candidate.touch()
    assert scan.list_videos_recursive(intro, raise_on_error=True)
    candidate.unlink()
    old = source.read_bytes()
    assert montage_intro.prepend(source, intro, seed=1, canvas=(180, 320),
        stickers=[], crf='20', keep_audio=False) is None
    assert source.read_bytes() == old
    candidate.write_bytes(b'corrupt')
    with pytest.raises(RuntimeError, match='无法探测'):
        montage_intro.prepend(source, intro, seed=1, canvas=(180, 320),
            stickers=[], crf='20', keep_audio=False)
    assert source.read_bytes() == old


def test_tui_unconfigured_intro_does_not_enable_default_folder(tmp_path):
    source = tmp_path / 'reference.mp4'
    source.touch()
    with mock.patch.object(tui, 'list_videos_flat', return_value=[source]), \
         mock.patch('pycut.core.montage_clips._collect_files', return_value=[source]), \
         mock.patch.object(tui, '_multi_select', return_value=([source], {str(source): 1})), \
         mock.patch.object(tui, '_set_fission_counts', return_value=True), \
         mock.patch.object(tui, '_confirm', return_value=True) as confirm, \
         mock.patch.dict(os.environ, {'PYCUT_INTRO_DIR': ''}):
        jobs = tui._wizard_montage(None, 100)
    assert jobs[0][jobs[0].index('--intro-dir') + 1] == ''
    assert ('前贴', '未配置，跳过') in confirm.call_args.args[1]


def ffmpeg(*args):
    rc, _, err = run_cmd([config.FFMPEG, '-v', 'error', '-y', *map(str, args)], 30)
    assert rc == 0, err


def packet_hashes(path):
    rc, text, err = run_cmd([config.FFPROBE, '-v', 'error', '-select_streams', 'a:0',
        '-show_packets', '-show_data_hash', 'sha256', '-show_entries', 'packet=data_hash',
        '-of', 'json', str(path)], 30)
    assert rc == 0, err
    return [packet['data_hash'] for packet in json.loads(text)['packets']]


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires FFmpeg')
def test_aac_packet_copy_keeps_short_audio_and_full_video(tmp_path):
    video = tmp_path / 'main.mp4'
    audio = tmp_path / 'reference.m4a'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=180x320:rate=30:duration=3',
           '-c:v', 'libx264', video)
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', 'aac', audio)
    original_hashes = packet_hashes(audio)
    assert original_hashes
    assert montage_pipeline._attach_audio(video, audio)
    assert packet_hashes(video) == original_hashes
    assert montage_pipeline._duration(video) == 3
    # True errors remain atomic and do not trigger a transcoding fallback.
    original = video.read_bytes()
    audio.write_bytes(b'bad-audio')
    assert not montage_pipeline._attach_audio(video, audio)
    assert video.read_bytes() == original
    assert not list(tmp_path.glob('*.aud_*'))


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires FFmpeg')
def test_unsupported_codec_only_transcodes_for_container_compatibility(tmp_path):
    video = tmp_path / 'main.mp4'
    audio = tmp_path / 'reference.wav'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=180x320:rate=30:duration=2',
           '-c:v', 'libx264', video)
    ffmpeg('-f', 'lavfi', '-i', 'sine=duration=1', '-c:a', 'adpcm_ima_wav', audio)
    with mock.patch.object(montage_pipeline, 'log') as log:
        assert montage_pipeline._attach_audio(video, audio)
    assert any('音频兼容' in call.args[0] for call in log.call_args_list)
    assert montage_pipeline._duration(video) == 2
