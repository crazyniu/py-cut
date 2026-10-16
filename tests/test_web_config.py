"""真实 HTTP/.env 与矩形马赛克的回归，不调用外部 LLM。"""
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock
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
            self.assertEqual(settings.directories(settings.read_env()['PYCUT_CLIP_DIR']), [folder])
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
            with urllib.request.urlopen(url) as response:
                page = response.read().decode('utf-8')
            self.assertIn('data-panel="llm"', page)
            self.assertNotIn('DIRECTORY_FIELDS', page)
            for key in settings.FIELDS:
                self.assertIn(f'id="{key}"', page)
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
            before = Path(td, '.env').read_bytes()
            invalid = {'PYCUT_CLIP_DIR':str(Path(td, 'missing'))}
            req = urllib.request.Request(url+'api/config', json.dumps(invalid).encode(), headers, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            feedback = json.loads(error.exception.read())
            self.assertEqual(feedback['problems'], [f'混剪切片目录第 1 项不存在或无法访问：{Path(td, "missing")}'])
            self.assertEqual(before, Path(td, '.env').read_bytes())
            req = urllib.request.Request(url+'api/config', b'{', headers, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code, 400)
            headers['Origin'] = 'https://example.com'
            req = urllib.request.Request(url+'api/config', b'{}', headers, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code, 403)

    def test_dir_browse_endpoint_requires_token_and_returns_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, 'PROJECT_ROOT', Path(td)), mock.patch.object(config, 'refresh'), mock.patch.dict('os.environ', {}, clear=True):
            root = Path(td)
            (root / '中文 空格').mkdir()
            url = web_config.start(open_browser=False)
            self.addCleanup(web_config.stop)
            quoted = urllib.parse.quote(str(root))
            # 无 token 拒绝
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + 'api/dirs?path=' + quoted)
            self.assertEqual(error.exception.code, 403)
            # 带 token 返回真实绝对路径与中文空格目录
            headers = {'X-Config-Token': web_config._TOKEN}
            req = urllib.request.Request(url + 'api/dirs?path=' + quoted, headers=headers)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read())
            self.assertIsNone(data['error'])
            self.assertEqual(data['path'], str(root.resolve()))
            self.assertIn('中文 空格', [d['name'] for d in data['dirs']])
            # 不存在的路径返回清晰错误
            req = urllib.request.Request(url + 'api/dirs?path=' + urllib.parse.quote(str(root / 'nope')), headers=headers)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read())
            self.assertEqual(data['error'], '目录不存在')

    def test_missing_configuration_never_enters_processing(self):
        with mock.patch.object(config, 'refresh'), mock.patch.object(settings, 'effective', return_value={}), mock.patch.object(web_config, 'start', return_value='http://localhost/'), mock.patch.object(tui, '_flash'), mock.patch.object(tui, '_wizard_cover') as wizard:
            self.assertIsNone(tui._run_feature_wizard(None, 'cover', 100))
            wizard.assert_not_called()

    def test_legacy_key_status_and_external_precedence(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, 'PROJECT_ROOT', Path(td)), mock.patch.dict('os.environ', {'DEEPSEEK_API_KEY':'legacy-secret', 'PYCUT_CLIP_DIR':'external'}, clear=True), mock.patch.object(config, '_dotenv_values', {}):
            Path(td, '.env').write_text('PYCUT_CLIP_DIR=file-value\n')
            values = settings.effective()
            self.assertEqual(values['PYCUT_MAIN_KEY'], 'legacy-secret')
            self.assertEqual(values['PYCUT_CLIP_DIR'], 'external')
            self.assertIn('混剪切片目录', settings.overrides())



if __name__ == '__main__':
    unittest.main()
