"""Execute the real shell launcher with simulated package managers."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('bash'), 'requires bash launcher')
class LauncherInstallTests(unittest.TestCase):
    def exercise(self, ready=False, fail=False, platform='Linux'):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'run.sh', root / 'run.sh')
            launcher = root / 'run.sh'
            launcher.write_text(launcher.read_text().replace(
                '/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3',
                str(root / 'missing-managed-python')))
            (root / 'requirements.txt').write_text('numpy\n')
            (root / '.env').touch()
            bins = root / 'bin'
            bins.mkdir()
            scripts = {
                'python3': '''#!/bin/bash
if [ "$1" = - ]; then test -f "$STATE/ready"; exit $?; fi
if [ "$1" = -c ] && [[ "$2" == *EXTERNALLY-MANAGED* ]]; then exit 1; fi
if [ "$1" = pycut.py ]; then echo CLI_STARTED; fi
exit 0
''',
                'uname': '#!/bin/bash\necho "$TEST_PLATFORM"\n',
                'id': '#!/bin/bash\necho 0\n',
                'apt-get': '''#!/bin/bash
echo "$*" >> "$STATE/install.log"
if [ "$FAIL_INSTALL" = 1 ]; then exit 1; fi
if [ "$1" = install ]; then touch "$STATE/ready"; fi
''',
                'brew': '''#!/bin/bash
echo "brew $*" >> "$STATE/install.log"
if [ "$FAIL_INSTALL" = 1 ]; then exit 1; fi
if [ "$1" = install ]; then touch "$STATE/ready"; fi
''',
            }
            for name, script in scripts.items():
                p = bins / name
                p.write_text(script)
                p.chmod(0o755)
            if ready:
                (root / 'ready').touch()
            env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ['PATH'],
                       STATE=str(root), FAIL_INSTALL=str(int(fail)), TEST_PLATFORM=platform)
            result = subprocess.run(['bash', str(root / 'run.sh'), '--version'],
                                    env=env, capture_output=True, text=True, timeout=20)
            log = root / 'install.log'
            return result, log.read_text() if log.exists() else ''

    def test_missing_ffmpeg_is_installed_and_rechecked(self):
        result, commands = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('install -y ffmpeg', commands)
        self.assertIn('CLI_STARTED', result.stdout)

    def test_ready_environment_never_installs(self):
        result, commands = self.exercise(ready=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(commands, '')

    def test_install_failure_stops_before_cli(self):
        result, _ = self.exercise(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('CLI_STARTED', result.stdout)

    def test_macos_uses_homebrew_instead_of_linux_manager(self):
        result, commands = self.exercise(platform='Darwin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.strip(), 'brew install ffmpeg')

    def test_other_platforms_do_not_attempt_installation(self):
        result, commands = self.exercise(platform='MINGW64_NT')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands, '')
