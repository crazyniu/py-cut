"""合成素材验证前贴视频、贴纸、音轨与时长，不访问在线素材。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import cv2
import numpy as np
from pycut.core.montage_intro import prepend
from pycut.core.dedup import _video_duration


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 FFmpeg')
class IntroTests(unittest.TestCase):
    def test_prefix_audio_and_silent_modes_preserve_duration_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root/'prefixes'
            folder.mkdir()
            source = folder/'prefix.mp4'
            # 前贴无声，正文有声：必须给前贴补静音，正文音轨继续保留。
            subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i',
                            'testsrc2=size=180x320:rate=30:duration=1','-c:v','libx264',
                            '-pix_fmt','yuv420p',str(source)], check=True)
            original = source.read_bytes()
            sticker = root/'sticker.png'
            cv2.imwrite(str(sticker),np.full((60,60,3),(20,20,255),np.uint8))
            for keep_audio in (True, False):
                video = root/f'main_{keep_audio}.mp4'
                subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i',
                                'testsrc2=size=180x320:rate=30:duration=1',
                                '-f','lavfi','-i','sine=frequency=440:duration=1',
                                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',
                                '-shortest',str(video)], check=True)
                report = prepend(video,folder,seed=17,canvas=(180,320),stickers=[sticker],crf='20',keep_audio=keep_audio)
                self.assertTrue(report['sticker_applied'])
                self.assertAlmostEqual(_video_duration(video),2,delta=.07)
                probe=subprocess.check_output(['ffprobe','-v','error','-select_streams','a',
                                                '-show_entries','stream=index','-of','csv=p=0',str(video)])
                self.assertEqual(bool(probe.strip()), keep_audio)
                self.assertEqual(source.read_bytes(), original)

    def test_empty_intro_refuses_to_modify_main(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);video=root/'main.mp4';video.write_bytes(b'original')
            folder=root/'empty';folder.mkdir()
            with self.assertRaises(RuntimeError):
                prepend(video,folder,seed=1,canvas=(180,320),stickers=[],crf='20',keep_audio=False)
            self.assertEqual(video.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
