"""执行真实配置模板的原生目录选择 JS；Node 仅用于开发验证，不是运行依赖。"""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'requires Node.js for template JavaScript checks')
class DirectoryNavigationTests(unittest.TestCase):
    def test_template_navigation(self):
        script = Path(__file__).with_name('web_dir_navigation.js')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
