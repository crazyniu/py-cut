"""Retain segment-encoding root causes and preserve existing final output."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import cv2

from pycut import config
from pycut.core import montage_clips as clips
from pycut.core.utils import run_cmd


class SegmentErrors(unittest.TestCase):
    def test_first_failure_reports_original_error_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / '成片.mp4'
            out.write_bytes(b'previous-video')
            item = clips.ClipItem(root / '视频 (1).mp4', 3, 180, 320, False)
            rows = [{'item': item, 'offset': .25, 'take': 1.2}] * 5
            with mock.patch.object(config, 'MONTAGE_TMP', root / 'temp'), \
                 mock.patch.object(clips, 'log'), \
                 mock.patch.object(clips, 'run_cmd', return_value=(1, '',
                     'No such filter: broken\nError opening output files')) as execute:
                result = clips.concat_clips(rows, out)
            self.assertFalse(result['ok'])
            self.assertIn('No such filter: broken', result['error'])
            self.assertIn('视频 (1).mp4', result['error'])
            self.assertIn('分段 1/5', result['error'])
            self.assertIn('offset=0.250s', result['error'])
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(out.read_bytes(), b'previous-video')
            self.assertFalse(list((root / 'temp').glob('seg_*')))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'requires FFmpeg')
    def test_real_bad_source_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / '损坏.mp4'
            bad.write_bytes(b'not a movie')
            out = root / 'out.mp4'
            out.write_bytes(b'previous-video')
            row = {'item': clips.ClipItem(bad, 2, 180, 320, False),
                   'offset': 0., 'take': 1.2}
            with mock.patch.object(config, 'MONTAGE_TMP', root / 'temp'):
                result = clips.concat_clips([row], out)
            self.assertFalse(result['ok'])
            self.assertIn('moov atom not found', result['error'])
            self.assertEqual(out.read_bytes(), b'previous-video')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'requires FFmpeg')
    def test_real_mixed_sizes_speed_seek_and_frame_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for index, size in enumerate(('180x320', '320x180')):
                source = root / f'素材 ({index}).mp4'
                rc, _, err = run_cmd([clips.FFMPEG, '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30',
                    '-t', '2', '-c:v', 'libx264', str(source)], 30)
                self.assertEqual(rc, 0, err)
                rows.append({'item': clips.ClipItem(source, 2, 180, 320, False),
                    'offset': .2, 'take': .7, 'speed': 1.2,
                    'mirror': bool(index), 'dedup_seed': index})
            with mock.patch.object(config, 'MONTAGE_TMP', root / 'temp'):
                result = clips.concat_clips(rows, root / 'out.mp4',
                    canvas=(180, 320), fps=30, seam_fade=.1)
            self.assertTrue(result['ok'], result['error'])
            capture = cv2.VideoCapture(str(root / 'out.mp4'))
            frames = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                self.assertEqual(frame.shape[:2], (320, 180))
                frames += 1
            capture.release()
            self.assertEqual(frames, 42)
            self.assertAlmostEqual(result['duration'], 1.4, delta=1/30)


if __name__ == '__main__':
    unittest.main()
