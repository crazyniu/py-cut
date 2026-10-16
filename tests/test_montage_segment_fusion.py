"""段级效果只编码一次，标准段拼接不再次损失画质。"""
from pathlib import Path
from unittest.mock import patch

import pytest

from pycut.core import montage_clips as clips
from pycut.core.montage_sticker import build_sticker_cmd


def test_effect_order_double_seek_and_frame_budget(tmp_path):
    sticker = tmp_path / 'sticker.png'
    sticker.write_bytes(b'fixture')
    old, _ = build_sticker_cmd(Path('source.mp4'), [sticker], Path('out.mp4'),
                               (720, 1280), seed=13)
    cmd = clips._seg_cmd(Path('source.mp4'), .4, 2.3, Path('out.mp4'),
                         (720, 1280), 30, mirror=True, speed=1.15,
                         frames=60, dedup_seed=87, sticker=sticker,
                         sticker_seed=13, seam_fade=.2)
    graph = cmd[cmd.index('-filter_complex') + 1]
    old_graph = old[old.index('-filter_complex') + 1]
    assert graph.index('setpts=PTS/1.15000') < graph.index('hflip')
    assert 'tpad=' not in graph  # 源视频提前结束时不克隆末帧
    assert graph.index('geq=') < graph.index('fade=')
    assert old_graph.replace('[0:v]', '[segment]') in graph
    assert 'fade=t=in:st=0.400000:d=0.200000' in graph
    assert 'fade=t=out:st=2.200000:d=0.200000' in graph
    assert cmd.count('-ss') == 2
    second_input = cmd.index('-i', cmd.index('-i') + 1)
    assert second_input < cmd.index('-ss', cmd.index('-ss') + 1)
    assert cmd[cmd.index('-frames:v') + 1] == '60'
    assert cmd.count('-c:v') == 1


def test_missing_selected_sticker_is_not_silently_omitted(tmp_path):
    with pytest.raises(ValueError, match='贴纸'):
        clips._seg_cmd(Path('source.mp4'), 0, 2, tmp_path / 'out.mp4',
                       sticker=tmp_path / 'missing.png')


def test_normalized_concat_is_video_copy(tmp_path):
    segments = [tmp_path / 'segment.mp4']
    with patch.object(clips, '_normalize_segments', return_value=segments), \
            patch.object(clips, '_write_concat_list', return_value=tmp_path / 'list.txt'):
        cmd, _ = clips.build_concat_cmd([{'item': object(), 'take': 2}], tmp_path / 'out.mp4')
    assert cmd[cmd.index('-c:v') + 1] == 'copy'
    assert '-vf' not in cmd and '-r' not in cmd
    assert cmd[cmd.index('-map') + 1] == '0:v:0'
    assert cmd[-1] != str(tmp_path / 'out.mp4')  # 仍先写原子发布临时文件。
