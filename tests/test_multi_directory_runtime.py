"""Multi-root processing must use all configured directories without duplicate outputs."""
import json
from pathlib import Path
from unittest import mock

import pytest

from pycut import cli, config
from pycut.commands import cover_cmd, dedup_cmd, montage_cmd, montage_pipeline
from pycut.core import montage_clips, montage_pip, montage_intro, scan
from pycut.core.output_paths import matching_root, validate_targets, flat_output, sibling


def test_recursive_multi_roots_share_candidates_without_symlink_cycles(tmp_path):
    first = tmp_path / 'first'
    nested = first / 'a/b/c/d/e'
    nested.mkdir(parents=True)
    second = tmp_path / 'second'
    second.mkdir()
    a = nested / 'a.mp4'
    b = second / 'b.MOV'
    a.touch(); b.touch()
    (nested / 'cycle').symlink_to(first, target_is_directory=True)
    encoded = json.dumps([str(first), str(nested), str(second)])
    expected = {a.resolve(), b.resolve()}
    assert set(scan.list_videos_recursive(encoded, max_depth=None, raise_on_error=True)) == expected
    assert set(montage_clips._collect_files(encoded)) == expected
    assert set(montage_pip._iter_mp4(encoded)) == expected
    assert matching_root(a, encoded) == nested
    with pytest.raises(OSError):
        scan.list_videos_recursive([first, tmp_path/'missing'], max_depth=None, raise_on_error=True)


def test_repeat_and_json_cli_directories_are_both_processed(tmp_path):
    roots = [tmp_path / 'a', tmp_path / 'b']
    for index, root in enumerate(roots):
        root.mkdir()
        (root / f'{index}.mp4').touch()
    args = cli.build_parser().parse_args(['cover', '--src-dir', str(roots[0]),
                                          '--src-dir', json.dumps([str(roots[1]), str(roots[0])])])
    assert len(cover_cmd._pick_clips(args)) == 2
    with mock.patch.object(config, 'CLIP_DIRS', roots):
        assert len(cover_cmd._pick_clips(cli.build_parser().parse_args(['cover']))) == 2
        assert len(dedup_cmd._pick_srcs(cli.build_parser().parse_args(['dedup']))) == 2


def test_same_parent_roots_collision_is_rejected_before_processing(tmp_path):
    sources = [tmp_path/'a'/'same.mp4', tmp_path/'b'/'same.mp4']
    roots = [p.parent for p in sources]
    pairs = [(p, flat_output(p, sibling(matching_root(p, roots), '去重'))) for p in sources]
    with pytest.raises(ValueError, match='同名冲突'):
        validate_targets(pairs)


def test_montage_flattens_reference_batch_and_preserves_repeated_roots(tmp_path):
    reference = tmp_path / 'reference'
    source = reference / 'a' / '1.mp4'
    source.parent.mkdir(parents=True); source.touch()
    background = [tmp_path/'bg1', tmp_path/'bg2']
    for root in background: root.mkdir()
    args = cli.build_parser().parse_args(['montage', '--source', str(source),
        '--clip-dir', str(background[0]), '--clip-dir', str(background[1]),
        '--dedup-dir', '[]', '--intro-dir', '[]', '--out-dir', str(tmp_path/'out'), '--no-sub'])
    result = {'final':'ok', 'source_duration':3, 'segments':1, 'clips_used':1, 'final_duration':3}
    with mock.patch.object(config, 'REFERENCE_DIR', reference), \
         mock.patch.object(montage_cmd, 'self_check'), \
         mock.patch.object(montage_cmd, 'install_signal_handlers'), \
         mock.patch.object(montage_pipeline, 'run_pipeline', return_value=result) as run:
        assert montage_cmd.run(args) == 0
    opts = run.call_args.args[0]
    assert opts.clip_dir == background
    assert not opts.use_pip
    assert opts.intro_dir is None
    assert opts.out_dir.parent == tmp_path / 'out'
    assert opts.out_dir.name.startswith('混剪-')
    assert opts.out_name == '1_1.mp4'


def test_intro_multiple_empty_directories_skip_but_bad_root_fails(tmp_path):
    roots = [tmp_path/'one', tmp_path/'two']
    for root in roots: root.mkdir()
    source = tmp_path/'source.mp4'; source.write_bytes(b'unchanged')
    kwargs = dict(seed=1, canvas=(180,320), stickers=[], crf='20', keep_audio=False)
    assert montage_intro.prepend(source, roots, **kwargs) is None
    with pytest.raises(OSError):
        montage_intro.prepend(source, [*roots,tmp_path/'missing'], **kwargs)
    assert source.read_bytes() == b'unchanged'


def test_dedup_multi_root_batches_keep_sibling_outputs_and_explicit_bucket_root(tmp_path):
    roots = [tmp_path/'one'/'clips', tmp_path/'two'/'clips']
    for root in roots:
        root.mkdir(parents=True)
        (root/'clip.mp4').touch()
    for explicit in ('', str(tmp_path/'有人脸')):
        argv = ['dedup','--src-dir',str(roots[0]),'--src-dir',str(roots[1])]
        if explicit: argv += ['--out-dir',explicit]
        args = cli.build_parser().parse_args(argv)
        with mock.patch.object(dedup_cmd,'self_check'), \
             mock.patch.object(dedup_cmd,'install_signal_handlers'), \
             mock.patch.object(dedup_cmd,'process_batch',return_value=[]) as batch, \
             mock.patch.object(dedup_cmd,'write_report',return_value=tmp_path/'report.json'):
            if explicit:
                # Two same-named sources must not race in a shared explicit output.
                assert dedup_cmd.run(args) == 2
                batch.assert_not_called()
            else:
                assert dedup_cmd.run(args) == 0
                assert {Path(call.args[1].out_dir) for call in batch.call_args_list} == {
                    tmp_path/'one'/'去重',tmp_path/'two'/'去重'}


def test_montage_auto_selects_only_reference_and_rejects_ambiguity(tmp_path):
    reference=tmp_path/'reference'; source=reference/'a'/'1.mp4'
    source.parent.mkdir(parents=True);source.touch()
    args=cli.build_parser().parse_args(['montage','--no-sub','--out-dir',str(tmp_path/'out')])
    result={'final':'ok','source_duration':3,'segments':1,'clips_used':1,'final_duration':3}
    with mock.patch.object(config,'REFERENCE_DIR',reference), \
         mock.patch.object(montage_cmd,'self_check'), \
         mock.patch.object(montage_cmd,'install_signal_handlers'), \
         mock.patch.object(montage_pipeline,'run_pipeline',return_value=result) as run:
        assert montage_cmd.run(args)==0
        assert run.call_args.args[0].source==source
        (reference/'b').mkdir();(reference/'b'/'1.mp4').touch()
        run.reset_mock()
        assert montage_cmd.run(args)==2
        run.assert_not_called()
        probe=cli.build_parser().parse_args(['montage','--probe','--clip-dir',str(tmp_path/'missing')])
        assert montage_cmd.run(probe)==2


def test_dedup_interruption_does_not_start_next_root(tmp_path):
    roots=[tmp_path/'one'/'clips',tmp_path/'two'/'clips']
    for root in roots:
        root.mkdir(parents=True);(root/'clip.mp4').touch()
    args=cli.build_parser().parse_args(['dedup','--src-dir',str(roots[0]),'--src-dir',str(roots[1])])
    from pycut.core.runtime import STOP
    def stop_after_one(*args,**kwargs):
        STOP.flag=True
        return []
    with mock.patch.object(dedup_cmd,'self_check'), \
         mock.patch.object(dedup_cmd,'install_signal_handlers'), \
         mock.patch.object(dedup_cmd,'process_batch',side_effect=stop_after_one) as batch, \
         mock.patch.object(dedup_cmd,'write_report',return_value=tmp_path/'report.json'):
        assert dedup_cmd.run(args)==130
    assert batch.call_count==1
    STOP.flag=False


@pytest.mark.parametrize('task,command',[('字幕查重','cover'),('去重','dedup')])
def test_parent_child_roots_rerun_excludes_only_actual_output(tmp_path,task,command):
    parent=tmp_path/'library';child=parent/'child'
    child.mkdir(parents=True)
    source=child/'source.mp4';source.touch()
    generated=parent/task;generated.mkdir()
    (generated/'source_1.mp4').touch()
    # An unrelated user folder with the same task name must remain input.
    legitimate=parent/'other'/task;legitimate.mkdir(parents=True)
    user=legitimate/'user.mp4';user.touch()
    args=cli.build_parser().parse_args([command,'--src-dir',str(parent),'--src-dir',str(child)])
    picker=cover_cmd._pick_clips if command=='cover' else dedup_cmd._pick_srcs
    assert set(picker(args))=={source,user}
    # A nested explicit output is also excluded, without matching all task names.
    explicit=parent/'published';explicit.mkdir();(explicit/'old.mp4').touch()
    args.out_dir=str(explicit)
    assert set(picker(args))=={source,user,generated/'source_1.mp4'}
    args.out_dir=str(parent)
    with pytest.raises(ValueError,match='输入目录相同'):
        picker(args)


@pytest.mark.parametrize('task',['去重','字幕查重'])
def test_input_root_named_after_task_is_valid_with_separate_output(tmp_path,task):
    from pycut.core.output_paths import without_outputs
    root=tmp_path/task;root.mkdir();source=root/'clip.mp4';source.touch()
    assert without_outputs([source],[root],task,tmp_path/'elsewhere')==[source]
    with pytest.raises(ValueError,match='输入目录相同'):
        without_outputs([source],[root],task,root)
