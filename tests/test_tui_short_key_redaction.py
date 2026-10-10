"""Short CLI secrets must not fragment ordinary TUI log text."""
from __future__ import annotations

import unittest

from pycut import tui_runner


class ShortKeyRedactionTests(unittest.TestCase):
    def test_short_key_hides_whole_matching_line(self):
        self.assertEqual(
            tui_runner._redact("failed to launch", ["slice", "--llm-key", "a"]),
            "[日志已隐藏：包含敏感参数]",
        )

    def test_unrelated_line_is_unchanged(self):
        self.assertEqual(
            tui_runner._redact("process finished", ["slice", "--llm-key", "a"]),
            "process finished",
        )

    def test_equals_form_short_key(self):
        self.assertEqual(
            tui_runner._redact("token=xy", ["slice", "--api-key=xy"]),
            "[日志已隐藏：包含敏感参数]",
        )

    def test_long_key_still_masks_only_secret(self):
        self.assertEqual(
            tui_runner._redact("error sk-private-token in command",
                               ["slice", "--llm-key", "sk-private-token"]),
            "error ******** in command",
        )


if __name__ == "__main__":
    unittest.main()
