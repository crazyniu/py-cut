"""Montage preview must see the same candidates as the encoder."""
from pathlib import Path
from unittest import mock

from pycut import config, tui, settings
from pycut.core.montage_clips import _collect_files


def test_montage_nested_chinese_libraries_and_specific_empty_message(tmp_path):
    background = tmp_path / '中文 背景'
    pip = tmp_path / '切片'
    reference = tmp_path / '话术' / 'a' / '爆款.mp4'
    for root in (background, pip):
        nested = root / '179179' / '有人脸' / '额外' / '深层'
        nested.mkdir(parents=True)
        (nested / '素材_1.MP4').write_bytes(b'candidate')
    selection = ([reference], {str(reference): 1})
    with mock.patch.object(config, 'CLIP_DIRS', [background]), mock.patch.object(config, 'PIP_DIRS', [pip]), mock.patch.object(tui, 'list_videos_recursive', return_value=[reference]), mock.patch.object(settings, 'problems', return_value=[]), mock.patch.object(tui, '_multi_select', return_value=selection), mock.patch.object(tui, '_set_fission_counts', return_value=True), mock.patch.object(tui, '_confirm', return_value=True), mock.patch.object(tui, '_flash') as flash:
        jobs = tui._wizard_montage(None, 100)
        assert jobs and '--clip-dir' in jobs[0]
        assert '--dedup-dir' in jobs[0]
        assert '--no-pip' not in jobs[0]
        flash.assert_not_called()
        for candidate in _collect_files(pip):
            candidate.unlink()

        assert tui._wizard_montage(None, 100) is None
        warning = flash.call_args.args[1]
        assert '画中画素材不可用' in warning
        for candidate in _collect_files(background):
            candidate.unlink()

        assert tui._wizard_montage(None, 100) is None
        warning = flash.call_args.args[1]
        assert '未找到混剪切片' in warning and str(background) in warning


def test_flash_keeps_long_chinese_path_across_lines():
    screen = mock.Mock()
    screen.getmaxyx.return_value = (30, 50)
    path = '/Volumes/Lexar/' + '中文 长目录/' * 6
    with mock.patch.object(tui, '_safe_add') as draw, mock.patch.object(tui, '_footer'):
        tui._flash(screen, '背景切片：\n' + path + '\n画中画切片：\n' + path)
    header_rows = tui._brand(screen)
    shown = ''.join(call.args[3][1:] for call in draw.call_args_list if call.args[1] >= header_rows and call.args[3].strip())
    assert shown == '背景切片：' + path + '画中画切片：' + path
    assert all(tui._disp_width(call.args[3]) <= 45 for call in draw.call_args_list if call.args[1] >= header_rows and call.args[3].strip())
    screen.getch.assert_called_once()
