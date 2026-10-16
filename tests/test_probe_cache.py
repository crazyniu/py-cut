"""Only valid, unchanged-file metadata is reusable across commands/threads."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
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


def test_cache_is_reused_by_new_process(fixture):
    import subprocess
    import sys
    source,tool,args=fixture
    cache.cached_probe(source,args,30,lambda *a,**k:(0,PAYLOAD,''))
    code='''import sys
from pathlib import Path
from pycut import config
from pycut.core.probe_cache import cached_probe
config.MONTAGE_TMP=Path(sys.argv[1])
def forbidden(*args,**kwargs): raise AssertionError('warm cache launched ffprobe')
result=cached_probe(Path(sys.argv[2]),[sys.argv[3],'-of','json',sys.argv[2]],30,forbidden)
assert result[0]==0
'''
    result=subprocess.run([sys.executable,'-c',code,str(config.MONTAGE_TMP),str(source),str(tool)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_same_size_edit_with_preserved_mtime_is_invalidated_by_ctime(fixture):
    import os
    source,tool,args=fixture
    run=mock.Mock(return_value=(0,PAYLOAD,''))
    cache.cached_probe(source,args,30,run)
    before=source.stat()
    source.write_bytes(b'other')
    os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert source.stat().st_size==before.st_size
    cache.cached_probe(source,args,30,run)
    assert run.call_count==2


def test_library_discovers_added_removed_and_modified_files_and_shares_pip_cache(tmp_path,monkeypatch):
    from pycut.core import montage_clips as clips, montage_pip as pip
    library=tmp_path/'library';library.mkdir()
    first=library/'a.mp4';first.write_bytes(b'first')
    second=library/'b.mp4';second.write_bytes(b'second video')
    tool=tmp_path/'probe';tool.write_bytes(b'binary')
    monkeypatch.setattr(config,'MONTAGE_TMP',tmp_path/'cache')
    monkeypatch.setattr(clips,'FFPROBE',str(tool))
    run=mock.Mock(return_value=(0,PAYLOAD,''))
    monkeypatch.setattr(clips,'run_cmd',run)
    paths=lambda items:{item.path for item in items}
    assert paths(clips.scan_library(library))=={first,second}
    assert run.call_count==2
    first.unlink()
    third=library/'c.mp4';third.write_bytes(b'third content')
    assert paths(clips.scan_library(library))=={second,third}
    assert run.call_count==3  # only the newly discovered file
    second.write_bytes(b'modified')
    assert paths(clips.scan_library(library))=={second,third}
    assert run.call_count==4
    assert paths(pip.scan_dedup_pool(library))=={second,third}
    assert run.call_count==4  # no second ffprobe pass for PIP
