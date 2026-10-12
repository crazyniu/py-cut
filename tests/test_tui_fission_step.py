"""The count step is required, editable, bounded and preserves backtracking."""
from pathlib import Path
from unittest import mock
from pycut import cli, tui


def screen(keys):
    result=mock.Mock()
    result.getmaxyx.return_value=(24,100)
    result.getch.side_effect=keys
    return result


def test_full_keyboard_flow_requires_count_after_selection():
    source=Path('/素材/爆款.mp4')
    terminal=screen([ord(' '),10,ord('5'),10,10])
    with mock.patch.object(tui,'_attr',return_value=0),mock.patch('pycut.core.montage_clips._collect_files',return_value=[source]),mock.patch.object(tui,'list_videos_flat',return_value=[source]):
        commands=tui._wizard_montage(terminal,100)
    assert cli.build_parser().parse_args(commands[0]).fission==5
    rendered='\n'.join(c.args[2] for c in terminal.addstr.call_args_list)
    assert '2/3 设置裂变数量' in rendered and '生成条数' in rendered
    assert '5 支成片' in rendered


def test_multi_video_counts_and_invalid_zero_are_not_silently_defaulted():
    chosen=[Path('/a.mp4'),Path('/中文 第二支.mp4')]
    counts={str(p):1 for p in chosen}
    terminal=screen([ord('0'),10,127,ord('5'),10,ord('1'),ord('2'),10])
    with mock.patch.object(tui,'_attr',return_value=0):
        assert tui._set_fission_counts(terminal,chosen,counts)
    assert list(counts.values())==[5,12]
    assert any('请输入 1～99' in c.args[2] for c in terminal.addstr.call_args_list)


def test_cancel_second_count_preserves_first_and_returns_selection():
    chosen=[Path('/a.mp4'),Path('/b.mp4')]
    counts={str(p):1 for p in chosen}
    terminal=screen([ord('5'),10,27])
    with mock.patch.object(tui,'_attr',return_value=0):
        assert not tui._set_fission_counts(terminal,chosen,counts)
    assert counts=={str(chosen[0]):5,str(chosen[1]):1}


def test_final_review_back_keeps_counts_and_requires_reconfirmation():
    source=Path('/素材/a.mp4')
    selection=([source],{str(source):1})
    terminal=screen([ord('7'),10,10])
    with mock.patch.object(tui,'_attr',return_value=0),mock.patch('pycut.core.montage_clips._collect_files',return_value=[source]),mock.patch.object(tui,'list_videos_flat',return_value=[source]),mock.patch.object(tui,'_multi_select',return_value=selection) as select,mock.patch.object(tui,'_confirm',side_effect=[False,True]):
        commands=tui._wizard_montage(terminal,100)
    assert cli.build_parser().parse_args(commands[0]).fission==7
    assert select.call_count==1
