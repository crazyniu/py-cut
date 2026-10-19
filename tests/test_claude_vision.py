"""Exercise SDK safety/lifecycle behavior without claiming model accuracy."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from pycut.core.runtime import STOP
from pycut.desktop.claude_vision import ClaudeVision
from pycut.desktop.local_vision import make_local_vision


SERVER = '''
import base64, json, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    req = json.loads(line)
    if req['type'] == 'control_request':
        subtype = req['request']['subtype']
        result = {'models':[]} if subtype == 'initialize' else {'mcpServers':[]}
        if mode == 'mcp' and subtype == 'mcp_status': result = {'mcpServers':[{'name':'unexpected'}]}
        print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':req['request_id'],'response':result}}),flush=True)
    elif req['type'] == 'user':
        assert len(req['message']['content']) == 3
        image = req['message']['content'][1]['source']
        assert image['type'] == 'base64' and image['media_type'] == 'image/jpeg'
        assert base64.b64decode(image['data']).startswith(bytes.fromhex('ffd8'))
        tools = ['Read'] if mode == 'exposed' else []
        print(json.dumps({'type':'system','subtype':'init','tools':tools,'mcp_servers':[]}),flush=True)
        if mode == 'timeout': time.sleep(10)
        if mode == 'tool':
            print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read'}]}}),flush=True)
        if mode == 'approval':
            print(json.dumps({'type':'control_request','request_id':'permission','request':{}}),flush=True)
        answer = 'not-json' if mode == 'malformed' else '{"has_subtitle":true}'
        print(json.dumps({'type':'result','subtype':'error_during_execution' if mode == 'failed' else 'success','is_error':mode == 'auth','result':answer}),flush=True)
'''


class ClaudeVisionTests(unittest.TestCase):
    def setUp(self):
        STOP.flag = False
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / 'server.py'
        self.script.write_text(SERVER)
        self.real_popen = subprocess.Popen
        self.children, self.directories = [], []
        self.frames = [np.zeros((16, 9, 3), dtype=np.uint8)] * 2

    def tearDown(self):
        STOP.flag = False
        self.temp.cleanup()
        for child in self.children:
            self.assertIsNotNone(child.poll())
        for folder in self.directories:
            self.assertFalse(Path(folder).exists())

    def server(self, mode):
        def spawn(command, **kwargs):
            self.assertIn('--safe-mode', command)
            self.assertIn('--no-session-persistence', command)
            self.assertIn('--strict-mcp-config', command)
            self.assertEqual(command[command.index('--tools')+1], '')
            self.assertEqual(command[command.index('--model')+1], 'selected-real-model')
            self.directories.append(kwargs['cwd'])
            child = self.real_popen([sys.executable, str(self.script), mode], **kwargs)
            self.children.append(child)
            return child
        return patch('pycut.desktop.agent_catalog.subprocess.Popen', side_effect=spawn)

    def test_explicit_model_and_base64_frames_complete_and_cleanup(self):
        with self.server('ok'):
            client = make_local_vision('claude', 'test-agent', 'selected-real-model')
            self.assertEqual(client.ask_vision(self.frames, 'detect'), {'has_subtitle':True})
            self.assertEqual((client.calls,client.failures), (1,0))
            client.close()

    def test_failed_invalid_exposed_tools_and_permissions_never_return_detection(self):
        for mode in ('failed','auth','malformed','mcp','exposed','tool','approval','timeout'):
            with self.subTest(mode=mode), self.server(mode):
                client = ClaudeVision('test-agent','selected-real-model')
                start = time.monotonic()
                self.assertIsNone(client.ask_vision(self.frames,'detect',deadline=start+.3))
                self.assertEqual(client.failures,1)
                self.assertLess(time.monotonic()-start,1.5)

    def test_expired_and_cancelled_requests_never_spawn(self):
        with patch('pycut.desktop.agent_catalog.subprocess.Popen') as spawn:
            client = ClaudeVision('test-agent','selected-real-model')
            self.assertIsNone(client.ask_vision(self.frames,'detect',deadline=time.monotonic()-1))
            STOP.flag = True
            self.assertIsNone(client.ask_vision(self.frames,'detect'))
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
