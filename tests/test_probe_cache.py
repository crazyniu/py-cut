"""Only valid, unchanged-file metadata is reusable across commands/threads."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
import sqlite3
import pytest
from pycut import config
from pycut.core import probe_cache as cache

PAYLOAD=json.dumps({'streams':[{'codec_type':'video','width':720,'height':1280,'duration':'3'}]})


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    source=tmp_path/'clip.mp4';source.write_bytes(b'video')
    tool=tmp_path/'ffprobe';tool.write_bytes(b'probe binary')
    monkeypatch.setattr(config,'MONTAGE_TMP',tmp_path/'cache')
    return source,tool,[str(tool),'-of','json',str(source)]


def test_reuses_success_and_invalidates_file_tool_and_query(fixture):
    source,tool,args=fixture
    run=mock.Mock(return_value=(0,PAYLOAD,''))
    assert cache.cached_probe(source,args,30,run)==(0,PAYLOAD,'')
    assert cache.cached_probe(source,args,30,run)==(0,PAYLOAD,'')
    assert run.call_count==1
    source.write_bytes(b'changed video')
    cache.cached_probe(source,args,30,run)
    assert run.call_count==2
    tool.write_bytes(b'new binary')
    cache.cached_probe(source,args,30,run)
    assert run.call_count==3
    cache.cached_probe(source,args[:-1]+['-extra',str(source)],30,run)
    assert run.call_count==4


@pytest.mark.parametrize('result', [(1,'','failed'),(0,'not JSON',''),(0,'{}',''),(0,json.dumps({'streams':[{'codec_type':'video','width':720,'height':1280,'duration':'nan'}]}),'')])
def test_failed_or_invalid_probes_are_not_cached(fixture,result):
    source,tool,args=fixture
    run=mock.Mock(return_value=result)
    for _ in range(2):assert cache.cached_probe(source,args,30,run)==result
    assert run.call_count==2


def test_corrupt_or_unwritable_cache_does_not_fail_probe(fixture):
    source,tool,args=fixture
    database=config.MONTAGE_TMP/'probe-cache-v1.sqlite3'
    database.parent.mkdir();database.write_bytes(b'broken database')
    run=mock.Mock(return_value=(0,PAYLOAD,''))
    assert cache.cached_probe(source,args,30,run)==(0,PAYLOAD,'')
    assert run.call_count==1


def test_file_changed_during_probe_is_not_saved(fixture):
    source,tool,args=fixture
    def changing(*args,**kwargs):
        source.write_bytes(source.read_bytes()+b'new')
        return 0,PAYLOAD,''
    run=mock.Mock(side_effect=changing)
    for _ in range(2):cache.cached_probe(source,args,30,run)
    assert run.call_count==2


def test_parallel_reads_and_writes_then_warm_read(fixture):
    source,tool,args=fixture
    run=mock.Mock(return_value=(0,PAYLOAD,''))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:cache.cached_probe(source,args,30,run),range(12)))
    assert all(result==(0,PAYLOAD,'') for result in results)
    calls=run.call_count
    for _ in range(3):cache.cached_probe(source,args,30,run)
    assert run.call_count==calls
