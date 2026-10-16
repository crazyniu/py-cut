"""ASS is prepared before PIP encoding, burned once, and cleaned on every outcome."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from pycut import config
from pycut.commands import montage_pipeline
from pycut.core import montage_clips, montage_pip


@pytest.mark.parametrize('failure',[None,'render','prepare'])
def test_pip_ass_is_burned_once_and_cleaned_with_atomic_publication(tmp_path,failure):
    source=tmp_path/'source.mp4';source.touch()
    srt=tmp_path/'source.srt'
    srt.write_text('1\n00:00:00,400 --> 00:00:00,800\nFIRST\n\n2\n00:00:02,900 --> 00:00:03,500\nLAST\n')
    if failure=='prepare':srt.write_text('invalid subtitle')
    out=tmp_path/'out';out.mkdir()
    final=out/'source_1.mp4';final.write_bytes(b'previous-video')
    prepared={'source':SimpleNamespace(duration=3,width=180,height=320,segments=[],speech_total=3,has_audio=True),
              'background':[object()], 'pip':[montage_pip.PipItem(tmp_path/'pip.mp4',3)],'stickers':[]}
    opts=montage_pipeline.MontageOptions(source=source,out_dir=out,srt=str(srt),
        prepared=prepared,seed=7,online_stickers=False,seam_fade=0)
    captured=[]
    def background(rows,destination,**kwargs):
        destination.write_bytes(b'background'*200)
        return {'ok':True}
    def render(base,timeline,working,**kwargs):
        ass=kwargs['ass_path'];captured.append(ass)
        assert ass.is_file()
        text=ass.read_text()
        assert '0:00:00.40,0:00:00.80' in text
        assert '0:00:02.90,0:00:03.00' in text
        assert kwargs['seed']==7
        assert 'variation_seed' not in kwargs
        if failure=='render':raise RuntimeError('native encode failed')
        working.write_bytes(b'rendered-with-subtitles'*200)
    with mock.patch.object(config,'MONTAGE_TMP',tmp_path/'temp'), \
         mock.patch.object(montage_clips,'pick_clips',return_value=[{'item':object(),'take':3}]), \
         mock.patch.object(montage_clips,'concat_clips',side_effect=background), \
         mock.patch.object(montage_pip,'build_pip_cmd',side_effect=render) as encode, \
         mock.patch.object(montage_pipeline,'_apply_variation'), \
         mock.patch.object(montage_pipeline,'_duration',return_value=3), \
         mock.patch.object(montage_pipeline,'_attach_audio',return_value=True) as audio, \
         mock.patch.object(montage_pipeline,'_burn_subtitles') as second_burn:
        result=montage_pipeline.run_pipeline(opts)
    second_burn.assert_not_called()
    assert not list((tmp_path/'temp').glob('*.ass'))
    assert all(not path.exists() for path in captured)
    if failure:
        assert '步骤C' in result['error']
        assert final.read_bytes()==b'previous-video'
        audio.assert_not_called()
        if failure=='prepare':encode.assert_not_called()
    else:
        assert result['final']==str(final)
        audio.assert_called_once()
        assert final.read_bytes()!=b'previous-video'
        manifest = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        assert manifest['pip_variation_seed'] == 7
        assert manifest['pip_effects'] == 'standard_geometry_pixel+optional_mirror'


def test_no_pip_keeps_separate_subtitle_step(tmp_path):
    source=tmp_path/'source.mp4';source.touch()
    srt=tmp_path/'source.srt';srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nTEXT\n')
    opts=montage_pipeline.MontageOptions(source=source,out_dir=tmp_path/'out',use_pip=False,
        keep_audio=False,srt=str(srt),online_stickers=False,seam_fade=0,
        prepared={'source':SimpleNamespace(duration=3,width=180,height=320,segments=[],speech_total=3,has_audio=False),
                  'background':[object()],'stickers':[]})
    def background(rows,destination,**kwargs):
        destination.write_bytes(b'base'*400);return {'ok':True}
    def publish(command,stage,working):working.write_bytes(b'working'*200);return True
    with mock.patch.object(config,'MONTAGE_TMP',tmp_path/'temp'), \
         mock.patch.object(montage_clips,'pick_clips',return_value=[{'item':object(),'take':3}]), \
         mock.patch.object(montage_clips,'concat_clips',side_effect=background), \
         mock.patch.object(montage_pipeline,'_apply_variation'), \
         mock.patch.object(montage_pipeline,'_duration',return_value=3), \
         mock.patch.object(montage_pipeline,'_publish_stage',side_effect=publish), \
         mock.patch.object(montage_pipeline,'_burn_subtitles',return_value=True) as burn:
        result=montage_pipeline.run_pipeline(opts)
    assert result['final']
    burn.assert_called_once()
    manifest = json.loads(Path(result['report']).read_text(encoding='utf-8'))
    assert manifest['pip_variation_seed'] is None
    assert manifest['pip_effects'] is None
