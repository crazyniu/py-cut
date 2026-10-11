"""独立质检必须抽取真实片段编号，而不是从 0 起的列表索引。"""
import unittest
from tools.verify import spread_sample


class VerifySamplingTests(unittest.TestCase):
    def test_returns_identifiers_including_first_and_last(self):
        self.assertEqual(spread_sample([1, 2], 2), [1, 2])
        self.assertEqual(spread_sample([3, 5, 8, 11, 20], 3), [3, 8, 20])

    def test_empty_and_single_samples_are_valid(self):
        self.assertEqual(spread_sample([], 3), [])
        self.assertEqual(spread_sample([1], 0), [])
        self.assertIn(spread_sample([3, 8], 1)[0], [3, 8])
