"""Real FFmpeg coverage/audio checks, failed validation, and interruption handling."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import cv2
import numpy as np
from pycut.core import subtitle
from pycut.core.montage_clips import _probe_one


class NativeMosaicTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), '需要 FFmpeg')
    def test_rectangle_audio_and_duration_on_real_video(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            source=root/'source.mp4'
            frame=np.random.default_rng(9).integers(0,256,(320,180,3),dtype=np.uint8)
            cv2.putText(frame,'SUBTITLE',(10,263),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
            subprocess.run(['ffmpeg','-y','-loglevel','error','-f','rawvideo','-pix_fmt',
                'bgr24','-s','180x320','-r','30','-i','pipe:0','-f','lavfi','-i',
                'sine=frequency=440:duration=1','-t','1','-c:v','libx264','-crf','0',
                '-pix_fmt','yuv420p','-c:a','aac',str(source)],input=frame.tobytes()*30,check=True)
            def read(path):
                cap=cv2.VideoCapture(str(path));ok,frame=cap.read();cap.release()
                self.assertTrue(ok)
                return frame
            before=read(source)
            clip=root/'covered.mp4';shutil.copyfile(source,clip)
            self.assertTrue(subtitle.cover_clip(clip,(0,238,180,48)))
            after=read(clip)
            old, new=_probe_one(source),_probe_one(clip)
            self.assertEqual((new.width,new.height,new.has_audio),(180,320,True))
            self.assertAlmostEqual(old.duration,new.duration,delta=.04)
            outside=np.concatenate([np.abs(after[:238].astype(float)-before[:238]),
                                    np.abs(after[286:].astype(float)-before[286:])])
            self.assertLess(outside.mean(),8)
            for x in (0,168):
                self.assertGreater(np.abs(after[238:286,x:x+12].astype(float)-before[238:286,x:x+12]).mean(),15)
            # One continuous Gaussian-blur region, with one overlay and no pixelation.
            self.assertLess(after[242:282].astype(float).std(axis=(0,1)).max(),15)
            def audio_hash(path):
                return subprocess.check_output(['ffmpeg','-v','error','-i',str(path),
                    '-map','0:a:0','-c:a','copy','-f','hash','-hash','md5','-'])
            self.assertEqual(audio_hash(source),audio_hash(clip))

    def test_mismatched_output_and_cancel_never_replace_original(self):
        with tempfile.TemporaryDirectory() as td:
            clip=Path(td)/'video.mp4';clip.write_bytes(b'original')
            tmp=clip.with_name('video.tmp_cover.mp4')
            info=SimpleNamespace(width=180,height=320,duration=1,has_audio=True)
            broken=SimpleNamespace(width=180,height=320,duration=.2,has_audio=False)
            child=mock.Mock(returncode=0);child.poll.return_value=0
            def launch(*a,**k):
                tmp.write_bytes(b'partial'*300)
                return child
            with mock.patch('pycut.core.montage_clips._probe_one',side_effect=[info,broken]), \
                 mock.patch.object(subtitle.subprocess,'Popen',side_effect=launch):
                self.assertFalse(subtitle.cover_clip(clip,(0,238,180,48)))
            self.assertEqual(clip.read_bytes(),b'original')
            self.assertFalse(tmp.exists())
            child=mock.Mock();child.poll.return_value=None
            def cancel(*a,**k):
                subtitle.STOP.flag=True
            child.wait.side_effect=cancel
            with mock.patch.object(subtitle.STOP,'flag',False), \
                 mock.patch('pycut.core.montage_clips._probe_one',return_value=info), \
                 mock.patch.object(subtitle.subprocess,'Popen',return_value=child):
                self.assertFalse(subtitle.cover_clip(clip,(0,238,180,48)))
            child.terminate.assert_called_once()
            self.assertEqual(clip.read_bytes(),b'original')

    def test_tiny_positive_subtitle_is_not_marked_as_no_subtitle(self):
        model=SimpleNamespace(ask_vision=lambda *a, **kw:{'frames':[{'frame':i,'has_subtitle':True,'box':[.2,.5,.7,.501]} for i in range(5)]})
        values={'format=duration':'1','stream=width':'180','stream=height':'320'}
        with mock.patch.object(subtitle,'probe_scalar',side_effect=lambda p,k:values[k]), \
             mock.patch.object(subtitle,'grab_frames',return_value=[Path(f'{i}.jpg') for i in range(5)]), \
             mock.patch.object(subtitle.cv2,'imread',return_value=np.zeros((320,180,3),np.uint8)):
            with self.assertRaisesRegex(RuntimeError,'未标记无字幕'):
                subtitle.detect_region(model,Path('video.mp4'))


if __name__ == '__main__':
    unittest.main()
