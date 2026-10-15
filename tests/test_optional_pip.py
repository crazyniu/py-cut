"""画中画可选：未配置 PYCUT_PIP_DIR 不扫描/不渲染；勾选才校验并复用 --no-pip。"""
from pathlib import Path
from unittest import mock

import pytest

from pycut import cli, config, settings, tui


def _selection(source):
    return ([source], {str(source): 1})


def _wizard_env(background, pip, source, confirm_side_effect=None):
    """构造 montage 向导环境，返回 (jobs, flash, confirm_mock, collect)。"""
    with mock.patch.object(config, 'MONTAGE_CLIP_DIR', background), \
         mock.patch.object(config, 'MONTAGE_DEDUP_DIR', pip), \
         mock.patch.object(tui, 'list_videos_flat', return_value=[source]), \
         mock.patch.object(tui, '_multi_select', return_value=_selection(source)), \
         mock.patch.object(tui, '_set_fission_counts', return_value=True), \
         mock.patch.object(tui, '_confirm', side_effect=confirm_side_effect
                           or (lambda *a: True)) as confirm, \
         mock.patch.object(tui, '_flash') as flash:
        jobs = tui._wizard_montage(None, 100)
    return jobs, flash, confirm


def test_montage_without_pip_dir_passes_no_pip_and_does_not_scan(tmp_path):
    background = tmp_path / '背景'
    background.mkdir()
    (background / 'bg.mp4').write_bytes(b'x')
    pip = tmp_path / '切片'
    source = tmp_path / '爆款.mp4'

    scanned = []

    def spy(path):
        scanned.append(Path(path))
        return [Path('candidate')]

    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR': ''}), \
         mock.patch('pycut.core.montage_clips._collect_files', side_effect=spy):
        jobs, flash, confirm = _wizard_env(background, pip, source)
    assert jobs
    args = cli.build_parser().parse_args(jobs[0])
    assert args.no_pip is True
    assert not args.dedup_dir
    # 未配置 → 复选框默认关；且从未扫描画中画目录。
    assert confirm.call_args.args[2][0][2] is False
    assert pip not in scanned
    flash.assert_not_called()


def test_montage_with_pip_dir_checked_passes_dedup_dir(tmp_path):
    background = tmp_path / '背景'
    background.mkdir()
    (background / 'bg.mp4').write_bytes(b'x')
    pip = tmp_path / '切片'
    nested = pip / '有人脸'
    nested.mkdir(parents=True)
    (nested / 'p_1.mp4').write_bytes(b'x')
    source = tmp_path / '爆款.mp4'

    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR': str(pip)}):
        jobs, flash, confirm = _wizard_env(background, pip, source)
    args = cli.build_parser().parse_args(jobs[0])
    assert args.dedup_dir == str(pip)
    assert args.no_pip is False
    assert confirm.call_args.args[2][0][2] is True
    flash.assert_not_called()


def test_montage_with_pip_dir_unchecked_passes_no_pip(tmp_path):
    background = tmp_path / '背景'
    background.mkdir()
    (background / 'bg.mp4').write_bytes(b'x')
    pip = tmp_path / '切片'
    nested = pip / '有人脸'
    nested.mkdir(parents=True)
    (nested / 'p_1.mp4').write_bytes(b'x')
    source = tmp_path / '爆款.mp4'

    def uncheck(_scr, _lines, _toggles, values):
        values[:] = [False]
        return True

    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR': str(pip)}):
        jobs, flash, confirm = _wizard_env(background, pip, source,
                                           confirm_side_effect=uncheck)
    args = cli.build_parser().parse_args(jobs[0])
    assert args.no_pip is True
    assert not args.dedup_dir
    flash.assert_not_called()


def test_montage_checking_pip_without_config_is_rejected(tmp_path):
    background = tmp_path / '背景'
    background.mkdir()
    (background / 'bg.mp4').write_bytes(b'x')
    pip = tmp_path / '切片'
    source = tmp_path / '爆款.mp4'

    def check(_scr, _lines, _toggles, values):
        values[:] = [True]
        return True

    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR': ''}):
        jobs, flash, confirm = _wizard_env(background, pip, source,
                                           confirm_side_effect=check)
    assert jobs is None
    assert '未配置 PYCUT_PIP_DIR' in flash.call_args.args[1]


@pytest.mark.parametrize('enabled', [False, True])
def test_montage_back_keeps_pip_choice_and_fission_count(tmp_path, enabled):
    source = tmp_path / '爆款.mp4'
    selection = _selection(source)
    defaults = []

    def confirm(_scr, _lines, toggles, values):
        defaults.append(toggles[0][2])
        if len(defaults) == 1:
            values[:] = [enabled]
            return False
        values[:] = [toggles[0][2]]
        return True

    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR': str(tmp_path)}), \
         mock.patch.object(tui, 'list_videos_flat', return_value=[source]), \
         mock.patch.object(tui, '_multi_select', return_value=selection) as select, \
         mock.patch.object(tui, '_set_fission_counts', return_value=True) as counts, \
         mock.patch.object(tui, '_confirm', side_effect=confirm), \
         mock.patch('pycut.core.montage_clips._collect_files', return_value=[source]) as collect:
        selection[1][str(source)] = 7
        jobs = tui._wizard_montage(None, 100)
    assert defaults == [True, enabled]
    args = cli.build_parser().parse_args(jobs[0])
    assert args.no_pip is not enabled
    assert args.fission == 7
    assert counts.call_count == 2
    assert counts.call_args.args[2] is selection[1]
    select.assert_called_once()
    assert collect.call_count == (2 if enabled else 1)


def test_settings_pip_dir_is_optional_but_validated_when_set(tmp_path):
    # 留空不再报「尚未配置」。
    assert settings.problems({'PYCUT_SOURCE_DIR': str(tmp_path),
                              'PYCUT_REFERENCE_DIR': str(tmp_path),
                              'PYCUT_CLIP_DIR': str(tmp_path),
                              'PYCUT_OUTPUT_DIR': str(tmp_path),
                              'PYCUT_PIP_DIR': ''}) == []
    # 配置了不存在的目录仍报错。
    missing = tmp_path / '不存在'
    assert '不存在或无法访问' in '；'.join(
        settings.problems({'PYCUT_PIP_DIR': str(missing)}, require_all=False))
