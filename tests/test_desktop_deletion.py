"""ID-scoped deletion, exact Trash moves, partial failure and queue/scan ownership."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pycut.desktop.library import LibraryIndexer
from pycut.desktop.service import Service
from pycut.desktop.trash import move_to_trash


class DesktopDeletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.service = Service(self.base / "data", start_jobs=False)
        self.addCleanup(self.service.close)
        self.store, self.jobs = self.service.store, self.service.jobs
        self.project = self.store.create_project("Delete test", str(self.base / "finished"))
        self.library = self.store.libraries(self.project["id"])[1]
        self.root = self.base / "sources"
        self.root.mkdir()
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"original input")
        self.store.add_roots(self.library["id"], [str(self.root)])
        self.scan()
        self.asset = self.store.assets(self.library["id"])["items"][0]
        self.bin = self.base / "Trash"
        self.bin.mkdir()
        self.moves = []
        def recycle(path):
            destination = self.bin / (str(len(self.moves)) + "-" + path.name)
            shutil.move(str(path), str(destination))
            self.moves.append((path, destination))
            return str(destination)
        self.recycle = recycle
        self.native = patch("pycut.desktop.deletion.move_to_trash", side_effect=recycle)
        self.native.start()
        self.addCleanup(self.native.stop)

    def scan(self):
        with patch("pycut.desktop.library.probe_metadata", return_value={"duration": 2, "width": 480, "height": 854}), patch(
                "pycut.desktop.library.tool_identity", return_value="test"):
            self.service.indexer.scan(self.library["id"])

    def job(self, *, legacy=False):
        job = self.jobs.submit({"project_id": self.project["id"], "kind": "dedup", "library_id": self.library["id"]})
        self.jobs._update(job["id"], status="completed", completed=1)
        if legacy:
            with self.store.connect() as db:
                request = json.loads(db.execute("SELECT request_json FROM desktop_jobs WHERE id=?", (job["id"],)).fetchone()[0])
                request.pop("batch_dir")
                db.execute("UPDATE desktop_jobs SET request_json=? WHERE id=?", (json.dumps(request), job["id"]))
        return self.jobs.detail(job["id"])

    def publish(self, job, name="final.mp4", parent=None):
        path = (parent or Path(job["batch_dir"])) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"published video")
        identifier = job["id"] + name
        self.store.record_output(identifier, self.project["id"], job["id"], str(path),
                                 [{"asset_id": self.asset["id"], "role": "background"}])
        with self.store.connect() as db:
            db.execute("UPDATE desktop_items SET status='completed',output_id=? WHERE job_id=?",
                       (identifier, job["id"]))
        return identifier, path

    def target(self, job, kind="job"):
        return {"kind": kind, "job_id": job["id"], "project_id": self.project["id"]}

    def execute(self, target):
        return self.service.dispatch("trash.execute", {"target": target})

    def test_delete_job_trashes_entire_owned_folder_and_cascades_only_its_records(self):
        job = self.job()
        identifier, path = self.publish(job)
        (path.parent / "note.txt").write_text("folder content")
        plan = self.service.dispatch("trash.preview", {"target": self.target(job)})
        self.assertEqual(plan["paths"], [str(path.parent)])
        self.assertTrue(path.exists())
        result = self.execute(self.target(job))
        self.assertTrue(result["deleted_job"])
        self.assertFalse(path.parent.exists())
        self.assertEqual((self.moves[0][1] / "note.txt").read_text(), "folder content")
        self.assertTrue(self.source.exists())
        self.assertEqual(self.store.outputs(self.project["id"]), [])
        with self.assertRaises(ValueError):
            self.jobs.detail(job["id"])
        self.assertEqual(self.store.asset_detail(self.asset["id"])["usage_count"], 0)

    def test_delete_one_output_keeps_other_video_folder_and_task(self):
        job = self.job()
        first, path = self.publish(job)
        second, retained = self.publish(job, "second.mp4")
        result = self.execute({"kind": "outputs", "project_id": self.project["id"], "ids": [first]})
        self.assertFalse(result["deleted_job"])
        self.assertFalse(path.exists())
        self.assertTrue(retained.exists())
        self.assertEqual([row["id"] for row in self.jobs.detail(job["id"])["outputs"]], [second])

    def test_last_video_deletion_keeps_empty_folder_available_until_folder_is_trashed(self):
        job = self.job()
        output, path = self.publish(job)
        self.execute({"kind": "outputs", "project_id": self.project["id"], "ids": [output]})
        self.assertTrue(self.jobs.detail(job["id"])["batch_available"])
        self.assertTrue(path.parent.is_dir())
        self.execute(self.target(job, "batch"))
        self.assertFalse(self.jobs.detail(job["id"])["batch_available"])
        self.assertFalse(path.parent.exists())

    def test_new_submission_waits_for_trash_then_reads_updated_membership(self):
        entered, release, submitted, finished = (threading.Event() for _ in range(4))
        errors = []
        target = {"kind": "assets", "library_id": self.library["id"], "ids": [self.asset["id"]]}
        def recycle(path):
            entered.set()
            release.wait(3)
            return self.recycle(path)
        def submit():
            submitted.set()
            try:
                self.jobs.submit({"kind": "dedup", "project_id": self.project["id"], "library_id": self.library["id"]})
            except ValueError as error:
                errors.append(str(error))
            finished.set()
        with patch("pycut.desktop.deletion.move_to_trash", side_effect=recycle):
            deleting = threading.Thread(target=lambda: self.execute(target))
            deleting.start()
            self.assertTrue(entered.wait(2))
            enqueuing = threading.Thread(target=submit)
            enqueuing.start()
            self.assertTrue(submitted.wait(2))
            try:
                self.assertFalse(finished.wait(.1))
            finally:
                release.set()
                deleting.join(3)
                enqueuing.join(3)
        self.assertTrue(finished.is_set())
        self.assertTrue(errors)
        self.assertEqual(self.jobs.list(self.project["id"]), [])

    def test_delete_batch_keeps_task_and_marks_published_items_deleted(self):
        job = self.job()
        self.publish(job)
        self.execute(self.target(job, "batch"))
        detail = self.jobs.detail(job["id"])
        self.assertEqual(detail["outputs"], [])
        self.assertEqual(detail["items"][0]["status"], "deleted")
        self.assertTrue(self.source.exists())

    def test_legacy_shared_directory_retains_other_jobs_and_unrelated_files(self):
        first, second = self.job(legacy=True), self.job(legacy=True)
        shared = self.base / "shared"
        _, removed = self.publish(first, "first.mp4", shared)
        _, retained = self.publish(second, "second.mp4", shared)
        note = shared / "my-document.txt"
        note.write_text("keep")
        result = self.execute(self.target(first))
        self.assertTrue(result["deleted_job"])
        self.assertFalse(removed.exists())
        self.assertTrue(retained.exists())
        self.assertTrue(note.exists())
        self.assertTrue(self.jobs.detail(second["id"])["outputs"])

    def test_source_deletion_removes_all_memberships_but_keeps_usage_history(self):
        other = self.store.create_project("Other")
        library = self.store.libraries(other["id"])[1]
        self.store.add_roots(library["id"], [str(self.root)])
        with patch("pycut.desktop.library.probe_metadata", return_value={"duration": 2}), patch(
                "pycut.desktop.library.tool_identity", return_value="test"):
            LibraryIndexer(self.store).scan(library["id"])
        job = self.job()
        self.publish(job)
        self.execute({"kind": "assets", "library_id": self.library["id"], "ids": [self.asset["id"]]})
        self.assertFalse(self.source.exists())
        self.assertEqual(self.store.assets(self.library["id"])["total"], 0)
        self.assertEqual(self.store.assets(library["id"])["total"], 0)
        self.assertFalse(self.store.asset_detail(self.asset["id"])["available"])
        self.assertEqual(self.store.asset_detail(self.asset["id"])["usage_count"], 1)
        self.scan()
        self.assertEqual(self.store.assets(self.library["id"])["total"], 0)
        self.moves[0][1].replace(self.source)
        self.scan()
        self.assertEqual(self.store.assets(self.library["id"])["total"], 1)

    def test_partial_failure_keeps_job_and_failed_records_for_retry(self):
        job = self.job(legacy=True)
        first, path = self.publish(job, "first.mp4", self.base / "shared")
        second, retained = self.publish(job, "second.mp4", path.parent)
        def partial(path):
            if path == retained:
                raise PermissionError("test denied")
            return self.recycle(path)
        with patch("pycut.desktop.deletion.move_to_trash", side_effect=partial):
            result = self.execute(self.target(job))
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(result["deleted_job"])
        self.assertFalse(path.exists())
        self.assertTrue(retained.exists())
        self.assertEqual([row["id"] for row in self.jobs.detail(job["id"])["outputs"]], [second])
        self.assertTrue(self.execute(self.target(job))["deleted_job"])

    def test_running_and_queued_input_users_block_deletion_without_moving_files(self):
        job = self.jobs.submit({"kind": "dedup", "project_id": self.project["id"], "library_id": self.library["id"]})
        target = {"kind": "assets", "library_id": self.library["id"], "ids": [self.asset["id"]]}
        for status in ("queued", "running", "cancelling"):
            self.jobs._update(job["id"], status=status)
            with self.assertRaisesRegex(ValueError, "占用"):
                self.execute(target)
        self.jobs._update(job["id"], status="completed")
        self.jobs._active = job["id"]
        with self.assertRaisesRegex(ValueError, "先停止"):
            self.execute(self.target(job))
        self.jobs._active = None
        self.assertEqual(self.moves, [])
        self.assertTrue(self.source.exists())

    def test_legacy_output_owner_must_finish_even_without_an_explicit_output_root(self):
        job = self.job(legacy=True)
        output, path = self.publish(job, parent=self.base / "shared")
        self.jobs._update(job["id"], status="running")
        with self.assertRaisesRegex(ValueError, "先停止"):
            self.execute({"kind": "outputs", "project_id": self.project["id"], "ids": [output]})
        self.assertTrue(path.exists())
        self.assertEqual(self.moves, [])
        self.jobs._update(job["id"], status="queued")
        with self.assertRaisesRegex(ValueError, "先停止"):
            self.execute(self.target(job, "batch"))

    def test_delete_job_removes_recovery_journals_and_never_resurrects_outputs(self):
        job = self.job()
        output, path = self.publish(job)
        journal = self.store.data_dir / "publications" / "pending.json"
        journal.parent.mkdir(exist_ok=True)
        journal.write_text(json.dumps({"job_id": job["id"]}))
        self.execute(self.target(job))
        self.assertFalse(journal.exists())
        self.moves[0][1].replace(path.parent)
        from pycut.desktop.jobs import JobManager
        restarted = JobManager(self.store, lambda event: None, start=False)
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted.list(self.project["id"]), [])
        self.assertEqual(self.store.outputs(self.project["id"]), [])

    def test_delete_queued_task_with_no_created_folder(self):
        job = self.jobs.submit({"kind": "dedup", "project_id": self.project["id"], "library_id": self.library["id"]})
        self.assertTrue(self.execute(self.target(job))["deleted_job"])
        self.assertEqual(self.moves, [])
        self.assertTrue(self.source.exists())

    def test_foreign_ids_invalid_selection_and_replaced_directory_never_move_files(self):
        job = self.job()
        output, path = self.publish(job)
        other = self.store.create_project("Other")
        for target in ({"kind": "outputs", "project_id": other["id"], "ids": [output]},
                       {**self.target(job), "project_id": other["id"]},
                       {"kind": "assets", "library_id": self.store.libraries(other["id"])[1]["id"], "ids": [self.asset["id"]]},
                       {"kind": "outputs", "project_id": self.project["id"], "ids": []},
                       {"kind": "path", "path": str(self.source)}):
            with self.assertRaises(ValueError):
                self.execute(target)
        actual = path.parent.with_name("actual")
        path.parent.rename(actual)
        path.parent.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "软链接"):
            self.execute(self.target(job))
        self.assertEqual(self.moves, [])

    def test_confirmed_missing_file_cleans_records_but_offline_directory_does_not(self):
        job = self.job()
        identifier, path = self.publish(job)
        path.rename(path.with_suffix(".elsewhere"))
        result = self.execute({"kind": "outputs", "project_id": self.project["id"], "ids": [identifier]})
        self.assertEqual(result["missing"], [str(path)])
        second, path = self.publish(job, "offline.mp4")
        path.parent.parent.rename(path.parent.parent.with_name("disconnected"))
        with self.assertRaisesRegex(ValueError, "目录不可用"):
            self.execute(self.target(job))
        self.assertEqual(self.jobs.detail(job["id"])["outputs"][0]["id"], second)


@unittest.skipUnless(sys.platform == "darwin", "macOS Trash API requires a Mac")
class NativeTrashTests(unittest.TestCase):
    def test_native_trash_preserves_unicode_files_folders_and_leaf_symlink_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            file = root / 'pycut-废纸篓 "原始".txt'
            file.write_text("recoverable file")
            destination = Path(move_to_trash(file))
            self.assertFalse(file.exists())
            self.assertEqual(destination.read_text(), "recoverable file")
            destination.replace(file)
            folder = root / "pycut-废纸篓-folder"
            folder.mkdir()
            (folder / "nested.txt").write_text("recoverable folder")
            destination = Path(move_to_trash(folder))
            self.assertEqual((destination / "nested.txt").read_text(), "recoverable folder")
            destination.replace(folder)
            link = root / "pycut-废纸篓-link"
            link.symlink_to(file)
            destination = Path(move_to_trash(link))
            self.assertTrue(destination.is_symlink())
            self.assertTrue(file.exists())
            destination.replace(link)
