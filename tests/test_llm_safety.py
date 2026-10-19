"""Offline LLM parsing/retry/sticky-backup tests without real API/video calls."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest import mock

from pycut import config
from pycut.core import llm


class LLMRetrySafetyTests(unittest.TestCase):
    def test_explicitly_disabled_fallback_stays_on_main(self):
        client = llm.make_llm('main', use_fallback=False)
        self.assertIsNone(client.fallback)

    def test_empty_length_response_retries_then_accepts_subtitle_box(self):
        client = llm.LLMClient('https://primary.invalid/v1', 'test-key', 'model')
        empty = json.dumps({'choices': [{'message': {'content': ''},
                                        'finish_reason': 'length'}]})
        box = {'has_subtitle': True, 'box': [0.1, 0.7, 0.9, 0.9]}
        good = json.dumps({'choices': [{'message': {
            'content': '```json\n' + json.dumps(box) + '\n```'},
            'finish_reason': 'stop'}]})
        with mock.patch.object(client, '_post', side_effect=[(200, empty), (200, good)]) as post:
            self.assertEqual(client.ask_vision(object(), 'subtitle?'), box)
        self.assertEqual(post.call_args_list[1].args[0]['max_tokens'],
                         config.LLM_MAX_TOKENS_RETRY)
        self.assertEqual(client.failures, 1)
        self.assertIsNone(client.switched_to)

    def setUp(self):
        self.image = mock.patch.object(
            llm.cv2, "imencode",
            return_value=(True, SimpleNamespace(tobytes=lambda: b"fake jpeg")),
        )
        self.stopped = mock.patch.object(llm.STOP, "flag", False)
        self.image.start()
        self.stopped.start()
        self.addCleanup(self.image.stop)
        self.addCleanup(self.stopped.stop)

    def test_empty_success_responses_trigger_fallback(self):
        backup = llm.LLMClient(
            "https://backup.invalid/v1", "test-key", "model", label="backup")
        backup.ask_vision = mock.Mock(return_value={"face": True})
        client = llm.LLMClient(
            "https://primary.invalid/v1", "test-key", "model",
            label="main", fallback=backup)
        length_response = json.dumps({
            "choices": [{"message": {"content": ""},
                         "finish_reason": "length"}],
        })
        with mock.patch.object(client, "_post",
                               return_value=(200, length_response)) as requests, \
             mock.patch.object(llm, "log"):
            result = client.ask_vision(object(), "face?")
        self.assertEqual(result, {"face": True})
        self.assertEqual(client.switched_to, "backup")
        self.assertEqual(client.failures, config.LLM_FAILOVER_FAILS)
        self.assertEqual(requests.call_count, config.LLM_FAILOVER_FAILS)
        self.assertEqual(requests.call_args.args[0]["max_tokens"],
                         config.LLM_MAX_TOKENS_RETRY)
        backup.ask_vision.assert_called_once()

    def test_non_json_http_200_also_counts_as_failure(self):
        client = llm.LLMClient(
            "https://primary.invalid/v1", "test-key", "model")
        with mock.patch.object(client, "_post",
                               return_value=(200, "invalid response")) as requests:
            self.assertIsNone(client.ask_vision(object(), "face?"))
        self.assertEqual(requests.call_count, 3)
        self.assertEqual(client.failures, 3)
        self.assertEqual(client._consec_fail, 3)

    def test_valid_response_resets_consecutive_failure_counter(self):
        client = llm.LLMClient(
            "https://primary.invalid/v1", "test-key", "model")
        bad = json.dumps({"choices": [
            {"message": {"content": "not-json"}, "finish_reason": "stop"}]})
        good = json.dumps({"choices": [
            {"message": {"content": json.dumps({"face": False,
                                                 "confidence": 0.9})},
             "finish_reason": "stop"}]})
        with mock.patch.object(client, "_post",
                               side_effect=[(200, bad), (200, good)]):
            result = client.ask_vision(object(), "face?")
        self.assertEqual(result["face"], False)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.failures, 1)
        self.assertEqual(client._consec_fail, 0)


if __name__ == "__main__":
    unittest.main()
