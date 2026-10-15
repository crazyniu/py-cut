"""Execute the real shell launcher with simulated package managers."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


def _bash_binaries():
    """Bash interpreters to exercise the launcher under.

    The launcher's shebang selects ``bash`` from PATH; that interpreter is always
    tested.  A newer bash (>= 4.0), e.g. Homebrew's, is added when present so the
    empty-array ``set -u`` fix is verified across major versions (Bash 3.2 vs 4+).
    """
    found = []
    default = shutil.which('bash')
    if default:
        found.append(default)
    for candidate in (shutil.which('bash4'), shutil.which('bash5'),
                      '/opt/homebrew/bin/bash', '/usr/local/bin/bash'):
        if candidate and candidate not in found and os.access(candidate, os.X_OK):
            found.append(candidate)
    return found


BASH_BINARIES = _bash_binaries()


@unittest.skipUnless(BASH_BINARIES, 'requires a bash launcher')
class LauncherInstallTests(unittest.TestCase):
    def exercise(self, ready=False, fail=False, platform='Linux', root=True,
                 no_sudo=False, bash=None):
        """Run the launcher in a temp dir with mocked tools.

        ``root`` controls the mocked ``id -u`` (0 = root, 1 = non-root);
        ``no_sudo`` omits the ``sudo`` mock so the no-sudo path is exercised.
        Returns ``(result, install_log, sudo_log)``.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'run.sh', tmp / 'run.sh')
            launcher = tmp / 'run.sh'
            launcher.write_text(launcher.read_text().replace(
                '/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3',
                str(tmp / 'missing-managed-python')))
            (tmp / 'requirements.txt').write_text('numpy\n')
            (tmp / '.env').touch()
            bins = tmp / 'bin'
            bins.mkdir()
            scripts = {
                'python3': '''#!/bin/bash
if [ "$1" = - ]; then test -f "$STATE/ready"; exit $?; fi
if [ "$1" = -c ] && [[ "$2" == *EXTERNALLY-MANAGED* ]]; then exit 1; fi
if [ "$1" = pycut.py ]; then echo CLI_STARTED; fi
exit 0
''',
                'uname': '#!/bin/bash\necho "$TEST_PLATFORM"\n',
                'id': '#!/bin/bash\necho %d\n' % (0 if root else 1),
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
            if not no_sudo:
                scripts['sudo'] = '#!/bin/bash\necho "sudo $*" >> "$STATE/sudo.log"\nexec "$@"\n'
            for name, script in scripts.items():
                p = bins / name
                p.write_text(script)
                p.chmod(0o755)
            if ready:
                (tmp / 'ready').touch()
            env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ['PATH'],
                       STATE=str(tmp), FAIL_INSTALL=str(int(fail)), TEST_PLATFORM=platform)
            result = subprocess.run([bash or BASH_BINARIES[0], str(launcher), '--version'],
                                    env=env, capture_output=True, text=True, timeout=20)
            install_log = tmp / 'install.log'
            sudo_log = tmp / 'sudo.log'
            return (result,
                    install_log.read_text() if install_log.exists() else '',
                    sudo_log.read_text() if sudo_log.exists() else '')

    def scenarios(self, **kwargs):
        """Run :meth:`exercise` under every discovered bash and yield
        ``(bash, result, install_log, sudo_log)``."""
        for bash in BASH_BINARIES:
            with self.subTest(bash=bash):
                yield bash, self.exercise(bash=bash, **kwargs)

    def test_root_install_succeeds_and_rechecks(self):
        for _bash, (result, commands, sudo_log) in self.scenarios(root=True, fail=False):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('install -y ffmpeg', commands)
            self.assertEqual(sudo_log, '', 'root must install without sudo')
            self.assertIn('CLI_STARTED', result.stdout)

    def test_root_install_failure_stops_before_cli(self):
        for _bash, (result, _commands, sudo_log) in self.scenarios(root=True, fail=True):
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sudo_log, '')
            self.assertNotIn('CLI_STARTED', result.stdout)

    def test_nonroot_sudo_install_succeeds_and_rechecks(self):
        for _bash, (result, commands, sudo_log) in self.scenarios(root=False, fail=False):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('install -y ffmpeg', commands)
            self.assertIn('sudo apt-get', sudo_log, 'non-root must install via sudo')
            self.assertIn('CLI_STARTED', result.stdout)

    def test_nonroot_sudo_install_failure_stops_before_cli(self):
        for _bash, (result, _commands, sudo_log) in self.scenarios(root=False, fail=True):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('sudo apt-get', sudo_log)
            self.assertNotIn('CLI_STARTED', result.stdout)

    def test_ready_environment_never_installs(self):
        result, commands, _sudo_log = self.exercise(ready=True, root=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(commands, '')

    def test_macos_uses_homebrew_instead_of_linux_manager(self):
        result, commands, _sudo_log = self.exercise(platform='Darwin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.strip(), 'brew install ffmpeg')

    def test_other_platforms_do_not_attempt_installation(self):
        result, commands, _sudo_log = self.exercise(platform='MINGW64_NT')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands, '')
