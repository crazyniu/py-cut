"""Desktop RPC error boundaries, indexed-only media, and directory-only migration."""
from __future__ import annotations

import json
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
        result = self.service.dispatch("settings.save", {"values": {"vision_provider": "pi", "agent_path": "C:/bin/pi.cmd"}})
        self.assertEqual(result["vision_provider"], "pi")
        for values in ({"llm_key": "not-stored"}, {"api_key": "not-stored"}, {"vision_provider": "remote"}):
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

    def test_legacy_import_multiple_relative_dirs_without_credentials_or_file_rewrite(self):
        legacy = self.base / "old-project"
        legacy.mkdir()
        for name in ("话术", "片段一", "片段二", "画中画", "前贴", "贴纸"):
            (legacy / name).mkdir()
        content = ("PYCUT_REFERENCE_DIR=话术\n"
                   + "PYCUT_CLIP_DIR='" + json.dumps(["片段一", "片段二"], ensure_ascii=False) + "'\n"
                   + 'PYCUT_PIP_DIR="画中画"\nPYCUT_INTRO_DIR=前贴\nPYCUT_STICKER_DIR=贴纸\n'
                   + "PYCUT_OUTPUT_DIR=成片\nPYCUT_MAIN_KEY=private-legacy-value\n"
                   + "PYCUT_BACKUP_KEY=private-backup-value\nPYCUT_MAIN_MODEL=legacy-model\n")
        source = legacy / ".env"
        source.write_text(content)
        result = self.service.dispatch("settings.import_legacy", {"path": str(source), "project_id": self.project["id"]})
        self.assertEqual(result["imported_directories"], 6)
        libraries = {item["role"]: item for item in self.service.dispatch("libraries.list", {"project_id": self.project["id"]})}
        self.assertEqual(libraries["background"]["roots"], [str(legacy / "片段一"), str(legacy / "片段二")])
        self.assertEqual(self.service.store.projects()[0]["output_dir"], str(legacy / "成片"))
        self.assertEqual(source.read_text(), content)
        self.assertEqual(self.service.store.settings(), {})
        exposed = json.dumps([result, libraries, self.service.store.projects(), self.service.dispatch("settings.get", {})])
        self.assertNotIn("private-legacy-value", exposed)
        self.assertNotIn("private-backup-value", exposed)
        self.assertNotIn("legacy-model", exposed)
        self.service.dispatch("settings.import_legacy", {"path": str(source), "project_id": self.project["id"]})
        self.assertEqual(len(self.service.dispatch("libraries.list", {"project_id": self.project["id"]})[1]["roots"]), 2)

    def test_invalid_legacy_directory_shape_and_missing_project_raise(self):
        source = self.base / ".env"
        source.write_text("PYCUT_CLIP_DIR='[42]'\n")
        with self.assertRaises(ValueError):
            self.service.dispatch("settings.import_legacy", {"path": str(source), "project_id": self.project["id"]})
        source.write_text("PYCUT_CLIP_DIR=素材\n")
        with self.assertRaises(ValueError):
            self.service.dispatch("settings.import_legacy", {"path": str(source)})

    def test_shutdown_rejects_new_requests(self):
        self.assertEqual(self.service.dispatch("app.shutdown", {}), {"closing": True})
        with self.assertRaises(RuntimeError):
            self.service.dispatch("projects.create", {"name": "too late"})


if __name__ == "__main__":
    unittest.main()
