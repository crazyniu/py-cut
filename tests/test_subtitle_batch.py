"""Multi-image subtitle requests retain every sampled frame and reject mismatches."""
import json
from pathlib import Path
from unittest import mock
import numpy as np
import pytest
from pycut.core import llm, subtitle


def test_single_and_multi_image_openai_payloads():
    client=llm.LLMClient('http://local.invalid/v1','test','model')
    response=json.dumps({'choices':[{'message':{'content':'{"frames":[]}'}}]})
    frame=np.zeros((8,8,3),np.uint8)
    with mock.patch.object(client,'_post',return_value=(200,response)) as post:
        client.ask_vision(frame,'face')
        single=post.call_args.args[0]['messages'][0]['content']
        assert len(single)==2 and single[1]['type']=='image_url'
        client.ask_vision([frame]*5,'subtitle')
        batch=post.call_args.args[0]['messages'][0]['content']
        assert len(batch)==6 and all(v['type']=='image_url' for v in batch[1:])
        assert post.call_count==2


@pytest.mark.parametrize('replies',[
    [], [{'frame':i,'has_subtitle':False} for i in range(4)],
    [{'frame':0,'has_subtitle':False}]*5,
    [{'frame':i,'has_subtitle':False} for i in range(1,6)],
    [{'frame':False,'has_subtitle':False}]+[{'frame':i,'has_subtitle':False} for i in range(1,5)],
    [{'frame':i} for i in range(5)],
])
def test_incomplete_or_mismatched_batch_cannot_mark_clean(tmp_path,replies):
    values={'format=duration':'4','stream=width':'720','stream=height':'1280'}
    client=mock.Mock(enabled=True)
    client.ask_vision.return_value={'frames':replies}
    frame=np.zeros((8,8,3),np.uint8)
    with mock.patch.object(subtitle,'probe_scalar',side_effect=lambda _,k:values[k]),mock.patch.object(subtitle,'_grab_frame',return_value=True),mock.patch.object(subtitle.cv2,'imread',return_value=frame):
        clip=tmp_path/'test.mp4'
        assert subtitle.process_clip(client,clip)=='review'
        assert not subtitle._marker(clip).exists()
        assert client.ask_vision.call_count==1


def test_old_mosaic_marker_is_not_a_verified_frosted_output(tmp_path):
    clip=tmp_path/'test.mp4'
    marker=subtitle._marker(clip)
    marker.write_text('{"version":2,"subtitle":true,"verified":true}')
    assert not subtitle.verified_clip(clip)
    marker.write_text('{"version":2,"subtitle":true,"verified":true,"effect":"frosted"}')
    assert subtitle.verified_clip(clip)
    marker.write_text('{"version":2,"subtitle":false}')
    assert subtitle.verified_clip(clip)


def test_exhausted_deadline_counts_failure_and_opens_existing_circuit():
    client=llm.LLMClient('http://local.invalid/v1','test','model')
    frame=np.zeros((8,8,3),np.uint8)
    # Each request crosses the deadline; reaching the established failure limit
    # must stop sending every remaining clip to an unresponsive gateway.
    from pycut import config
    for _ in range(config.LLM_MAX_CONSEC_FAIL):
        with mock.patch.object(client,'_post',return_value=(-1,'timeout')), mock.patch.object(llm.time,'monotonic',side_effect=[0,0,2]):
            assert client.ask_vision(frame,'subtitle',deadline=1) is None
    assert client.failures==config.LLM_MAX_CONSEC_FAIL
    assert not client.enabled
    with mock.patch.object(client,'_post') as post:
        assert client.ask_vision(frame,'subtitle') is None
        post.assert_not_called()


def test_gateway_disabled_does_not_skip_unverified_subtitle_output():
    from types import SimpleNamespace
    from pycut.core import slice_pipeline
    info=SimpleNamespace(stem='clip',path=Path('source.mp4'),duration=4)
    model=SimpleNamespace(enabled=False)
    opts=slice_pipeline.SliceOptions(no_sub=False)
    with mock.patch.object(slice_pipeline,'detect_scenes',return_value=[]),mock.patch.object(slice_pipeline,'video_done',return_value=True) as resume:
        slice_pipeline.process_video(model,info,opts,mock.Mock())
    assert resume.call_args.kwargs['require_sub'] is True
