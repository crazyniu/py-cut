"""Output contracts: source safety, flat resume and three-folder montage."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest import mock

from pycut import cli, config, tui
from pycut.commands import cover_cmd
from pycut.core import dedup, output_paths, slice_pipeline, subtitle


def test_sibling_layout_keeps_buckets_and_numbers():
    src = Path('/data/切片/有人脸/演示_2.mp4')
    assert dedup.default_output_path(src) == Path('/data/去重/有人脸/演示_2.mp4')
    assert dedup.resolve_output_path(src, dedup.DedupOptions(out_dir='/custom')) == Path('/custom/有人脸/演示_2.mp4')
    assert dedup.default_output_path(Path('/data/素材/演示.mp4')) == Path('/data/去重/演示_1.mp4')


def test_flat_resume_ignores_other_videos():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with mock.patch.object(config, 'OUT_FACE', root / '有人脸'), mock.patch.object(config, 'OUT_NOFACE', root / '无人脸'):
            config.OUT_FACE.mkdir()
            (config.OUT_FACE / '第一支_1.mp4').write_bytes(b'x' * 2048)
            (config.OUT_FACE / '第二支_1.mp4').write_bytes(b'y' * 2048)
            assert slice_pipeline.video_done('第一支', 1)
            assert not slice_pipeline.video_done('第一支', 2)
            assert len(subtitle.collect_clips()) == 2


def test_flat_collisions_and_input_overwrite_rejected():
    import pytest
    with pytest.raises(ValueError, match='同名冲突'):
        output_paths.validate_targets([(Path('/a/demo.mp4'), Path('/out/DEMO_1.mp4')),
                                      (Path('/b/DEMO.mp4'), Path('/out/demo_1.mp4'))])
    with pytest.raises(ValueError, match='覆盖输入'):
        output_paths.validate_targets([(Path('/a/demo.mp4'), Path('/a/demo.mp4'))])


def test_cover_publishes_copy_and_force_starts_from_source():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        src = root / '切片' / '有人脸' / 'demo_1.mp4'
        src.parent.mkdir(parents=True)
        src.write_bytes(b'original')
        dst = root / '字幕查重' / '有人脸' / src.name
        args = cli.build_parser().parse_args(['cover', '--src-dir', str(root / '切片'), '--force'])
        model = SimpleNamespace(enabled=True, failures=0, calls=0, switched_to=None)
        def process(llm, clip, **kwargs):
            assert clip.read_bytes() == b'original'
            clip.write_bytes(b'covered')
            subtitle._marker(clip).write_text('{}')
            return 'covered'
        with mock.patch.object(cover_cmd, 'self_check'), mock.patch.object(cover_cmd, 'make_llm', return_value=model), mock.patch.object(cover_cmd, 'process_clip', side_effect=process):
            assert cover_cmd.run(args) == 0
            assert cover_cmd.run(args) == 0
        assert src.read_bytes() == b'original'
        assert dst.read_bytes() == b'covered'
        with mock.patch.object(cover_cmd, 'self_check'), mock.patch.object(cover_cmd, 'make_llm', return_value=model), mock.patch.object(cover_cmd, 'process_clip', return_value='fail'):
            assert cover_cmd.run(args) == 2
        assert dst.read_bytes() == b'covered'


def test_montage_uses_configured_directories_and_output():
    source = Path('/data/爆款/demo.mp4')
    selection = ([source], {str(source): 5})
    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR': '/data/画中画'}), mock.patch.object(config, 'REFERENCE_DIR', Path('/data/爆款')), mock.patch.object(config, 'MONTAGE_CLIP_DIR', Path('/data/切片')), mock.patch.object(config, 'MONTAGE_DEDUP_DIR', Path('/data/画中画')), mock.patch.object(config, 'MONTAGE_OUT', Path('/data/混剪/1011')), mock.patch.object(tui, '_pick_dir') as pick, mock.patch.object(tui, 'list_videos_flat', return_value=[source]), mock.patch('pycut.core.montage_clips._collect_files', return_value=[source] * 3), mock.patch.object(tui, '_multi_select', return_value=selection), mock.patch.object(tui, '_set_fission_counts', return_value=True), mock.patch.object(tui, '_confirm', return_value=True):
        jobs = tui._wizard_montage(None, 100)
    args = cli.build_parser().parse_args(jobs[0])
    assert args.clip_dir == '/data/切片'
    assert args.dedup_dir == '/data/画中画'
    assert args.out_dir == '/data/混剪/1011'
    assert args.fission == 5
    pick.assert_not_called()


def test_slice_output_prefers_pip_and_keeps_explicit_override():
    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR':'/data/画中画 空格', 'PYCUT_CLIP_DIR':'/data/背景'}), mock.patch.object(config,'PIP_DIR',Path('/data/画中画 空格')):
        assert output_paths.slice_output_root(Path('/data/爆款/抖音')) == Path('/data/画中画 空格')
        assert output_paths.slice_output_root(Path('/data/爆款/抖音'),'/explicit') == Path('/explicit')
    with mock.patch.dict('os.environ', {'PYCUT_PIP_DIR':'', 'PYCUT_CLIP_DIR':'/data/背景'}):
        assert output_paths.slice_output_root(Path('/data/爆款/抖音')) == Path('/data/爆款/切片')


def test_slice_tui_confirmation_and_dedup_use_pip_root_without_timestamp():
    files=[Path('/source/demo.mp4')]
    target=Path('/画中画 切片')
    def confirm(screen,lines,toggles,values):
        assert ('画中画切片输出',str(target)) in lines
        values[:]=[False,True]
        return True
    with mock.patch.dict('os.environ',{'PYCUT_PIP_DIR':str(target)}),mock.patch.object(config,'SRC_DIR',Path('/source')),mock.patch.object(config,'CLIP_DIR',Path('/background')),mock.patch.object(config,'PIP_DIR',target),mock.patch('pycut.core.scan.list_videos_recursive',return_value=files),mock.patch.object(tui,'_confirm',side_effect=confirm):
        jobs=tui._wizard_slice(None,100)
        again=tui._wizard_slice(None,100)
    assert jobs==again
    assert cli.build_parser().parse_args(jobs[0]).out_dir==str(target)
    assert cli.build_parser().parse_args(jobs[1]).src_dir==str(target)
