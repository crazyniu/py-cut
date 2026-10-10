"""Unicode filename search regression tests for issue #61."""
import tempfile
import unittest
from pathlib import Path

from pycut.core.scan import search_videos


class UnicodeSearchTests(unittest.TestCase):
    def test_unicode_casefold_and_existing_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            german = root / "Straße.MP4"
            english = root / "Clip.mp4"
            german.touch()
            english.touch()
            self.assertEqual(search_videos(root, " STRASSE "), [german])
            self.assertEqual(search_videos(root, "straße"), [german])
            self.assertEqual(search_videos(root, "CLIP"), [english])
            self.assertEqual(search_videos(root, "  "), [])

    def test_depth_limit_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            video = deep / "Straße.mp4"
            video.touch()
            self.assertEqual(search_videos(root, "STRASSE", max_depth=1), [])
            self.assertEqual(search_videos(root, "STRASSE", max_depth=2), [video])


if __name__ == "__main__":
    unittest.main()
