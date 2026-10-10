"""Offline tests for timestamp-to-scene keyword matching."""
import unittest

from pycut.core.semantic_segments import (
    TranscriptSpan, match_segments,
)


class SemanticSegmentsTests(unittest.TestCase):
    def test_matches_cross_scene_speech_without_moving_cuts(self):
        scenes = [(0, 2), (2, 4), (4, 6)]
        words = [TranscriptSpan(1.5, 3, "Hello 世界")]
        found = match_segments(scenes, words, ["世界"])
        self.assertEqual([item.selected for item in found], [True, True, False])
        self.assertEqual([(x.start, x.end) for x in found], scenes)

    def test_touching_boundary_does_not_overlap(self):
        found = match_segments([(0, 2), (2, 4)],
                               [TranscriptSpan(2, 3, "hello")], ["HELLO"])
        self.assertEqual([x.selected for x in found], [False, True])

    def test_duplicate_hits_have_stable_reason(self):
        found = match_segments([(0, 5)],
                               [TranscriptSpan(0, 3, "Hi hi")], ["hi", "HI"])
        self.assertEqual(found[0].reason, "matched: hi")

    def test_empty_input_fails_closed(self):
        with self.assertRaises(ValueError):
            match_segments([(0, 2)], [], ["hello"])
        with self.assertRaises(ValueError):
            match_segments([(0, 2)], [TranscriptSpan(0, 1, "hello")], [" "])

    def test_invalid_ranges_fail(self):
        for span in [TranscriptSpan(-1, 1, "x"),
                     TranscriptSpan(2, 1, "x"),
                     TranscriptSpan(float("nan"), 2, "x")]:
            with self.subTest(span=span), self.assertRaises(ValueError):
                match_segments([(0, 2)], [span], ["x"])
        with self.assertRaises(ValueError):
            match_segments([(2, 2)], [TranscriptSpan(0, 3, "x")], ["x"])


if __name__ == "__main__":
    unittest.main()
