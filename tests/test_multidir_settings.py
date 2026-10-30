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
    assert settings.FIELDS['PYCUT_SOURCE_DIR'][0] == '原视频目录'
    assert settings.FIELDS['PYCUT_REFERENCE_DIR'][0] == '话术目录'


def test_public_commands_and_menu_have_four_tasks_without_slice():
    assert cli.list_commands() == ['cover', 'dedup', 'montage', 'fission']
    assert [item[0] for item in tui.FEATURES] == cli.list_commands()
    with pytest.raises(SystemExit): cli.build_parser().parse_args(['slice'])
    assert not hasattr(tui, '_wizard_slice')


def test_same_named_script_videos_are_distinguishable(tmp_path):
    with mock.patch.object(config, 'REFERENCE_DIR', tmp_path):
        assert tui._reference_label(tmp_path / 'a' / '1.mp4') == 'a/1.mp4'
        assert tui._reference_label(tmp_path / 'b' / '1.mp4') == 'b/1.mp4'


def test_cli_checks_only_libraries_it_will_actually_use(tmp_path):
    from pycut.commands import cover_cmd, montage_cmd
    values = {'PYCUT_CLIP_DIR': str(tmp_path), 'PYCUT_REFERENCE_DIR': '/missing',
              'PYCUT_PIP_DIR': '[invalid', 'PYCUT_INTRO_DIR': '[invalid'}
    with mock.patch.object(settings, 'effective', return_value=values), \
         mock.patch.object(config, 'CLIP_DIRS', [tmp_path]), \
         mock.patch.object(cover_cmd, 'run', return_value=0) as cover:
        assert cli.main(['cover']) == 0
        assert settings.directories(cover.call_args.args[0].src_dir) == [tmp_path]
    with mock.patch.object(settings, 'effective', return_value=values), \
         mock.patch.object(montage_cmd, 'run', return_value=0) as montage:
        args = ['montage', '--source', str(tmp_path / 'a.mp4'),
                '--clip-dir', str(tmp_path), '--no-pip']
        assert cli.main(args + ['--intro-dir', '']) == 0
        montage.assert_called_once()
        montage.reset_mock()
        assert cli.main(args) == 2  # malformed enabled default must not become an empty library
        montage.assert_not_called()


def test_malformed_environment_list_keeps_configuration_service_importable():
    with mock.patch.dict('os.environ', {'PYCUT_INTRO_DIR': '[invalid'}):
        assert config._configured_directories('PYCUT_INTRO_DIR') == []
        assert settings.problems({'PYCUT_INTRO_DIR': '[invalid'}, require_all=False)


def test_montage_confirmation_keeps_total_visible_on_first_summary_row(tmp_path):
    references = tmp_path / '话术'
    clips = tmp_path / '切片'; clips.mkdir()
    (clips / 'clip.mp4').touch()
    chosen = []
    for folder in ('a', 'b'):
        path = references / folder / '1.mp4'; path.parent.mkdir(parents=True); path.touch(); chosen.append(path)
    counts = {str(chosen[0]): 1, str(chosen[1]): 2}
    with mock.patch.object(config, 'REFERENCE_DIR', references), \
         mock.patch.object(config, 'CLIP_DIRS', [clips]), \
         mock.patch.object(config, 'PIP_DIRS', []), \
         mock.patch.object(config, 'INTRO_DIRS', []), \
         mock.patch.object(tui, '_multi_select', return_value=(chosen, counts)), \
         mock.patch.object(tui, '_set_fission_counts', return_value=True), \
         mock.patch.object(tui, '_confirm', return_value=True) as confirm:
        jobs = tui._wizard_montage(None, 60)
    assert '共 3 支成片' in confirm.call_args.args[1][0][1]
    assert len(jobs) == 2
    for job in jobs:
        args = cli.build_parser().parse_args(job)
        assert settings.directories(args.clip_dir) == [clips]
        assert args.no_pip


def test_unconfigured_montage_output_uses_script_root_sibling(tmp_path):
    reference = tmp_path / '话术'
    source = reference / 'a' / '1.mp4'; source.parent.mkdir(parents=True); source.touch()
    clips = tmp_path / '切片'; clips.mkdir(); (clips / 'clip.mp4').touch()
    with mock.patch.dict('os.environ', {'PYCUT_OUTPUT_DIR': ''}), \
         mock.patch.object(config, 'REFERENCE_DIR', reference), \
         mock.patch.object(config, 'CLIP_DIRS', [clips]), \
         mock.patch.object(config, 'PIP_DIRS', []), \
         mock.patch.object(config, 'INTRO_DIRS', []), \
         mock.patch.object(tui, '_multi_select', return_value=([source], {str(source):1})), \
         mock.patch.object(tui, '_set_fission_counts', return_value=True), \
         mock.patch.object(tui, '_confirm', return_value=True):
        jobs = tui._wizard_montage(None, 100)
    args = cli.build_parser().parse_args(jobs[0])
    assert Path(args.out_dir) == tmp_path / '混剪'
    assert Path(args.batch_dir).parent == Path(args.out_dir)
