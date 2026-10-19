"""Protocol/lifecycle coverage; these tests do not measure model accuracy."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from pycut.core.runtime import STOP
from pycut.desktop.local_vision import CodexVision, make_local_vision


SERVER = '''
import json, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    req = json.loads(line)
    method = req.get('method')
    if method == 'initialized': continue
    result = {}
    if method == 'account/read': result = {'account': {'type':'chatgpt'}}
    if method == 'config/read': result = {'config':{'mcp_servers':{'inherited':{'enabled':True}}}}
    if method == 'mcpServerStatus/list':
        result = {'data':[{'tools':{'unexpected':{}}}]} if mode == 'exposed' else {'data':[]}
    if method == 'thread/start':
        assert req['params']['config']['mcp_servers.inherited.enabled'] is False
        result = {'thread':{'id':'thread-1'}}
    print(json.dumps({'id':req['id'],'result':result}), flush=True)
    if method == 'turn/start':
        assert len(req['params']['input']) == 3
        if mode == 'timeout': time.sleep(10)
        elif mode == 'tool':
            print(json.dumps({'id':42,'method':'item/commandExecution/requestApproval','params':{}}),flush=True)
        else:
            text = json.dumps({'frames':[{'frame':0,'has_subtitle':True,'box':[0,.6,1,.8],'text':'example'}]})
            if mode == 'malformed': text = 'not json'
            print(json.dumps({'method':'item/completed','params':{'threadId':'thread-1','item':{'type':'agentMessage','text':text}}}),flush=True)
            status = 'failed' if mode == 'failed' else 'completed'
            print(json.dumps({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'status':status}}}),flush=True)
'''


class LocalVisionTests(unittest.TestCase):
    def setUp(self):
        STOP.flag = False
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / 'server.py'
        self.script.write_text(SERVER)
        self.real_popen = subprocess.Popen
        self.frames = [np.zeros((16, 9, 3), dtype=np.uint8)] * 2

    def tearDown(self):
        STOP.flag = False
        self.temp.cleanup()

    def client(self, mode):
        def spawn(command, **kwargs):
            self.assertIn('features.shell_tool=false', command)
            return self.real_popen([sys.executable, str(self.script), mode], **kwargs)
        return patch('pycut.desktop.local_vision.subprocess.Popen', side_effect=spawn)

    def test_completed_turn_and_cleanup(self):
        with self.client('ok'):
            client = CodexVision('codex')
            self.assertTrue(client.authenticated())
            result = client.ask_vision(self.frames, 'detect', deadline=time.monotonic()+2)
            self.assertEqual(result['frames'][0]['text'], 'example')
            self.assertEqual((client.calls, client.failures), (1, 0))
            directory = client._directory.name
            process = client._process
            client.close()
            self.assertIsNotNone(process.poll())
            self.assertFalse(Path(directory).exists())

    def test_failed_invalid_and_tool_requests_never_return_detection(self):
        for mode in ('failed', 'malformed', 'tool', 'exposed'):
            with self.subTest(mode=mode), self.client(mode):
                client = CodexVision('codex')
                self.assertIsNone(client.ask_vision(self.frames, 'detect', deadline=time.monotonic()+2))
                self.assertEqual(client.failures, 1)
                self.assertIsNone(client._process)
                client.close()

    def test_deadline_kills_pending_turn(self):
        with self.client('timeout'):
            client = CodexVision('codex')
            start = time.monotonic()
            self.assertIsNone(client.ask_vision(self.frames, 'detect', deadline=start+.2))
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertIsNone(client._process)
            client.close()

    def test_expired_or_cancelled_request_never_spawns(self):
        with patch('pycut.desktop.local_vision.subprocess.Popen') as spawn:
            client = CodexVision('codex')
            self.assertIsNone(client.ask_vision(self.frames, 'detect', deadline=time.monotonic()-1))
            STOP.flag = True
            self.assertIsNone(client.ask_vision(self.frames, 'detect'))
            spawn.assert_not_called()
            client.close()

    def test_unsupported_provider_is_explicit(self):
        with self.assertRaises(ValueError):
            make_local_vision('pi')


if __name__ == '__main__':
    unittest.main()
