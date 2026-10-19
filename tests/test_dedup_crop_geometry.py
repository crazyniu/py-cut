"""Verify FFmpeg scale/crop integer boundaries using the real encoder."""
from pathlib import Path
import shutil
import tempfile
import unittest

import cv2

from pycut.core import dedup, montage_clips
from pycut.core.utils import run_cmd


@unittest.skipUnless(shutil.which('ffmpeg'), 'requires FFmpeg')
class CropGeometry(unittest.TestCase):
    def test_seed_87_preserves_crop_and_full_frame_budget_for_source_formats(self):
        # 702*1.0138=711.6876: FFmpeg scale truncates to 711, while round incorrectly predicts 712.
        params, chain = dedup.montage_variation(720, 1280, 87)
        self.assertEqual(params['zoom'], 1.0138)
        self.assertIn('crop=702:1270:', chain)
        self.assertIn('crop=710:1272:', chain)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, size, sar, rotate in (
                ('small', '180x320', '1', False),
                ('540p', '540x960', '1', False),
                ('sar', '180x320', '2', False),
                ('rotation', '320x180', '1', True),
            ):
                with self.subTest(source=name):
                    src = root / f'{name}.mp4'
                    rc, _, error = run_cmd([
                        montage_clips.FFMPEG, '-v', 'error', '-y',
                        '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30',
                        '-vf', f'setsar={sar}', '-t', '3.2',
                        '-c:v', 'libx264', str(src)], 30)
                    self.assertEqual(rc, 0, error)
                    if rotate:
                        rotated = root / 'rotated.mp4'
                        rc, _, error = run_cmd([
                            montage_clips.FFMPEG, '-v', 'error', '-y',
                            '-i', str(src), '-c', 'copy',
                            '-metadata:s:v:0', 'rotate=90', str(rotated)], 30)
                        self.assertEqual(rc, 0, error)
                        src = rotated
                    out = root / f'{name}-out.mp4'
                    command = montage_clips._seg_cmd(
                        src, .2, 2.801, out, (720, 1280), 30,
                        speed=1.218, mirror=True, frames=69, dedup_seed=87)
                    rc, _, error = run_cmd(command, 30)
                    self.assertEqual(rc, 0, error)
                    cap = cv2.VideoCapture(str(out))
                    count = 0
                    while True:
                        ok, frame = cap.read()
                        if not ok:
                            break
                        self.assertEqual(frame.shape[:2], (1280, 720))
                        count += 1
                    cap.release()
                    self.assertEqual(count, 69)
                    self.assertAlmostEqual(montage_clips._probe_duration(out),
                                           2.3, delta=1/30)


if __name__ == '__main__':
    unittest.main()
