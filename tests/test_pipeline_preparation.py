"""同批裂变准备复用、提前失败和实际参数传递。"""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from pycut import cli, config
from pycut.commands import montage_cmd, montage_pipeline
from pycut.core import montage_source, montage_clips, montage_pip, online_stickers


class PreparationTests(unittest.TestCase):
    def test_fission_prepares_once_and_cleans_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root/'source.mp4';source.touch()
            args = cli.build_parser().parse_args(['montage','--source',str(source),
                '--no-sub','--no-pip','--step','b','--fission','3','--noise-db','-18',
                '--min-silence','0.3','--out-dir',str(root/'out')])
            profile = SimpleNamespace(duration=2,width=720,height=1280,segments=[],speech_total=0,has_audio=False)
            downloads = []
            def download(directory):
                downloads.append(directory)
                return [directory/'sticker.png']
            def concat(items, target, **kwargs):
                target.write_bytes(b'video'*300)
                return {'ok':True}
            with mock.patch.object(config,'MONTAGE_TMP',root/'tmp'), \
                 mock.patch.object(montage_cmd,'install_signal_handlers'), \
                 mock.patch.object(montage_cmd,'self_check'), \
                 mock.patch.object(montage_source,'probe_source',return_value=profile) as probe, \
                 mock.patch.object(montage_clips,'scan_library',return_value=[object()]) as scan, \
                 mock.patch.object(montage_clips,'pick_clips',side_effect=lambda *a,**k:[{'item':object(),'take':2}]), \
                 mock.patch.object(montage_clips,'concat_clips',side_effect=concat) as encode, \
                 mock.patch.object(montage_pipeline,'_apply_variation'), \
                 mock.patch.object(montage_pipeline,'_duration',return_value=2), \
                 mock.patch.object(online_stickers,'download_stickers',side_effect=download) as fetch:
                self.assertEqual(montage_cmd.run(args),0)
            probe.assert_called_once_with(source,noise_db=-18,min_silence=.3)
            self.assertEqual((scan.call_count,fetch.call_count,encode.call_count),(1,1,3))
            self.assertFalse(downloads[0].exists())

    def test_missing_intro_stops_before_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source.mp4';source.touch()
            args=cli.build_parser().parse_args(['montage','--source',str(source),
                '--intro-dir',str(Path(td)/'empty'),'--no-sub'])
            with mock.patch.object(montage_cmd,'self_check'), \
                 mock.patch.object(montage_cmd,'install_signal_handlers'), \
                 mock.patch.object(montage_pipeline,'run_pipeline') as pipeline:
                self.assertEqual(montage_cmd.run(args),2)
            pipeline.assert_not_called()

    def test_pip_probes_concurrently_without_mixing_duration_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            values={'a.mp4':2.0,'b.mp4':4.0,'c.mp4':3.0}
            for name in reversed(values):
                (root/name).write_bytes(name.encode())
            barrier=threading.Barrier(3)
            def probe(path):
                barrier.wait(timeout=2)
                return values[path.name]
            with mock.patch.object(config,'MONTAGE_WORKERS',3), \
                 mock.patch.object(montage_pip,'_probe_duration',side_effect=probe):
                pool=montage_pip.scan_dedup_pool(root,min_dur=1)
            self.assertEqual([(item.path.name,item.duration) for item in pool],list(values.items()))

    def test_dotenv_media_tools_loaded_before_constants(self):
        code = "from unittest import mock; from pycut import settings; p=mock.patch.object(settings, 'read_env', return_value={'PYCUT_FFMPEG':'/custom/ffmpeg','PYCUT_FFPROBE':'/custom/ffprobe'}); p.start(); from pycut import config; print(config.FFMPEG); print(config.FFPROBE)"
        env={k:v for k,v in os.environ.items() if k not in ('PYCUT_FFMPEG','PYCUT_FFPROBE')}
        out=subprocess.check_output([sys.executable,'-c',code],env=env,text=True)
        self.assertEqual(out.splitlines(),['/custom/ffmpeg','/custom/ffprobe'])


if __name__ == '__main__':
    unittest.main()
