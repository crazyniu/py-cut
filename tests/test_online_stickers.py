"""Network recovery preserves the complete validated, temporary sticker pool."""
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError

import cv2
import numpy as np

from pycut.core import online_stickers as stickers
from pycut.core import montage_clips, montage_source
from pycut.commands import montage_pipeline
from pycut import config
from types import SimpleNamespace


class StickerDownloadTests(unittest.TestCase):
    def setUp(self):
        ok, png = cv2.imencode('.png', np.zeros((12, 12, 4), dtype=np.uint8))
        self.assertTrue(ok)
        self.png = png.tobytes()

    def response(self, data=None):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = self.png if data is None else data
        return response

    def test_mirror_recovers_network_failure_and_records_actual_source(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                stickers.urllib.request, 'urlopen', side_effect=[
                    URLError('connection reset'), self.response(),
                    self.response(), self.response()]) as fetch:
            root = Path(directory)
            files = stickers.download_stickers(root)
            self.assertEqual(len(files), 3)
            mirror = fetch.call_args_list[1].args[0].full_url
            self.assertIn('cdn.jsdelivr.net/gh/twitter/twemoji@v14.0.2/', mirror)
            self.assertEqual(json.loads((root/'credits.json').read_text())['sources'][0], mirror)
            self.assertTrue(all(file.read_bytes() == self.png for file in files))
            self.assertTrue(all(call.kwargs['timeout'] == 15 for call in fetch.call_args_list))

    def test_second_round_recovers_after_both_hosts_fail(self):
        with mock.patch.object(stickers.urllib.request, 'urlopen', side_effect=[
                TimeoutError('timed out'), http.client.IncompleteRead(b'partial'),
                self.response()]) as fetch:
            data, source = stickers._download(stickers.SOURCES[0])
            self.assertEqual(data, self.png)
            self.assertEqual(source, stickers.SOURCES[0])
            self.assertEqual(fetch.call_count, 3)

    def test_invalid_response_uses_valid_mirror(self):
        with mock.patch.object(stickers.urllib.request, 'urlopen', side_effect=[
                self.response(b'<html>error</html>'), self.response()]):
            self.assertEqual(stickers._download(stickers.SOURCES[0])[0], self.png)

    def test_exhaustion_reports_hosts_and_reason_without_publishing_image(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                stickers.urllib.request, 'urlopen', side_effect=URLError('timed out')) as fetch:
            with self.assertRaisesRegex(ValueError, 'raw.githubusercontent.com.*timed out.*cdn.jsdelivr.net'):
                stickers.download_stickers(Path(directory))
            self.assertEqual(fetch.call_count, 4)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancellation_stops_before_request_and_after_read(self):
        for checks in ([InterruptedError('stop')], [None, InterruptedError('stop')]):
            with self.subTest(checks=checks), mock.patch.object(
                    stickers, 'check_stop', side_effect=checks), mock.patch.object(
                    stickers.urllib.request, 'urlopen', return_value=self.response()) as fetch:
                with self.assertRaises(InterruptedError):
                    stickers._download(stickers.SOURCES[0])
                self.assertEqual(fetch.call_count, len(checks) - 1)

    def test_pipeline_keeps_diagnostic_and_cleans_failed_downloads(self):
        profile = SimpleNamespace(duration=6, width=720, height=1280,
                                  segments=[], speech_total=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail(destination):
                (destination/'0.png').write_bytes(self.png)
                raise ValueError('cdn.jsdelivr.net: timed out')
            with mock.patch.object(config, 'MONTAGE_TMP', root/'tmp'), mock.patch.object(
                    montage_source, 'probe_source', return_value=profile), mock.patch.object(
                    montage_source, 'plan_clip_slots', return_value=[(0, 6)]), mock.patch.object(
                    stickers, 'download_stickers', side_effect=fail), mock.patch.object(
                    montage_clips, 'concat_clips') as encode:
                result = montage_pipeline.run_pipeline(montage_pipeline.MontageOptions(
                    source=root/'source.mp4', out_dir=root, srt='', keep_audio=False,
                    online_stickers=True, sticker_chance=1))
            self.assertIn('cdn.jsdelivr.net: timed out', result['error'])
            self.assertEqual(result['final'], '')
            encode.assert_not_called()
            self.assertFalse(list(root.rglob('*.png')))
