"""Display orientation, stronger bounded effects, and doubled segment stickers."""
import json
from pathlib import Path
from unittest import mock

import pytest

from pycut import config
from pycut.core import dedup, montage_clips, montage_sticker


@pytest.mark.parametrize('rotation,size', [(-90,(1080,1920)),(90,(1080,1920)),
                                          (270,(1080,1920)),(-180,(1920,1080)),(0,(1920,1080))])
def test_probe_uses_display_orientation(rotation, size):
    video = {'codec_type':'video','width':1920,'height':1080,'duration':'4',
             'side_data_list':[{'rotation':rotation}]}
    with mock.patch.object(montage_clips, 'cached_probe', return_value=(
            0,json.dumps({'streams':[video]}),'')) as probe:
        details = {}
        item = montage_clips._probe_one(Path('phone.mov'),details=details)
    assert (item.width,item.height) == size
    assert details['width'] == 1920
    assert 'stream_side_data=rotation' in ' '.join(probe.call_args.args[1])


def test_rotation_tag_compatibility():
    video = {'codec_type':'video','width':1920,'height':1080,'duration':'4',
             'tags':{'rotate':'90'}}
    with mock.patch.object(montage_clips, 'cached_probe', return_value=(
            0,json.dumps({'streams':[video]}),'')):
        item = montage_clips._probe_one(Path('phone.mov'))
    assert (item.width,item.height) == (1080,1920)


def test_video_duration_requests_scalar_output_with_phone_side_data():
    with mock.patch.object(dedup,'run_cmd',return_value=(0,'31.800000\n','')) as run:
        assert dedup._video_duration(Path('phone-derived.mp4')) == 31.8
    args=run.call_args.args[0]
    assert args[args.index('-of')+1] == 'default=nw=1:nk=1'


def test_montage_effects_are_stronger_and_bounded_across_seeds():
    for seed in range(64):
        params, chain = dedup.montage_variation(216,384,seed)
        original = dedup.build_params('standard',seed,has_audio=False,width=216,height=384)
        assert params['preset'] == 'montage-strong'
        for key, upper in [('zoom',config.DEDUP_ZOOM_RANGE[1]),
                           ('contrast',config.DEDUP_CONTRAST_RANGE[1]),
                           ('saturation',config.DEDUP_SATURATION_RANGE[1]),
                           ('rotate_deg',config.DEDUP_ROTATE_DEG[1]),
                           ('brightness',config.DEDUP_BRIGHTNESS),
                           ('hue_deg',config.DEDUP_HUE_DEG),
                           ('noise',config.DEDUP_NOISE_RANGE[1]),
                           ('sharpen',config.DEDUP_SHARPEN_RANGE[1])]:
            assert abs(original[key]) <= abs(params[key]) + .0001 <= upper + .0002
        assert all(.988 <= gain <= 1.012 for gain in params['channel_gains'])
        assert 'setpts' not in chain and 'fps=' not in chain
        assert (params,chain) == dedup.montage_variation(216,384,seed)


def test_double_sticker_has_two_opposite_overlays(tmp_path):
    image = tmp_path/'image.png'
    image.touch()
    chain, label = montage_sticker.sticker_chain(image,(720,1280),double=True)
    assert chain.count('overlay=') == 2
    assert 'split=2' in chain and label == 'vstk'
