"""Protocol metadata checks; catalogs never establish model recognition accuracy."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from pycut.core.runtime import STOP
from pycut.desktop.agent_catalog import list_models


SERVER = '''
import json, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    req = json.loads(line)
    method = req.get('method')
    assert method not in ('thread/start', 'turn/start')
    assert req.get('type') != 'user'
    if method == 'initialized': continue
    if mode == 'timeout': time.sleep(10)
    if method:
        result = {}
        if method == 'model/list':
            cursor = req['params'].get('cursor')
            result = {'data': [
                {'id':'text-only','inputModalities':['text']},
                {'id':'hidden','hidden':True},
                {'id':'vision','model':'real-vision','displayName':'Vision','inputModalities':['image'],'isDefault':True}
            ], 'nextCursor':'next'} if not cursor else {'data':[{'id':'older-catalog'}], 'nextCursor':None}
            if mode == 'loop': result['nextCursor'] = 'next'
        print(json.dumps({'id':req['id'],'result':result}),flush=True)
'''


class AgentCatalogTests(unittest.TestCase):
    def setUp(self):
        STOP.flag = False
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / 'server.py'
        self.script.write_text(SERVER)
        self.real_popen = subprocess.Popen
        self.children = []

    def tearDown(self):
        STOP.flag = False
        self.temp.cleanup()
        for child in self.children:
            self.assertIsNotNone(child.poll())

    def server(self, mode, provider):
        def spawn(command, **kwargs):
            if provider == 'codex':
                self.assertIn('features.shell_tool=false', command)
            child = self.real_popen([sys.executable, str(self.script), mode], **kwargs)
            self.children.append(child)
            return child
        return patch('pycut.desktop.local_vision.subprocess.Popen', side_effect=spawn)

    def test_codex_pagination_modalities_and_legacy_catalog(self):
        with self.server('ok', 'codex'):
            self.assertEqual(list_models('codex', 'test-agent'), [
                {'id':'real-vision','name':'Vision','is_default':True},
                {'id':'older-catalog','name':'older-catalog','is_default':False}])

    def test_timeouts_errors_and_invalid_pagination_close_children(self):
        for provider, mode in [('codex','loop'), ('codex','timeout')]:
            with self.subTest(provider=provider), self.server(mode, provider):
                start = time.monotonic()
                with self.assertRaises((RuntimeError, TimeoutError)) as error:
                    list_models(provider, 'test-agent', timeout=.2)
                self.assertNotIn('private-token', str(error.exception))
                self.assertLess(time.monotonic()-start, 1.5)

    def test_missing_unknown_or_interrupted_agents_never_spawn(self):
        with patch('pycut.desktop.agent_catalog._binary', return_value=''), patch(
                'pycut.desktop.local_vision.subprocess.Popen') as spawn:
            for provider in ('unknown','claude'):
                with self.assertRaises(ValueError): list_models(provider)
            STOP.flag = True
            with self.assertRaises(TimeoutError): list_models('codex', 'test-agent')
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
