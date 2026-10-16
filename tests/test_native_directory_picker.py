"""Native adapter argument boundaries, cancellation and authenticated API."""
import base64
import json
import subprocess
import urllib.error
import urllib.request
from unittest import mock

import pytest
from pycut import native_directory_picker as picker, web_config, config


@pytest.mark.parametrize('platform',['darwin','win32','linux'])
def test_native_arguments_and_unicode_paths_are_not_shell_code(tmp_path,platform):
    directory=tmp_path/'中文 空格; $(echo fake)'
    directory.mkdir()
    result=subprocess.CompletedProcess([],0,str(directory)+'\n','')
    with mock.patch.object(picker.sys,'platform',platform),mock.patch.object(picker.shutil,'which',return_value='/usr/bin/zenity'),mock.patch.object(picker.subprocess,'run',return_value=result) as run:
        assert picker.choose_directory(str(directory),tmp_path,'选择目录')==str(directory)
    command=run.call_args.args[0]
    assert isinstance(command,list)
    assert run.call_args.kwargs.get('shell') is not True
    assert run.call_args.kwargs['timeout']==120
    assert run.call_args.kwargs['env']['PYCUT_PICKER_START']==str(directory)
    if platform=='darwin': assert command[-1]==str(directory)
    elif platform=='win32':
        assert '-EncodedCommand' in command
        script=base64.b64decode(command[-1]).decode('utf-16le')
        assert str(directory) not in script and '$env:PYCUT_PICKER_START' in script
    else: assert '--filename='+str(directory)+'/' in command


def test_cancel_timeout_and_headless_errors_are_explicit(tmp_path):
    with mock.patch.object(picker.sys,'platform','darwin'),mock.patch.object(picker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'\n','')):
        assert picker.choose_directory('',tmp_path,'选择') is None
    with mock.patch.object(picker.sys,'platform','linux'),mock.patch.object(picker.shutil,'which',return_value='zenity'),mock.patch.object(picker.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','')):
        assert picker.choose_directory('',tmp_path,'选择') is None
    with mock.patch.object(picker.sys,'platform','darwin'),mock.patch.object(picker.subprocess,'run',side_effect=subprocess.TimeoutExpired('picker',120)):
        with pytest.raises(picker.PickerError,match='超时'): picker.choose_directory('',tmp_path,'选择')
    with mock.patch.object(picker.sys,'platform','linux'),mock.patch.object(picker.shutil,'which',return_value=None),mock.patch.dict('os.environ',{},clear=True):
        with pytest.raises(picker.PickerError,match='手动输入'): picker.choose_directory('',tmp_path,'选择')
    with mock.patch.object(picker.sys,'platform','linux'),mock.patch.object(picker.shutil,'which',return_value=None),mock.patch.dict('os.environ',{'DISPLAY':':1'}),mock.patch.object(picker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,str(tmp_path),'')) as run:
        assert picker.choose_directory('',tmp_path,'选择')==str(tmp_path)
        assert run.call_args.args[0][1]=='-c'


def test_native_endpoint_requires_session_and_origin_before_opening(tmp_path):
    web_config.stop()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with mock.patch.object(config,'PROJECT_ROOT',tmp_path),mock.patch.object(picker,'choose_directory',return_value=str(tmp_path)) as choose:
        url=web_config.start()
        def post(data,headers):
            return opener.open(urllib.request.Request(url+'api/pick-directory',json.dumps(data).encode(),{'Content-Type':'application/json',**headers},method='POST'),timeout=3)
        try:
            values={'key':'PYCUT_CLIP_DIR','path':str(tmp_path)}
            for headers in ({},{'X-Config-Token':web_config._TOKEN,'Origin':'https://example.com'}):
                with pytest.raises(urllib.error.HTTPError) as error: post(values,headers)
                assert error.value.code==403
            choose.assert_not_called()
            auth={'X-Config-Token':web_config._TOKEN}
            with post(values,auth) as response: assert json.loads(response.read())['path']==str(tmp_path)
            choose.return_value=None
            with post(values,auth) as response: assert json.loads(response.read())=={'path':None,'cancelled':True}
            choose.side_effect=picker.PickerError('当前没有图形桌面，请手动输入路径')
            with pytest.raises(urllib.error.HTTPError) as error: post(values,auth)
            assert error.value.code==503 and '手动输入' in json.loads(error.value.read())['error']
            with pytest.raises(urllib.error.HTTPError) as error: post({'key':'PYCUT_MAIN_KEY','path':''},auth)
            assert error.value.code==400
        finally: web_config.stop()


def test_only_one_native_window_can_be_open(tmp_path):
    picker._LOCK.acquire()
    try:
        with pytest.raises(picker.PickerError,match='已有文件夹选择窗口'):
            picker.choose_directory('',tmp_path,'选择')
    finally: picker._LOCK.release()
