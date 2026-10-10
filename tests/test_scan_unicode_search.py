"""Unicode-aware filename search regression tests for issue #61."""
import tempfile
import unittest
from pathlib import Path

from pycut.core.scan import search_videos


class UnicodeSearchTests(unittest.TestCase):
    def test_casefold_and_existing_search_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            german = root / "Straße.MP4"
            english = root / "Holiday.mp4"
            nested = root / "nested" / "STRASSE.mov"
            nested.parent.mkdir()
            for path in (german, english, nested):
                path.touch()
            self.assertIn(german, search_videos(root, " STRASSE ", max_depth=0))
            self.assertIn(german, search_videos(root, "straße", max_depth=0))
            self.assertEqual(search_videos(root, "holiday", max_depth=0), [english])
            self.assertEqual(search_videos(root, "  ", max_depth=3), [])
            self.assertNotIn(nested, search_videos(root, "strasse", max_depth=0))
            self.assertIn(nested, search_videos(root, "straße", max_depth=1))


if __name__ == "__main__":
    unittest.main()
