"""Unicode case-insensitive video filename search regression."""
import tempfile
import unittest
from pathlib import Path

from pycut.core.scan import search_videos


class ScanUnicodeSearchTests(unittest.TestCase):
    def test_unicode_casefold_matches_german_sharp_s(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            video = root / "Straße.MP4"
            video.touch()
            self.assertEqual(search_videos(root, "  STRASSE  "), [video])
            self.assertEqual(search_videos(root, "straße"), [video])

    def test_empty_and_ascii_search_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            video = root / "Clip.Mp4"
            video.touch()
            self.assertEqual(search_videos(root, "cLiP"), [video])
            self.assertEqual(search_videos(root, "  "), [])

    def test_depth_limit_and_hidden_directory_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            video = nested / "Straße.mp4"
            video.touch()
            hidden = root / ".hidden"
            hidden.mkdir()
            (hidden / "Straße.mp4").touch()
            self.assertEqual(search_videos(root, "STRASSE", max_depth=1), [])
            self.assertEqual(search_videos(root, "STRASSE", max_depth=2), [video])


if __name__ == "__main__":
    unittest.main()
