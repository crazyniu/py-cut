"""Desktop task boundaries: frozen inputs, serial execution, and publish-only usage.

Mock children exercise the queue protocol only; catalog and publication tests use
real files and SQLite. These tests do not claim codec quality or Agent accuracy.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pycut.core.dedup_profiles import apply_account_profile
from pycut.desktop.engine import (check_snapshot, option_specs, parse_options,
                                  publication_identity, recover_publications, signature, _montage)
from pycut.desktop.jobs import JobManager
from pycut.desktop.library import LibraryIndexer
from pycut.desktop.store import Store


class DesktopJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.store = Store(self.base / "app")
        self.project = self.store.create_project("产品", str(self.base / "output"))
        self.libraries = {row["role"]: row for row in self.store.libraries(self.project["id"])}
        self.root = self.base / "素材"
        self.root.mkdir()
        self.probe = patch("pycut.desktop.library.probe_metadata",
                           return_value={"duration": 3.0, "width": 1080, "height": 1920})
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.identity = patch("pycut.desktop.library.tool_identity", return_value="test-probe")
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.reference = self.add_asset("reference", "话术.mp4")
        self.background = self.add_asset("background", "片段.mp4")
        self.events = []
        self.manager = JobManager(self.store, self.events.append, start=False)
        self.addCleanup(self.manager.shutdown)

    def add_asset(self, role, name):
        root = self.root / role
        root.mkdir(exist_ok=True)
        path = root / name
        path.write_bytes(b"source:" + name.encode())
        library = self.libraries[role]
        self.store.add_roots(library["id"], [str(root)])
        LibraryIndexer(self.store).scan(library["id"])
        return next(item for item in self.store.assets(library["id"])["items"] if item["name"] == name)

    def request(self, **extra):
        return {"kind": "montage", "project_id": self.project["id"],
                "reference_ids": [self.reference["id"]],
                "background_library_id": self.libraries["background"]["id"],
                "options": {"no_pip": True, "no_sticker": True}, **extra}

    def stored_request(self, identifier):
        with self.store.connect() as db:
            row = db.execute("SELECT request_json FROM desktop_jobs WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0])

    def test_removed_material_excluded_and_submitted_job_freezes_membership(self):
        removed = self.add_asset("background", "不用.mp4")
        self.store.set_removed(self.libraries["background"]["id"], [removed["id"]], True)
        job = self.manager.submit(self.request())
        frozen = self.stored_request(job["id"])
        self.assertEqual([item["id"] for item in frozen["pools"]["background"]], [self.background["id"]])
        self.store.set_removed(self.libraries["background"]["id"], [self.background["id"]], True)
        self.add_asset("background", "后来添加.mp4")
        self.assertEqual(self.stored_request(job["id"]), frozen)
        check_snapshot(frozen)
        Path(self.background["path"]).write_bytes(b"modified after submit")
        with self.assertRaisesRegex(ValueError, "素材在任务创建后发生变化"):
            check_snapshot(frozen)

    def test_project_ids_cannot_select_foreign_references_or_libraries(self):
        other = self.store.create_project("另一个产品")
        libraries = {item["role"]: item for item in self.store.libraries(other["id"])}
        self.store.add_roots(libraries["reference"]["id"], [str(Path(self.reference["path"]).parent)])
        self.store.add_roots(libraries["background"]["id"], [str(Path(self.background["path"]).parent)])
        for role in ("reference", "background"):
            LibraryIndexer(self.store).scan(libraries[role]["id"])
        with self.assertRaisesRegex(ValueError, "当前项目"):
            self.manager.submit(self.request(background_library_id=libraries["background"]["id"]))
        self.store.set_removed(self.libraries["reference"]["id"], [self.reference["id"]], True)
        with self.assertRaisesRegex(ValueError, "话术已移除"):
            self.manager.submit(self.request())
        with self.assertRaisesRegex(ValueError, "当前项目"):
            self.manager.submit({"kind": "cover", "project_id": self.project["id"],
                                 "library_id": libraries["background"]["id"]})

    def test_disabled_optional_libraries_are_never_read(self):
        job = self.manager.submit(self.request(pip_library_id="missing", intro_library_id="missing",
                                               sticker_library_id="missing"))
        frozen = self.stored_request(job["id"])
        for role in ("pip", "intro", "sticker"):
            self.assertEqual(frozen["pools"][role], [])
            self.assertEqual(frozen["roots"][role], [])
        with self.assertRaisesRegex(ValueError, "当前项目"):
            self.manager.submit(self.request(pip_library_id="missing", options={"use_pip": True}))

    def test_enabled_optional_pool_missing_root_surfaces_error(self):
        for role, options, filename in (("pip", {"no_pip": False}, "picture.mp4"),
                                        ("intro", {"intro_enabled": True}, "intro.mp4"),
                                        ("sticker", {"no_sticker": False}, "sticker.png")):
            with self.subTest(role=role):
                asset = self.add_asset(role, filename)
                Path(asset["path"]).unlink()
                Path(asset["path"]).parent.rmdir()
                with self.assertRaises((OSError, ValueError)):
                    self.manager.submit(self.request(**{role + "_library_id": self.libraries[role]["id"]},
                        options={"no_pip": True, "no_sticker": True, **options}))

    def test_removed_same_stem_subtitle_is_not_auto_selected(self):
        subtitle = self.add_asset("reference", "话术.srt")
        self.store.set_removed(self.libraries["reference"]["id"], [subtitle["id"]], True)
        frozen = self.manager._snapshot(self.request())
        self.assertEqual(frozen["references"][0]["subtitle_path"], "")
        self.assertNotIn(subtitle["path"], {item["path"] for item in frozen["snapshot"]})

    def test_disabled_subtitles_do_not_open_missing_srt(self):
        subtitle = self.add_asset("reference", "话术.srt")
        Path(subtitle["path"]).unlink()
        for option in ({"no_sub": True}, {"use_sub": False}):
            with self.subTest(option=option):
                frozen = self.manager._snapshot(self.request(options={"no_pip": True, "no_sticker": True, **option}))
                self.assertEqual(frozen["references"][0]["subtitle_path"], "")
                self.assertNotIn(subtitle["path"], {item["path"] for item in frozen["snapshot"]})

    def test_options_keep_existing_command_defaults_and_validate(self):
        args = parse_options("montage", {})
        self.assertEqual((args.pip_slot, args.mirror_chance, args.speed_max), (2.5, .35, 1.5))
        self.assertEqual((args.no_pip, args.no_sub, args.no_audio), (False, False, False))
        self.assertEqual(args.fill_ratio, 1.0)
        self.assertEqual(parse_options("cover", {"samples": 5, "force": True}).samples, 5)
        with self.assertRaises(ValueError):
            parse_options("montage", {"no_pip": "false"})
        with self.assertRaises(ValueError):
            parse_options("dedup", {"preset": "not-a-preset"})
        specs = option_specs("cover")
        self.assertTrue(any(item["name"] == "samples" for item in specs))
        self.assertFalse({"llm_key", "llm_url", "llm_model", "gateway"} & {item["name"] for item in specs})
        for count in (0, 100, True):
            with self.assertRaises(ValueError):
                self.manager.submit(self.request(options={"fission": count}))

    def test_explicit_false_overrides_enabled_account_profile(self):
        profile = self.base / "profile.json"
        profile.write_text(json.dumps({"version": 1, "accounts": {"demo": {"mirror": True}}}))
        args = parse_options("dedup", {"profile_file": str(profile), "account": "demo", "mirror": False})
        apply_account_profile(args)
        self.assertFalse(args.mirror)

    def test_selected_cover_inputs_are_validated_and_queue_cancel_is_persistent(self):
        request = {"kind": "cover", "project_id": self.project["id"],
                   "library_id": self.libraries["background"]["id"], "asset_ids": [self.background["id"]]}
        job = self.manager.submit(request)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.manager.cancel(job["id"])["status"], "cancelled")
        other = JobManager(self.store, self.events.append, start=False)
        try:
            self.assertEqual(other.detail(job["id"])["status"], "cancelled")
        finally:
            other.shutdown()
        self.store.set_removed(self.libraries["background"]["id"], [self.background["id"]], True)
        with self.assertRaises(ValueError):
            self.manager.submit(request)

    def test_restarting_interrupts_running_jobs_but_keeps_queued_jobs(self):
        first = self.manager.submit(self.request())
        second = self.manager.submit(self.request())
        self.manager._update(first["id"], status="running")
        restarted = JobManager(self.store, self.events.append, start=False)
        try:
            self.assertEqual(restarted.detail(first["id"])["status"], "interrupted")
            self.assertEqual(restarted.detail(second["id"])["status"], "queued")
            self.assertEqual(restarted.detail(first["id"])["completed"], 0)
        finally:
            restarted.shutdown()

    def test_per_video_states_follow_events_and_cancellation_preserves_completed(self):
        job = self.manager.submit(self.request(options={"fission": 3, "no_pip": True, "no_sticker": True}))
        self.assertEqual([item["status"] for item in job["items"]], ["queued"] * 3)
        first, second = [item["id"] for item in job["items"][:2]]
        def item(identifier, state):
            self.manager._line(job["id"], '@pycut-desktop ' + json.dumps({
                "event": "item", "payload": {"id": identifier, "status": state}}))
        self.manager._update(job["id"], status="running")
        item(first, "running")
        item(first, "completed")
        item(second, "running")
        self.assertEqual(self.manager.detail(job["id"])["completed"], 0)
        self.manager._update(job["id"], status="cancelled", completed=1)
        states = [entry["status"] for entry in self.manager.detail(job["id"])["items"]]
        self.assertEqual(states, ["completed", "cancelled", "cancelled"])
        with self.assertRaises(ValueError):
            item(first, "invented")

    def test_real_progress_does_not_increase_final_counts_and_logs_are_sanitized(self):
        job = self.manager.submit(self.request())
        self.manager._line(job["id"], '@pycut-progress {"stage":"背景编码","completed":9,"total":10}')
        self.assertEqual(self.manager.detail(job["id"])["completed"], 0)
        with patch.dict(os.environ, {"PYCUT_MAIN_KEY": "private-test-key-12345"}):
            self.manager._line(job["id"], "failed key=private-test-key-12345")
        self.assertNotIn("private-test-key-12345", self.manager.detail(job["id"])["logs"][-1])
        self.assertEqual(self.manager.detail(job["id"])["progress"]["completed"], 9)

    def test_serial_queue_and_running_cancellation_protocol(self):
        first = self.manager.submit(self.request())
        second = self.manager.submit(self.request())
        started, cancelled = threading.Event(), threading.Event()
        processes = []
        class Input(io.StringIO):
            def write(self, value):
                if value == "cancel\n":
                    cancelled.set()
                return super().write(value)
        class Child:
            def __init__(self):
                self.stdin = Input()
                self.stdout = self.lines()
                self.release = threading.Event()
                processes.append(self)
                started.set()
            def lines(self):
                self.release.wait(3)
                yield '@pycut-desktop ' + json.dumps({"event": "result", "payload": {
                    "status": "cancelled" if cancelled.is_set() else "completed"}}) + '\n'
            def wait(self, timeout=None):
                return 130 if cancelled.is_set() else 0
            def poll(self):
                return None if not self.release.is_set() else self.wait()
            def kill(self):
                self.release.set()
        with patch("pycut.desktop.jobs.subprocess.Popen", side_effect=lambda *a, **k: Child()):
            self.manager._thread.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(len(processes), 1)
                self.assertEqual(self.manager.detail(second["id"])["status"], "queued")
                self.assertEqual(self.manager.cancel(first["id"])["status"], "cancelling")
                self.assertTrue(cancelled.wait(1))
                self.manager.cancel(second["id"])
                processes[0].release.set()
                deadline = time.monotonic() + 2
                while self.manager.detail(first["id"])["status"] != "cancelled" and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.manager.detail(first["id"])["status"], "cancelled")
                self.assertEqual(len(processes), 1)
            finally:
                for process in processes:
                    process.release.set()
                self.manager.shutdown()

    def test_queue_starts_next_only_after_previous_child_finishes(self):
        first = self.manager.submit(self.request())
        second = self.manager.submit(self.request())
        started = [threading.Event(), threading.Event()]
        released = [threading.Event(), threading.Event()]
        processes = []
        class Child:
            def __init__(self):
                self.index = len(processes)
                self.stdin = io.StringIO()
                self.stdout = self.lines()
                processes.append(self)
                started[self.index].set()
            def lines(self):
                released[self.index].wait(3)
                yield '@pycut-desktop ' + json.dumps({"event": "result", "payload": {"status": "completed"}}) + '\n'
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0 if released[self.index].is_set() else None
            def kill(self):
                released[self.index].set()
        with patch("pycut.desktop.jobs.subprocess.Popen", side_effect=lambda *a, **k: Child()):
            self.manager._thread.start()
            try:
                self.assertTrue(started[0].wait(2))
                self.assertFalse(started[1].is_set())
                self.assertEqual(self.manager.detail(second["id"])["status"], "queued")
                released[0].set()
                self.assertTrue(started[1].wait(2))
                self.assertEqual(self.manager.detail(first["id"])["status"], "completed")
                self.assertEqual(self.manager.detail(second["id"])["status"], "running")
                released[1].set()
                deadline = time.monotonic() + 2
                while self.manager.detail(second["id"])["status"] != "completed" and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.manager.detail(second["id"])["status"], "completed")
                self.assertEqual(len(processes), 2)
            finally:
                for release in released:
                    release.set()
                self.manager.shutdown()

    def test_child_exit_without_result_is_failure(self):
        job = self.manager.submit(self.request())
        class Child:
            stdin = io.StringIO()
            stdout = iter(["encoder failed\n"])
            def wait(self, timeout=None):
                return 2
            def poll(self):
                return 2
        with patch("pycut.desktop.jobs.subprocess.Popen", return_value=Child()):
            self.manager._thread.start()
            try:
                deadline = time.monotonic() + 2
                while self.manager.detail(job["id"])["status"] != "failed" and time.monotonic() < deadline:
                    time.sleep(.01)
                detail = self.manager.detail(job["id"])
                self.assertEqual(detail["status"], "failed")
                self.assertEqual(detail["completed"], 0)
                self.assertIn("未返回完整结果", detail["error"])
                self.assertEqual(detail["outputs"], [])
            finally:
                self.manager.shutdown()

    def test_publication_uses_frozen_file_version_not_stale_ui_metadata(self):
        from pycut.core.runtime import reset_stop
        reset_stop()
        background = Path(self.background["path"])
        indexed_signature = self.store.asset_detail(self.background["id"])["signature"]
        background.write_bytes(b"changed-before-submitting-job")
        frozen = self.manager._snapshot(self.request())
        current_signature = signature(background)
        self.assertNotEqual(json.loads(indexed_signature), current_signature)
        def pipeline(options):
            final = options.out_dir / options.out_name
            staged = final.with_suffix(".staged.mp4")
            staged.write_bytes(b"complete-output")
            options.before_publish(final, staged, [{"path": str(background), "role": "background", "offset": 0, "duration": 1}])
            staged.replace(final)
            return {"final": str(final)}
        with patch("pycut.core.ffmpeg.self_check"), patch("pycut.commands.montage_pipeline.run_pipeline", side_effect=pipeline):
            result = _montage(frozen, self.store.data_dir, self.store)
        self.assertEqual(result["completed"], 1)
        recorded = self.store.asset_detail(self.background["id"], self.project["id"])["uses"][0]
        self.assertEqual(json.loads(recorded["signature"]), current_signature)

    def publication(self, identifier, path, *, identity=None):
        journal = self.store.data_dir / "publications" / (identifier + ".json")
        journal.parent.mkdir(exist_ok=True)
        record = {"id": identifier, "job_id": "job-publication", "project_id": self.project["id"],
                  "path": str(path), "identity": identity if identity is not None else publication_identity(path),
                  "uses": [{"asset_id": self.background["id"], "role": "background", "start": 0, "end": 1}]}
        journal.write_text(json.dumps(record))
        return journal, record

    def test_prepared_publication_recovers_once_after_atomic_replace(self):
        staged = self.base / "staged.mp4"
        staged.write_bytes(b"complete-video")
        final = self.base / "final.mp4"
        identity = publication_identity(staged)
        journal, record = self.publication("out-1", final, identity=identity)
        self.assertEqual(recover_publications(self.store, self.store.data_dir), [])
        self.assertTrue(journal.exists())
        staged.replace(final)
        self.assertEqual(len(recover_publications(self.store, self.store.data_dir)), 1)
        self.assertFalse(journal.exists())
        journal.write_text(json.dumps(record))
        self.assertEqual(len(recover_publications(self.store, self.store.data_dir)), 1)
        self.assertEqual(self.store.assets(self.libraries["background"]["id"])["items"][0]["usage_count"], 1)
        self.assertEqual(len(self.store.outputs(self.project["id"])), 1)

    def test_restart_recovery_reconciles_actual_published_count(self):
        job = self.manager.submit(self.request())
        self.manager._update(job["id"], status="running")
        output = self.base / "recovered.mp4"
        output.write_bytes(b"published-before-crash")
        journal, record = self.publication("recovered-output", output)
        record["job_id"] = job["id"]
        journal.write_text(json.dumps(record))
        restarted = JobManager(self.store, self.events.append, start=False)
        try:
            detail = restarted.detail(job["id"])
            self.assertEqual(detail["status"], "interrupted")
            self.assertEqual(len(detail["outputs"]), 1)
            self.assertEqual(detail["completed"], 1)
            self.assertEqual(detail["planned"], 1)
        finally:
            restarted.shutdown()

    def test_worker_failure_after_publication_reconciles_output_count(self):
        job = self.manager.submit(self.request())
        output = self.base / "published-before-worker-error.mp4"
        output.write_bytes(b"published-complete")
        journal, record = self.publication("worker-error-output", output)
        record["job_id"] = job["id"]
        journal.write_text(json.dumps(record))
        class Child:
            stdin = io.StringIO()
            stdout = iter(['@pycut-desktop ' + json.dumps({"event": "result", "payload": {
                "status": "failed", "error": "worker failed after publication"}}) + '\n'])
            def wait(self, timeout=None):
                return 2
            def poll(self):
                return 2
        with patch("pycut.desktop.jobs.subprocess.Popen", return_value=Child()):
            self.manager._thread.start()
            try:
                deadline = time.monotonic() + 2
                while len(self.manager.detail(job["id"])["outputs"]) != 1 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.manager._closed.set()
                self.manager._wake.set()
                self.manager._thread.join(timeout=2)
                detail = self.manager.detail(job["id"])
                self.assertEqual(detail["status"], "failed")
                self.assertEqual(len(detail["outputs"]), 1)
                self.assertEqual(detail["completed"], 1)
            finally:
                self.manager.shutdown()

    def test_unrelated_or_missing_files_never_recover_and_job_filter_applies(self):
        output = self.base / "unrelated.mp4"
        output.write_bytes(b"old-content")
        journal, record = self.publication("out-mismatch", output)
        output.unlink()
        self.assertEqual(recover_publications(self.store, self.store.data_dir), [])
        output.write_bytes(b"different-content")
        self.assertEqual(recover_publications(self.store, self.store.data_dir), [])
        self.assertTrue(journal.exists())
        other = self.base / "complete.mp4"
        other.write_bytes(b"complete")
        valid, _ = self.publication("out-other-job", other)
        self.assertEqual(recover_publications(self.store, self.store.data_dir, "another-job"), [])
        self.assertTrue(valid.exists())
        self.assertEqual(self.store.outputs(self.project["id"]), [])


if __name__ == "__main__":
    unittest.main()
