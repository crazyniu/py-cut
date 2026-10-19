"""Exercise catalog membership, scoped history, real-file snapshots and UI media."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pycut import config
from pycut.desktop.library import LibraryIndexer
from pycut.desktop.media import MediaCache, probe_metadata
from pycut.desktop.store import Store, stamp


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = Store(self.base / "app")
        self.project = self.store.create_project("产品一")
        self.library = next(row for row in self.store.libraries(self.project["id"])
                            if row["role"] == "background")
        self.root = self.base / "素材"
        self.root.mkdir()
        self.store.add_roots(self.library["id"], [str(self.root)])
        self.indexer = LibraryIndexer(self.store)
        self.probe = patch("pycut.desktop.library.probe_metadata",
                           return_value={"duration": 3.0, "width": 1080, "height": 1920})
        self.probe_mock = self.probe.start()
        self.addCleanup(self.probe.stop)
        self.identity = patch("pycut.desktop.library.tool_identity", return_value="tool-v1")
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def file(self, name="video.mp4"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
        return path

    def scan(self):
        return self.indexer.scan(self.library["id"])

    def assets(self, **kwargs):
        return self.store.assets(self.library["id"], **kwargs)

    def test_project_defaults_and_reopen(self):
        self.assertEqual(len(self.store.libraries(self.project["id"])), 5)
        other = self.store.create_project("产品二", str(self.base / "成片"))
        self.assertEqual(Store(self.base / "app").projects()[1]["id"], other["id"])
        self.assertEqual(len(self.store.libraries(other["id"])), 5)
        renamed = self.store.update_project(other["id"], name="产品二改名")
        self.assertEqual(renamed["name"], "产品二改名")
        with self.assertRaises(ValueError):
            self.store.update_project(other["id"], unknown=True)

    def test_scan_reports_real_incremental_counts_and_final_state(self):
        events = []
        for number in range(26):
            self.file(f"{number}.mp4")
        result = LibraryIndexer(self.store, events.append).scan(self.library["id"])
        self.assertEqual([(e["indexed"], e["scanning"]) for e in events],
                         [(0, True), (25, True), (26, False)])
        self.assertEqual(events[-1]["available"], result["available"])

    def test_type_filter_counts_and_pages_match_visible_assets(self):
        self.file("reference.mp4")
        self.file("reference.srt")
        self.scan()
        self.assertEqual(self.assets()["total"], 2)
        videos = self.assets(kind="video", limit=1)
        self.assertEqual(videos["total"], 1)
        self.assertEqual(videos["items"][0]["name"], "reference.mp4")
        self.assertEqual(self.assets(kind="subtitle")["items"][0]["name"], "reference.srt")
        with self.assertRaises(ValueError):
            self.assets(kind="unknown")

    def test_recursive_overlapping_roots_no_directory_symlinks(self):
        nested = self.file("a/b/c/d/竖屏.MP4")
        self.store.add_roots(self.library["id"], [str(nested.parent), str(self.root)])
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "not-in-library.mp4").write_bytes(b"source")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        summary = self.scan()
        self.assertEqual(summary["indexed"], 1)
        self.assertEqual(self.assets()["total"], 1)
        self.assertEqual(self.assets(root=str(nested.parent))["total"], 1)
        self.assertEqual(self.assets()["items"][0]["path"], str(nested.resolve()))

    def test_removal_is_per_library_persistent_and_restorable(self):
        video = self.file()
        self.scan()
        asset = self.assets()["items"][0]
        second = self.store.create_library(self.project["id"], "另一个素材库")
        self.store.add_roots(second["id"], [str(self.root)])
        self.indexer.scan(second["id"])
        self.store.set_removed(self.library["id"], [asset["id"]], True)
        self.scan()
        self.assertTrue(video.exists())
        self.assertEqual(self.assets()["total"], 0)
        self.assertEqual(self.assets(removed=True)["items"][0]["id"], asset["id"])
        self.assertEqual(self.store.assets(second["id"])["total"], 1)
        self.store.remove_root(self.library["id"], str(self.root))
        self.assertEqual(self.assets(removed=True)["total"], 0)
        self.store.add_roots(self.library["id"], [str(self.root)])
        self.scan()
        self.assertEqual(self.assets()["total"], 0)
        self.store.set_removed(self.library["id"], [asset["id"]], False)
        self.assertEqual(self.assets()["total"], 1)

    def test_offline_keeps_metadata_and_snapshot_uses_live_files(self):
        video = self.file()
        self.scan()
        current = self.store.snapshot(self.library["id"])
        self.assertEqual(current[0]["signature"], stamp(video))
        video.write_bytes(b"changed-source")
        self.assertNotEqual(self.store.snapshot(self.library["id"])[0]["signature"], current[0]["signature"])
        video.unlink()
        self.assertEqual(self.store.snapshot(self.library["id"]), [])
        self.root.rmdir()
        summary = self.scan()
        self.assertEqual(summary["offline"], 1)
        self.assertTrue(summary["errors"])
        self.assertFalse(self.assets()["items"][0]["available"])
        self.assertFalse(self.store.libraries(self.project["id"])[1]["roots"][0]["available"])
        self.file()
        self.scan()
        self.assertTrue(self.assets()["items"][0]["available"])

    def test_probe_failure_remains_visible_and_retries(self):
        self.file()
        self.probe_mock.side_effect = ValueError("bad media")
        summary = self.scan()
        self.assertEqual(summary["indexed"], 1)
        self.assertIn("bad media", summary["errors"][0]["error"])
        self.assertEqual(self.assets()["items"][0]["error"], "bad media")
        self.probe_mock.side_effect = None
        self.scan()
        self.assertEqual(self.assets()["items"][0]["error"], "")
        self.assertEqual(self.probe_mock.call_count, 2)

    def test_metadata_cache_invalidates_by_stat_and_tool(self):
        video = self.file()
        self.scan()
        self.scan()
        self.assertEqual(self.probe_mock.call_count, 1)
        video.write_bytes(b"changed")
        self.scan()
        self.assertEqual(self.probe_mock.call_count, 2)
        with patch("pycut.desktop.library.tool_identity", return_value="tool-v2"):
            self.scan()
        self.assertEqual(self.probe_mock.call_count, 3)

    def test_same_stem_subtitle_and_search_sort_pagination(self):
        self.file("甲.MP4")
        subtitle = self.file("甲.SRT")
        self.file("乙.mp4")
        self.scan()
        result = self.assets(query="甲")
        self.assertEqual(result["total"], 2)
        video = next(item for item in result["items"] if item["kind"] == "video")
        self.assertEqual(video["srt_path"], str(subtitle.resolve()))
        self.assertEqual(len(self.assets(limit=1, offset=1)["items"]), 1)
        self.assertEqual(self.assets(sort="size")["items"][0]["kind"], "video")
        with self.assertRaises(ValueError):
            self.assets(sort="name; DROP TABLE assets")

    def test_usage_once_per_final_scoped_and_immutable(self):
        video = self.file()
        self.scan()
        asset = self.assets()["items"][0]
        output = self.base / "output.mp4"
        output.write_bytes(b"final")
        uses = [{"asset_id": asset["id"], "role": "background", "start": 0, "end": 1},
                {"path": str(video), "role": "background", "start": 2, "end": 3}]
        self.store.record_output("output-1", self.project["id"], "job", str(output), uses)
        self.store.record_output("output-1", self.project["id"], "job", str(output), uses)
        self.assertEqual(self.assets()["items"][0]["usage_count"], 1)
        detail = self.store.asset_detail(asset["id"], self.project["id"])
        self.assertEqual(len(detail["uses"]), 2)
        old_signature = detail["uses"][0]["signature"]
        video.write_bytes(b"changed")
        self.scan()
        self.assertNotEqual(self.assets()["items"][0]["signature"], old_signature)
        self.assertEqual(self.store.asset_detail(asset["id"])["uses"][0]["signature"], old_signature)
        other = self.store.create_project("另一个产品")
        library = self.store.libraries(other["id"])[1]
        self.store.add_roots(library["id"], [str(self.root)])
        self.indexer.scan(library["id"])
        self.assertEqual(self.store.assets(library["id"])["items"][0]["usage_count"], 0)
        self.store.set_removed(self.library["id"], [asset["id"]], True)
        self.store.remove_root(self.library["id"], str(self.root))
        self.assertEqual(self.store.asset_detail(asset["id"], self.project["id"])["usage_count"], 1)
        self.assertEqual(len(self.store.outputs(self.project["id"])[0]["uses"]), 2)

    def test_unpublished_outputs_never_count_and_external_provenance_records(self):
        self.file()
        self.scan()
        with self.assertRaises(FileNotFoundError):
            self.store.record_output("failed", self.project["id"], "job", str(self.base / "absent.mp4"), [])
        self.assertEqual(self.store.outputs(self.project["id"]), [])
        output = self.base / "published.mp4"
        output.write_bytes(b"final")
        sticker = self.base / "already-cleaned.png"
        self.store.record_output("ok", self.project["id"], "job", str(output),
                                 [{"path": str(sticker), "role": "sticker", "url": "https://example.com/a.png"}])
        use = self.store.outputs(self.project["id"])[0]["uses"][0]
        self.assertEqual(use["path"], str(sticker.resolve()))
        self.assertEqual(use["details"]["url"], "https://example.com/a.png")
        self.store.record_output("dedup-output", self.project["id"], "job", str(output), [])
        self.assertEqual(len(self.store.outputs(self.project["id"])), 2)

    def test_credential_fields_rejected_recursively_and_settings_scoped(self):
        for key in ("PYCUT_MAIN_KEY", "apiKey", "password", "token", "key"):
            with self.assertRaises(ValueError):
                self.store.save_settings({"provider": {key: "test-secret"}})
        self.store.save_settings({"agent": "codex", "theme": "dark"})
        self.store.save_settings({"count": 2}, self.project["id"])
        self.assertEqual(self.store.settings()["agent"], "codex")
        self.assertEqual(self.store.settings(self.project["id"]), {"count": 2})

    def test_foreign_keys_and_membership_validation(self):
        self.file()
        self.scan()
        asset = self.assets()["items"][0]
        self.assertEqual(self.store.asset_detail(asset["id"], self.project["id"])["usage_count"], 0)
        other = self.store.create_project("Other")
        with self.assertRaises(ValueError):
            self.store.asset_detail(asset["id"], other["id"])
        with self.assertRaises(ValueError):
            self.store.set_removed(self.library["id"], ["absent"], True)
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


@unittest.skipUnless(shutil.which(config.FFMPEG) and shutil.which(config.FFPROBE), "FFmpeg tools unavailable")
class RealMediaTests(unittest.TestCase):
    def test_portrait_thumbnail_preview_cache_and_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "竖屏.mkv"
            subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=180x320:rate=12:duration=0.5", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=0.5", "-c:v", "ffv1", "-c:a", "pcm_s16le",
                            str(source)], check=True, capture_output=True)
            store = Store(base / "data")
            project = store.create_project("产品")
            library = store.libraries(project["id"])[1]
            store.add_roots(library["id"], [str(base)])
            summary = LibraryIndexer(store).scan(library["id"])
            self.assertEqual(summary["errors"], [])
            asset = store.assets(library["id"])["items"][0]
            self.assertEqual((asset["width"], asset["height"]), (180, 320))
            cache = MediaCache(store)
            thumbnail = Path(cache.thumbnail(asset["id"]))
            preview = Path(cache.preview(asset["id"]))
            self.assertTrue(thumbnail.is_file())
            self.assertNotEqual(preview, source)
            self.assertEqual(cache.preview(asset["id"]), str(preview))
            details = cache.details_path(source)
            self.assertEqual(details["video_codec"], "ffv1")
            self.assertEqual(details["fps"], 12)
            self.assertEqual(details["size"], source.stat().st_size)
            self.assertEqual(details["audio"][0]["codec"], "pcm_s16le")
            with patch("pycut.desktop.media.probe_metadata", side_effect=AssertionError("cached details re-probed")):
                self.assertEqual(cache.details_path(source), details)
            metadata = probe_metadata(preview, "video")
            self.assertEqual(metadata["video_codec"], "h264")
            self.assertEqual(metadata["audio_codecs"], ["aac"])
            self.assertAlmostEqual(metadata["width"] / metadata["height"], 9 / 16)
            for path in (thumbnail, preview):
                subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
                               check=True, capture_output=True)
            self.assertEqual(cache.preview_path(preview), str(preview))
            source.write_bytes(b"bad")
            with self.assertRaises(ValueError):
                cache.preview(asset["id"])
            self.assertEqual(len(list(cache.directory.glob("*.mp4"))), 1)

    def test_eviction_keeps_recent_thumbnail_and_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            cache = MediaCache(store, max_bytes=100)
            old = cache.directory / "old.jpg"
            new = cache.directory / "new.jpg"
            old.write_bytes(b"a" * 80)
            new.write_bytes(b"b" * 80)
            cache._prune(new)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            with self.assertRaises(FileNotFoundError):
                cache.preview_path(Path(directory) / "missing.mp4")


if __name__ == "__main__":
    unittest.main()
