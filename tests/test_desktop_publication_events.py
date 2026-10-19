"""Publication/event integration with real files and SQLite.

Only codec/probe/model responses are mocked. Command orchestration, staged
replacement, callbacks, item/output association and recovery are real. These
tests establish publication accounting, not media quality or model accuracy.
"""
from contextlib import contextmanager, ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycut.core.ffmpeg import VideoInfo
from pycut.core.output_paths import flat_output
from pycut.core.runtime import STOP
from pycut.desktop import engine
from pycut.desktop.jobs import JobManager
from pycut.desktop.library import LibraryIndexer
from pycut.desktop.store import Store


class VisionClient:
    calls = failures = 0
    switched_to = None
    enabled = True

    def close(self):
        pass


class DesktopPublicationTests(unittest.TestCase):
    def setUp(self):
        STOP.flag = False
        self.addCleanup(setattr, STOP, "flag", False)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.store = Store(self.base / "data")
        self.output = self.base / "output"
        self.output.mkdir()
        self.source_root = self.base / "input"
        self.source_root.mkdir()
        self.project = self.store.create_project("Publication test", str(self.output))
        self.libraries = {item["role"]: item for item in self.store.libraries(self.project["id"])}
        self.store.save_settings({"vision_provider": "codex", "vision_model": "selected-model"})
        self.asset = self.add_source("first.mp4")
        self.manager = JobManager(self.store, lambda event: None, start=False)
        self.addCleanup(self.manager.shutdown)

    def index(self, library):
        self.store.add_roots(library["id"], [str(self.source_root)])
        with patch("pycut.desktop.library.probe_metadata", return_value={
                "duration": 2.0, "width": 720, "height": 1280}), patch(
                "pycut.desktop.library.tool_identity", return_value="publication-test-probe"):
            LibraryIndexer(self.store).scan(library["id"])

    def add_source(self, name):
        path = self.source_root / name
        path.write_bytes(b"source:" + name.encode())
        self.index(self.libraries["background"])
        return next(item for item in self.store.assets(self.libraries["background"]["id"])["items"]
                    if item["path"] == str(path))

    def submit(self, kind, **options):
        job = self.manager.submit({"kind": kind, "project_id": self.project["id"],
            "library_id": self.libraries["background"]["id"],
            "options": {"out_dir": str(self.output), "workers": 1, **options}})
        self.manager._update(job["id"], status="running")
        return job

    def run_job(self, job):
        with self.store.connect() as db:
            request = json.loads(db.execute("SELECT request_json FROM desktop_jobs WHERE id=?", (job["id"],)).fetchone()[0])
        def consume(event, payload):
            self.manager._line(job["id"], "@pycut-desktop " + json.dumps({"event": event, "payload": payload}))
        with patch.dict(os.environ), patch("sys.stdin", io.StringIO()), patch(
                "pycut.core.runtime.install_signal_handlers"), patch.object(engine, "emit", side_effect=consume), redirect_stdout(io.StringIO()):
            result = engine.run_job(request, self.store.data_dir, self.store)
            consume("result", result)
        return self.manager.detail(job["id"])

    @contextmanager
    def cover_responses(self, status):
        with patch("pycut.commands.cover_cmd.self_check"), patch(
                "pycut.commands.cover_cmd.verified_clip", return_value=False), patch(
                "pycut.commands.cover_cmd.process_clip", return_value=status), patch(
                "pycut.desktop.local_vision.make_local_vision", return_value=VisionClient()):
            yield

    @contextmanager
    def dedup_responses(self, fail_encoding=False):
        def probe(path):
            return VideoInfo(path=path, stem=path.stem, duration=2.0, width=720, height=1280,
                             has_audio=False, rotation=0, bitrate="5M")
        def encode(command, timeout):
            if fail_encoding:
                return 1, "", "encoder failed before publication"
            Path(command[-1]).write_bytes(b"new below-gate output" * 100)
            return 0, "", ""
        with ExitStack() as stack:
            for name, values in {
                "pycut.commands.dedup_cmd.self_check": {},
                "pycut.core.dedup.probe_video": {"side_effect": probe},
                "pycut.core.dedup._video_duration": {"return_value": 2.0},
                "pycut.core.dedup._probe_size": {"return_value": (720,1280)},
                "pycut.core.dedup.compare": {"return_value": {"phash_dist": 0.0, "psnr": 40.0}},
                "pycut.core.dedup.run_cmd": {"side_effect": encode},
            }.items():
                stack.enter_context(patch(name, **values))
            yield

    def test_uncertain_cover_preserves_previous_file_without_registering_new_output(self):
        target = flat_output(Path(self.asset["path"]), self.output)
        target.write_bytes(b"previous published output")
        identity = engine.signature(target)
        job = self.submit("cover")
        with self.cover_responses("review"):
            detail = self.run_job(job)
        self.assertEqual(engine.signature(target), identity)
        self.assertEqual(detail["status"], "review")
        self.assertEqual(detail["review"], 1)
        self.assertEqual(detail["outputs"], [])
        self.assertEqual(detail["items"][0]["status"], "review")
        self.assertFalse(detail["items"][0]["output_id"])

    def test_published_below_gate_dedup_is_registered_for_review_and_linked_to_its_item(self):
        job = self.submit("dedup", no_report=True)
        with self.dedup_responses():
            detail = self.run_job(job)
        self.assertEqual((detail["status"],detail["review"],detail["failed"]), ("review",1,0))
        self.assertEqual(len(detail["outputs"]), 1)
        output, item = detail["outputs"][0], detail["items"][0]
        self.assertEqual((item["id"],item["status"],item["output_id"]), (self.asset["id"],"review",output["id"]))
        self.assertEqual(Path(output["path"]).read_bytes(), b"new below-gate output" * 100)
        self.assertEqual(self.manager.detail(job["id"])["items"][0]["output_id"], output["id"])

    def test_failed_dedup_does_not_register_an_existing_output(self):
        target = flat_output(Path(self.asset["path"]), self.output)
        target.write_bytes(b"previous published output")
        identity = engine.signature(target)
        job = self.submit("dedup", force=True, no_report=True)
        with self.dedup_responses(fail_encoding=True):
            detail = self.run_job(job)
        self.assertEqual(engine.signature(target), identity)
        self.assertEqual((detail["status"],detail["failed"]), ("failed",1))
        self.assertEqual(detail["outputs"], [])
        self.assertFalse(detail["items"][0]["output_id"])

    def test_published_cover_association_survives_cancellation_and_restart(self):
        second = self.add_source("second.mp4")
        job = self.submit("cover")
        original_record = self.store.record_output
        def publish_then_cancel(*args, **kwargs):
            output_id = original_record(*args, **kwargs)
            STOP.flag = True
            return output_id
        with self.cover_responses("nosub"), patch.object(self.store, "record_output", side_effect=publish_then_cancel):
            detail = self.run_job(job)
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(len(detail["outputs"]), 1)
        output_id = detail["outputs"][0]["id"]
        items = {item["id"]: item for item in detail["items"]}
        self.assertEqual((items[self.asset["id"]]["status"],items[self.asset["id"]]["output_id"]), ("completed",output_id))
        self.assertEqual(items[second["id"]]["status"], "cancelled")
        self.assertFalse(items[second["id"]]["output_id"])
        restarted = JobManager(self.store, lambda event: None, start=False)
        try:
            restored = restarted.detail(job["id"])
            self.assertEqual(restored["items"], detail["items"])
            self.assertEqual([output["id"] for output in restored["outputs"]], [output_id])
        finally:
            restarted.shutdown()

    def test_prepared_publication_recovers_exact_item_output_once(self):
        self.index(self.libraries["reference"])
        job = self.manager.submit({"kind": "montage", "project_id": self.project["id"],
            "reference_ids": [self.asset["id"]], "background_library_id": self.libraries["background"]["id"],
            "options": {"fission": 2, "no_pip": True, "no_sticker": True}})
        self.manager._update(job["id"], status="running")
        target = self.output / "published_1.mp4"
        target.write_bytes(b"atomically published video")
        item_id = job["items"][0]["id"]
        journal = self.store.data_dir / "publications" / "recovery.json"
        engine._atomic_json(journal, {"id": "recovered-output", "project_id": self.project["id"],
            "job_id": job["id"], "item_id": item_id, "path": str(target),
            "identity": engine.publication_identity(target), "uses": []})
        self.assertEqual(len(engine.recover_publications(self.store, self.store.data_dir)), 1)
        self.assertFalse(journal.exists())
        self.assertEqual(engine.recover_publications(self.store, self.store.data_dir), [])
        restarted = JobManager(self.store, lambda event: None, start=False)
        try:
            detail = restarted.detail(job["id"])
            self.assertEqual((detail["status"],detail["completed"]), ("interrupted",1))
            first, second = detail["items"]
            self.assertEqual((first["id"],first["status"],first["output_id"]), (item_id,"completed","recovered-output"))
            self.assertEqual(second["status"], "interrupted")
            self.assertFalse(second["output_id"])
            self.assertEqual([output["id"] for output in detail["outputs"]], ["recovered-output"])
        finally:
            restarted.shutdown()


if __name__ == "__main__":
    unittest.main()
