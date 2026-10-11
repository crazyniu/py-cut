"""真实 HTTP/.env 与矩形马赛克的回归，不调用外部 LLM。"""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
import numpy as np
from pycut import config, settings, web_config, tui
from pycut.core import subtitle


class ConfigTests(unittest.TestCase):
    def test_save_preserves_comments_other_values_and_blank_secret(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, 'PROJECT_ROOT', Path(td)):
            path = Path(td) / '.env'
            path.write_text('# keep\nOTHER=value\nPYCUT_MAIN_KEY=original\n', encoding='utf-8')
            folder = Path(td) / '中文 空格'
            folder.mkdir()
            settings.save({'PYCUT_CLIP_DIR':str(folder), 'PYCUT_MAIN_KEY':''})
            self.assertEqual(settings.read_env()['PYCUT_MAIN_KEY'], 'original')
            self.assertEqual(settings.read_env()['PYCUT_CLIP_DIR'], str(folder))
            self.assertIn('# keep\nOTHER=value', path.read_text())
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                settings.save({'PYCUT_MAIN_KEY':'key\nOTHER=changed'})
            self.assertEqual(before, path.read_bytes())

    def test_reload_reads_manual_change_and_deletion(self):
        key = 'PYCUT_CLIP_DIR'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, 'PROJECT_ROOT', Path(td)), mock.patch.dict('os.environ', {}, clear=True):
            path = Path(td) / '.env'
            path.write_text(f'{key}=first\n')
            config._load_dotenv()
            self.assertEqual(settings.effective()[key], 'first')
            path.write_text(f'{key}=second\n')
            config._load_dotenv()
            self.assertEqual(settings.effective()[key], 'second')
            path.write_text('')
            config._load_dotenv()
            self.assertEqual(settings.effective()[key], '')

    def test_real_http_requires_token_and_never_returns_keys(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, 'PROJECT_ROOT', Path(td)), mock.patch.object(config, 'refresh'), mock.patch.dict('os.environ', {}, clear=True):
            Path(td, '.env').write_text('PYCUT_MAIN_KEY=test-secret\n')
            url = web_config.start(open_browser=False)
            self.addCleanup(web_config.stop)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + 'api/config')
            self.assertEqual(error.exception.code, 403)
            headers = {'X-Config-Token':web_config._TOKEN, 'Content-Type':'application/json'}
            req = urllib.request.Request(url+'api/config', headers=headers)
            with urllib.request.urlopen(req) as response:
                raw = response.read()
            self.assertNotIn(b'test-secret', raw)
            payload = {'PYCUT_CLIP_DIR':td, 'PYCUT_MAIN_KEY':''}
            req = urllib.request.Request(url+'api/config', json.dumps(payload).encode(), headers, method='POST')
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(settings.read_env()['PYCUT_MAIN_KEY'], 'test-secret')
            headers['Origin'] = 'https://example.com'
            req = urllib.request.Request(url+'api/config', b'{}', headers, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code, 403)

    def test_missing_configuration_never_enters_processing(self):
        with mock.patch.object(config, 'refresh'), mock.patch.object(settings, 'effective', return_value={}), mock.patch.object(web_config, 'start', return_value='http://localhost/'), mock.patch.object(tui, '_flash'), mock.patch.object(tui, '_wizard_slice') as wizard:
            self.assertIsNone(tui._run_feature_wizard(None, 'slice', 100))
            wizard.assert_not_called()

    def test_legacy_key_status_and_external_precedence(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, 'PROJECT_ROOT', Path(td)), mock.patch.dict('os.environ', {'DEEPSEEK_API_KEY':'legacy-secret', 'PYCUT_CLIP_DIR':'external'}, clear=True), mock.patch.object(config, '_dotenv_values', {}):
            Path(td, '.env').write_text('PYCUT_CLIP_DIR=file-value\n')
            values = settings.effective()
            self.assertEqual(values['PYCUT_MAIN_KEY'], 'legacy-secret')
            self.assertEqual(values['PYCUT_CLIP_DIR'], 'external')
            self.assertIn('背景切片目录', settings.overrides())

    def test_mosaic_is_full_width_rectangle_with_unchanged_exterior(self):
        frame = np.random.default_rng(4).integers(0, 255, (100, 90, 3), dtype=np.uint8)
        original = frame.copy()
        subtitle.mosaic_region(frame, 30, 24)
        np.testing.assert_array_equal(frame[:30], original[:30])
        np.testing.assert_array_equal(frame[54:], original[54:])
        self.assertFalse(np.array_equal(frame[30:54, :12], original[30:54, :12]))
        self.assertFalse(np.array_equal(frame[30:54, -12:], original[30:54, -12:]))
        np.testing.assert_array_equal(frame[30, 0], frame[30, 1])


if __name__ == '__main__':
    unittest.main()
