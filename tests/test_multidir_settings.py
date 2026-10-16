"""Multi-directory configuration remains a single safe, backward-compatible .env value."""
import json
from pathlib import Path
from unittest import mock

import pytest

from pycut import config, settings, cli, tui


def test_paths_roundtrip_legacy_json_repeat_and_overlapping_aliases(tmp_path):
    first = tmp_path / '中文 空格,目录'
    second = tmp_path / 'second'
    first.mkdir(); second.mkdir()
    with mock.patch.object(config, 'PROJECT_ROOT', tmp_path):
        assert settings.directories(str(first)) == [first]
        assert settings.directories([str(first), json.dumps([str(first), str(second)])]) == [first, second]
        (tmp_path / '.env').write_text('# keep\nOTHER=keep\nPYCUT_MAIN_KEY=secret\n')
        settings.save({'PYCUT_CLIP_DIR': json.dumps([str(first), str(second), str(first)]), 'PYCUT_MAIN_KEY': ''})
        values = settings.read_env()
        assert settings.directories(values['PYCUT_CLIP_DIR']) == [first, second]
        assert values['PYCUT_MAIN_KEY'] == 'secret'
        assert '# keep\nOTHER=keep' in (tmp_path / '.env').read_text()
        assert settings.problems(values, keys=('PYCUT_CLIP_DIR',)) == []
        settings.save({'PYCUT_INTRO_DIR': '[]'})
        assert settings.read_env()['PYCUT_INTRO_DIR'] == ''


@pytest.mark.parametrize('invalid', ['[bad', '[1]', '[null]', '["line\\nbreak"]'])
def test_invalid_path_arrays_rejected_without_changing_file(tmp_path, invalid):
    with mock.patch.object(config, 'PROJECT_ROOT', tmp_path):
        env = tmp_path / '.env'; env.write_text('OTHER=keep\n')
        with pytest.raises(ValueError): settings.save({'PYCUT_CLIP_DIR': invalid})
        assert env.read_text() == 'OTHER=keep\n'


def test_validation_is_task_specific_and_identifies_bad_row(tmp_path):
    valid = tmp_path / 'valid'; valid.mkdir()
    values = {'PYCUT_CLIP_DIR': json.dumps([str(valid), str(tmp_path / 'missing')])}
    errors = settings.problems(values, keys=('PYCUT_CLIP_DIR',))
    assert len(errors) == 1 and '第 2 项' in errors[0]
    assert settings.problems({'PYCUT_CLIP_DIR': str(valid)}, keys=('PYCUT_CLIP_DIR',)) == []
    assert 'PYCUT_SOURCE_DIR' not in settings.FIELDS
    assert settings.FIELDS['PYCUT_REFERENCE_DIR'][0] == '话术目录'


def test_public_commands_and_menu_have_three_tasks_only():
    assert cli.list_commands() == ['cover', 'dedup', 'montage']
    assert [item[0] for item in tui.FEATURES] == cli.list_commands()
    with pytest.raises(SystemExit): cli.build_parser().parse_args(['slice'])
    assert not hasattr(tui, '_wizard_slice')


def test_same_named_script_videos_are_distinguishable(tmp_path):
    with mock.patch.object(config, 'REFERENCE_DIR', tmp_path):
        assert tui._reference_label(tmp_path / 'a' / '1.mp4') == 'a/1.mp4'
        assert tui._reference_label(tmp_path / 'b' / '1.mp4') == 'b/1.mp4'
