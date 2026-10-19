"""CLI and TUI share one flat, video-only output folder per batch."""
from pathlib import Path
from unittest import mock

from pycut import cli, config, tui, tui_runner
from pycut.commands import montage_cmd, montage_pipeline
from pycut.core.output_paths import montage_batch, montage_stem


def test_batch_paths_are_new_and_names_do_not_overwrite_casefolded_variants(tmp_path):
    assert montage_batch(tmp_path).parent == tmp_path
    assert montage_batch(tmp_path) != montage_batch(tmp_path)
    (tmp_path/'same_1.mp4').touch()
    (tmp_path/'SAME_2_2.MP4').touch()
    assert montage_stem(Path('same.mp4'), tmp_path, 2) == 'same_3'


def test_same_named_scripts_and_fissions_share_flat_batch_and_cli_prints_it(tmp_path):
    root = tmp_path.resolve()/'OUT'
    batch = montage_batch(root)
    sources = [tmp_path/'a'/'1.mp4', tmp_path/'b'/'1.mp4']
    for source in sources:
        source.parent.mkdir(); source.touch()

    def publish(opts):
        opts.out_dir.mkdir(parents=True, exist_ok=True)
        final = opts.out_dir/opts.out_name
        assert not final.exists()
        final.write_bytes(b'verified video placeholder')
        return dict(final=str(final),source_duration=10,segments=2,
                    clips_used=3,final_duration=10)

    with mock.patch.object(montage_cmd,'self_check'), \
         mock.patch.object(montage_pipeline,'run_pipeline',side_effect=publish), \
         mock.patch.object(montage_cmd,'log') as log:
        for source in sources:
            args = cli.build_parser().parse_args(['montage','--source',str(source),
                '--out-dir',str(root),'--batch-dir',str(batch),'--fission','2',
                '--no-sub','--no-pip','--intro-dir',''])
            assert montage_cmd.run(args)==0
        assert log.call_args.args == (f'输出目录：{batch}',)
    assert sorted(p.name for p in batch.iterdir()) == ['1_1.mp4','1_2.mp4','1_2_1.mp4','1_2_2.mp4']
    assert list(root.iterdir()) == [batch]


def test_tui_selected_sources_share_one_batch_and_next_execution_uses_another(tmp_path):
    root = tmp_path.resolve()/'OUT'
    references = tmp_path/'reference'
    sources = [references/'a'/'1.mp4', references/'b'/'1.mp4']
    counts = {str(src):1 for src in sources}
    with mock.patch('pycut.core.output_paths.montage_output',return_value=root), \
         mock.patch.object(config,'REFERENCE_DIR',references), \
         mock.patch.object(config,'CLIP_DIRS',[tmp_path]), \
         mock.patch.object(config,'PIP_DIRS',[]), \
         mock.patch.object(config,'INTRO_DIRS',[]), \
         mock.patch('pycut.core.montage_clips._collect_files',return_value=[tmp_path/'clip.mp4']), \
         mock.patch.object(tui,'list_videos_recursive',return_value=sources), \
         mock.patch.object(tui,'_multi_select',return_value=(sources,counts)), \
         mock.patch.object(tui,'_set_fission_counts',return_value=True), \
         mock.patch.object(tui,'_confirm',return_value=True):
        first = [cli.build_parser().parse_args(job) for job in tui._wizard_montage(None,100)]
        second = [cli.build_parser().parse_args(job) for job in tui._wizard_montage(None,100)]
    assert len({job.batch_dir for job in first})==1
    assert Path(first[0].batch_dir).parent==root
    assert first[0].batch_dir!=second[0].batch_dir
    assert not root.exists()  # Selection does not create output folders.


def test_finished_tui_wraps_full_chinese_batch_path():
    from collections import deque
    from test_tui_layout import Screen
    folder = Path('/Volumes/Lexar/项目/很长的中文素材目录/输出目录/混剪-20261019-180000-a1b2c3d4')
    jobs = [['montage','--source','/a.mp4','--out-dir',str(folder.parent)]]
    batch = tui_runner._montage_plan(jobs)
    batch.update(files=[str(folder/'a_1.mp4')],completed=1,current_planned=1)
    screen = Screen(size=(24,80))
    with mock.patch.object(tui,'_attr',return_value=0):
        tui_runner._draw(screen,jobs[0],1,1,deque(),0,'success',batch=batch,results=[{}])
    text = ''.join(value for _,_,value in screen.printed)
    assert '输出目录：'+str(folder) in text
