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

    def test_empty_intro_skips_without_modifying_main(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);video=root/'main.mp4';video.write_bytes(b'original')
            folder=root/'empty';folder.mkdir()
            self.assertIsNone(prepend(video,folder,seed=1,canvas=(180,320),stickers=[],crf='20',keep_audio=False))
            self.assertEqual(video.read_bytes(), b'original')


    def test_compatible_video_stream_is_copied_without_reencoding_body(self):
        from unittest import mock
        from pycut.core import montage_intro
        from pycut.core.utils import run_cmd
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            folder=root/'prefixes';folder.mkdir()
            source=folder/'prefix.mp4'
            main=root/'main.mp4'
            for path,seconds in ((source,1),(main,2)):
                subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i',
                                f'testsrc2=size=180x320:rate=30:duration={seconds}',
                                '-an','-c:v','libx264','-preset','veryfast','-crf','20',
                                '-pix_fmt','yuv420p','-r','30','-fps_mode','cfr',str(path)],check=True)
            before=cv2.VideoCapture(str(main))
            original_frames=[]
            while True:
                ok,frame=before.read()
                if not ok:break
                original_frames.append(frame)
            before.release()
            with mock.patch.object(montage_intro,'run_cmd',wraps=run_cmd) as calls:
                report=prepend(main,folder,seed=45,canvas=(180,320),
                               stickers=[],crf='20',keep_audio=False)
            assert report is not None
            cmd_calls=[call.args[0] for call in calls.call_args_list]
            self.assertTrue(any('-f' in cmd and 'concat' in cmd and
                                '-c:v' in cmd and cmd[cmd.index('-c:v')+1]=='copy'
                                for cmd in cmd_calls))
            self.assertFalse(any('-c:v' in cmd and cmd[cmd.index('-c:v')+1]=='libx264'
                                 for cmd in cmd_calls), 'video stream copy should not fallback')
            self.assertAlmostEqual(_video_duration(main),3,delta=.07)
            merged=cv2.VideoCapture(str(main))
            merged_frames=[]
            while True:
                ok,frame=merged.read()
                if not ok:break
                merged_frames.append(frame)
            merged.release()
            self.assertGreaterEqual(len(merged_frames),len(original_frames))
            for a,b in zip(merged_frames[-len(original_frames):],original_frames):
                self.assertTrue(np.array_equal(a,b))


if __name__ == '__main__':
    unittest.main()
