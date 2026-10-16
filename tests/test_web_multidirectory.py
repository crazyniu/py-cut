"""Multi-directory Web groups preserve the string API and local access rules."""
import json
from pathlib import Path
from unittest import mock
import urllib.error
import urllib.request

import pytest
from pycut import config, settings, web_config


def test_multi_groups_keep_named_string_values_and_remove_source():
    page=web_config.page()
    assert 'PYCUT_SOURCE_DIR' not in page
    assert '话术目录' in page and '同名 SRT' in page
    for key in settings.MULTI_DIRECTORIES:
        assert f'<fieldset class="field multi-directory" data-key="{key}"' in page
        assert f'<input id="{key}" name="{key}" type="hidden">' in page
        assert f'class="directory-list" data-key="{key}"' in page
        assert f'class="add-directory" data-key="{key}"' in page
    assert 'MULTI_FIELDS' not in page
    assert 'picking||loading||saving' in page
    assert 'input.isConnected' in page and 'input.value!==original' in page
    assert 'browse-modal' not in page
    assert '/api/pick-directory' in page


def test_multi_directory_post_returns_canonical_paths_and_no_keys(tmp_path):
    first=tmp_path/'中文 空格'
    second=tmp_path/'第二目录'
    first.mkdir();second.mkdir()
    (tmp_path/'.env').write_text('PYCUT_MAIN_KEY=private-value\n',encoding='utf8')
    web_config.stop()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with mock.patch.object(config,'PROJECT_ROOT',tmp_path),mock.patch.object(config,'refresh'),mock.patch.dict('os.environ',{},clear=True):
        url=web_config.start()
        headers={'Content-Type':'application/json','X-Config-Token':web_config._TOKEN}
        def post(values,authorized=True):
            return opener.open(urllib.request.Request(url+'api/config',json.dumps(values).encode(),headers if authorized else {'Content-Type':'application/json'},method='POST'),timeout=3)
        try:
            values={'PYCUT_REFERENCE_DIR':str(first),'PYCUT_CLIP_DIR':json.dumps([str(first),str(second),str(first)]),'PYCUT_PIP_DIR':'','PYCUT_INTRO_DIR':'','PYCUT_MAIN_KEY':''}
            with post(values) as response: saved=json.loads(response.read())
            assert saved['problems']==[]
            assert json.loads(saved['values']['PYCUT_CLIP_DIR'])==[str(first),str(second)]
            assert saved['values']['PYCUT_MAIN_KEY']==''
            assert 'private-value' not in json.dumps(saved)
            assert settings.read_env()['PYCUT_MAIN_KEY']=='private-value'
            before=(tmp_path/'.env').read_bytes()
            with pytest.raises(urllib.error.HTTPError) as error:
                post({'PYCUT_CLIP_DIR':json.dumps([str(first),str(tmp_path/'missing')])})
            assert error.value.code==400
            assert '第 2 项' in json.loads(error.value.read())['problems'][0]
            assert (tmp_path/'.env').read_bytes()==before
            with pytest.raises(urllib.error.HTTPError) as error: post({'PYCUT_CLIP_DIR':'[broken'})
            assert error.value.code==400
            with pytest.raises(urllib.error.HTTPError) as error: post({},authorized=False)
            assert error.value.code==403
        finally: web_config.stop()
