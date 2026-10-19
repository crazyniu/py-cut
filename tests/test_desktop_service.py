"""Desktop RPC error boundaries and indexed-only media."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycut.desktop.service import Service


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.service = Service(self.base / "data", start_jobs=False)
        self.addCleanup(self.service.close)
        self.project = self.service.dispatch("projects.create", {"name": "产品"})

    def test_explicit_rpc_and_bad_requests_leave_service_usable(self):
        for params in (None, [], "text"):
            with self.assertRaises(ValueError):
                self.service.dispatch("projects.list", params)
        for method in ("store.connect", "__dict__", "subprocess.run", "unknown"):
            with self.assertRaises(ValueError):
                self.service.dispatch(method, {})
        with self.assertRaises(ValueError):
            self.service.dispatch("projects.update", {"project_id": self.project["id"], "unexpected": True})
        self.assertTrue(self.service.dispatch("app.ping", {})["ready"])
        self.assertEqual(self.service.dispatch("projects.list", {})[0]["id"], self.project["id"])

    def test_library_dtos_expose_paths_and_status_separately(self):
        library = self.service.dispatch("libraries.list", {"project_id": self.project["id"]})[1]
        root = self.base / "素材"
        root.mkdir()
        self.service.dispatch("libraries.add_roots", {"library_id": library["id"], "roots": [str(root)]})
        result = self.service.dispatch("libraries.list", {"project_id": self.project["id"]})[1]
        self.assertEqual(result["roots"], [str(root)])
        self.assertEqual(result["root_status"][0]["path"], str(root))

    def test_settings_allow_local_agents_and_never_accept_gateway_credentials(self):
        self.assertEqual(self.service.dispatch("settings.get", {})["vision_provider"], "codex")
        result = self.service.dispatch("settings.save", {"values": {"vision_provider": "codex", "vision_model": "selected-model"}})
        self.assertEqual(result["vision_model"], "selected-model")
        for values in ({"llm_key": "not-stored"}, {"api_key": "not-stored"}, {"vision_provider": "remote"}, {"vision_provider": "claude"}):
            with self.assertRaises(ValueError):
                self.service.dispatch("settings.save", {"values": values})
        self.assertNotIn("llm_key", self.service.dispatch("settings.get", {}))

    def test_media_output_preview_requires_recorded_output_id(self):
        with patch.object(self.service.media, "preview_path") as preview:
            with self.assertRaises(ValueError):
                self.service.dispatch("media.output_preview", {"output_id": "outside", "path": "/arbitrary.mp4"})
            preview.assert_not_called()
            output = self.base / "成片.mp4"
            output.write_bytes(b"published")
            self.service.store.record_output("known", self.project["id"], "job", str(output), [])
            preview.return_value = str(output)
            result = self.service.dispatch("media.output_preview", {"output_id": "known"})
            self.assertEqual(result, {"path": str(output)})
            preview.assert_called_once_with(output)

    def test_output_thumbnails_require_recorded_output(self):
        with patch.object(self.service.media, "thumbnail_path", return_value="thumb.jpg") as thumbnail:
            with self.assertRaises(ValueError):
                self.service.dispatch("media.output_thumbnail", {"output_id": "outside", "path": "/arbitrary.mp4"})
            thumbnail.assert_not_called()
            output = self.base / "published.mp4"
            output.write_bytes(b"published")
            self.service.store.record_output("known", self.project["id"], "job", str(output), [])
            self.assertEqual(self.service.dispatch("media.output_thumbnail", {"output_id": "known"}), {"path": "thumb.jpg"})
            thumbnail.assert_called_once_with(output)

    def test_legacy_import_is_no_longer_a_desktop_entry(self):
        with self.assertRaisesRegex(ValueError, "未知桌面接口"):
            self.service.dispatch("settings.import_legacy", {})

    def test_shutdown_rejects_new_requests(self):
        self.assertEqual(self.service.dispatch("app.shutdown", {}), {"closing": True})
        with self.assertRaises(RuntimeError):
            self.service.dispatch("projects.create", {"name": "too late"})


if __name__ == "__main__":
    unittest.main()
