"""Montage preview must see the same candidates as the encoder."""
from pathlib import Path
from unittest import mock

from pycut import config, tui
from pycut.core.montage_clips import _collect_files


def test_montage_nested_chinese_libraries_and_specific_empty_message(tmp_path):
    background = tmp_path / '中文 背景'
    pip = tmp_path / '切片'
    reference = tmp_path / '爆款.mp4'
    for root in (background, pip):
        nested = root / '179179' / '有人脸' / '额外' / '深层'
        nested.mkdir(parents=True)
        (nested / '素材_1.MP4').write_bytes(b'candidate')
    selection = ([reference], {str(reference): 1})
    with mock.patch.object(config, 'MONTAGE_CLIP_DIR', background), mock.patch.object(config, 'MONTAGE_DEDUP_DIR', pip), mock.patch.object(tui, 'list_videos_flat', return_value=[reference]), mock.patch.object(tui, '_multi_select', return_value=selection), mock.patch.object(tui, '_confirm', return_value=True), mock.patch.object(tui, '_flash') as flash:
        jobs = tui._wizard_montage(None, 100)
        assert jobs and '--clip-dir' in jobs[0]
        flash.assert_not_called()
        for candidate in _collect_files(pip):
            candidate.unlink()
        (pip / '不支持.mkv').write_bytes(b'candidate')
        assert tui._wizard_montage(None, 100) is None
        warning = flash.call_args.args[1]
        assert f'画中画切片：{pip}' in warning
        assert f'背景切片：{background}' not in warning
        assert 'MP4/MOV/M4V' in warning
        for candidate in _collect_files(background):
            candidate.unlink()
        assert tui._wizard_montage(None, 100) is None
        warning = flash.call_args.args[1]
        assert f'背景切片：{background}' in warning
        assert f'画中画切片：{pip}' in warning
