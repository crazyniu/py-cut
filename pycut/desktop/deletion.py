"""ID-scoped Trash operations coordinated with queue submission and library scans.

Only successful OS moves (or confirmed missing files in readable directories) remove
catalog records. Partial failures retain the job and failed paths for a later retry.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .trash import move_to_trash


def inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


class Deletions:
    def __init__(self, store, jobs, indexer, sink):
        self.store, self.jobs, self.indexer, self.sink = store, jobs, indexer, sink

    def _plan(self, target):
        if not isinstance(target, dict):
            raise ValueError("删除目标无效")
        kind = target.get("kind")
        with self.store.connect() as db:
            if kind == "assets":
                library = self.store.require(db, "libraries", target["library_id"])
                rows = []
                for identifier in self._ids(target):
                    row = db.execute("SELECT a.* FROM assets a JOIN memberships m ON m.asset_id=a.id "
                                     "WHERE a.id=? AND m.library_id=?", (identifier, library["id"])).fetchone()
                    if row is None:
                        raise ValueError("素材不属于当前素材库，请刷新后重试")
                    rows.append(dict(row))
                paths = [(Path(row["path"]), False) for row in rows]
            elif kind == "outputs":
                self.store.require(db, "projects", target["project_id"])
                rows = [self.store.require(db, "outputs", identifier) for identifier in self._ids(target)]
                if any(row["project_id"] != target["project_id"] for row in rows):
                    raise ValueError("成片不属于当前项目")
                for row in rows:
                    owner = db.execute("SELECT status FROM desktop_jobs WHERE id=?", (row["job_id"],)).fetchone()
                    if self.jobs._active == row["job_id"] or (owner and owner["status"] in ("queued", "running", "cancelling")):
                        raise ValueError("请先停止对应任务，等待处理结束后再删除")
                paths = [(Path(row["path"]), False) for row in rows]
            elif kind in ("batch", "job"):
                job = self.store.require(db, "desktop_jobs", target["job_id"])
                if job["project_id"] != target["project_id"]:
                    raise ValueError("任务不属于当前项目")
                if job["status"] in ("running", "cancelling") or (kind == "batch" and job["status"] == "queued") or self.jobs._active == job["id"]:
                    raise ValueError("请先停止此任务，等待处理结束后再删除")
                request = json.loads(job["request_json"])
                rows = db.execute("SELECT * FROM outputs WHERE job_id=?", (job["id"],)).fetchall()
                paths = [(Path(row["path"]), False) for row in rows]
                batch = Path(request["batch_dir"]) if request.get("batch_dir") else None
                owned = bool(batch and batch.name.endswith("-" + job["id"][:8]) and
                             batch.name.startswith(("混剪-", "字幕擦除-", "视频去重-")))
                # Earlier montage jobs already had unique folders; legacy cover/dedup
                # folders may be shared with other jobs or user files.
                if batch is None and paths and job["kind"] == "montage":
                    parents = {path.parent for path, _ in paths}
                    if len(parents) == 1:
                        candidate = parents.pop()
                        if re.fullmatch(r"混剪-\d{8}-\d{6}-[0-9a-f]{8}", candidate.name):
                            batch, owned = candidate, True
                if owned and all(inside(path, batch) for path, _ in paths):
                    other_outputs = db.execute("SELECT path FROM outputs WHERE job_id<>?", (job["id"],)).fetchall()
                    inputs = [Path(row["path"]) for row in request.get("snapshot", [])]
                    if not any(inside(Path(row[0]), batch) for row in other_outputs) and not any(
                            inside(path, batch) for path in inputs):
                        paths = [(batch, True)] if paths or os.path.lexists(batch) else []
            else:
                raise ValueError("未知删除类型")
        paths = list(dict.fromkeys(paths))
        for path, directory in paths:
            if not path.is_absolute() or path == Path(path.anchor):
                raise ValueError("删除路径无效")
            if any(parent.is_symlink() for parent in path.parents) or (directory and path.is_symlink()):
                raise ValueError("文件路径包含目录软链接，请先恢复实际目录")
        self._idle(paths, target.get("job_id") if kind == "job" else None)
        return paths

    @staticmethod
    def _ids(target):
        ids = target.get("ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError("请选择需要删除的文件")
        return list(dict.fromkeys(ids))

    def _idle(self, paths, deleting_job):
        with self.store.connect() as db:
            jobs = db.execute("SELECT * FROM desktop_jobs WHERE status IN ('queued','running','cancelling') OR id=?",
                              (self.jobs._active or "",)).fetchall()
        for job in jobs:
            if job["id"] == deleting_job and job["status"] == "queued" and self.jobs._active != deleting_job:
                continue
            request = json.loads(job["request_json"])
            protected = [Path(item["path"]) for item in request.get("snapshot", [])]
            output = request.get("batch_dir") or request.get("options", {}).get("out_dir")
            if output:
                protected.append(Path(output).expanduser().absolute())
            if any(inside(path, p) or inside(p, path) for path, _ in paths for p in protected):
                raise ValueError("文件正被处理中或排队的任务占用，请先停止相关任务")

    def preview(self, target):
        with self.jobs._lock, self.indexer._lock:
            paths = self._plan(target)
            return {"paths": [str(path) for path, _ in paths],
                    "shared_folder": target.get("kind") in ("batch", "job") and
                    any(not folder for _, folder in paths)}

    def _forget(self, path, directory):
        matches = lambda value: inside(Path(value), path) if directory else Path(value) == path
        with self.store.connect() as db:
            outputs = [row["id"] for row in db.execute("SELECT id,path FROM outputs") if matches(row["path"])]
            for identifier in outputs:
                db.execute("DELETE FROM usage WHERE output_id=?", (identifier,))
                db.execute("UPDATE desktop_items SET output_id='',status='deleted' WHERE output_id=?", (identifier,))
                db.execute("DELETE FROM outputs WHERE id=?", (identifier,))
            assets = [row["id"] for row in db.execute("SELECT id,path FROM assets") if matches(row["path"])]
            for identifier in assets:
                db.execute("DELETE FROM memberships WHERE asset_id=?", (identifier,))
                db.execute("DELETE FROM asset_roots WHERE asset_id=?", (identifier,))
                db.execute("UPDATE assets SET available=0,error='已移至废纸篓' WHERE id=?", (identifier,))
            for row in db.execute("SELECT id,srt_path FROM assets WHERE srt_path IS NOT NULL"):
                if matches(row["srt_path"]):
                    db.execute("UPDATE assets SET srt_path=NULL WHERE id=?", (row["id"],))

    def execute(self, target):
        with self.jobs._lock, self.indexer._lock:
            paths = self._plan(target)
            # Validate all addresses before moving the first file.
            for path, directory in paths:
                try:
                    mode = path.lstat().st_mode
                    if directory != stat.S_ISDIR(mode):
                        raise ValueError("所选文件或文件夹类型已改变，请刷新后重试")
                except FileNotFoundError:
                    if not path.parent.is_dir():
                        raise ValueError("所在目录不可用，文件和记录已保留")
            result = {"trashed": [], "missing": [], "errors": [], "deleted_job": False}
            for path, directory in paths:
                try:
                    if os.path.lexists(path):
                        location = move_to_trash(path)
                        result["trashed"].append({"path": str(path), "trash_path": location})
                    else:
                        result["missing"].append(str(path))
                    self._forget(path, directory)
                except OSError as error:
                    result["errors"].append({"path": str(path), "error": str(error)})
            if target["kind"] == "job" and not result["errors"]:
                identifier = target["job_id"]
                for journal in (self.store.data_dir / "publications").glob("*.json"):
                    if json.loads(journal.read_text())["job_id"] == identifier:
                        journal.unlink()
                with self.store.connect() as db:
                    db.execute("DELETE FROM desktop_logs WHERE job_id=?", (identifier,))
                    db.execute("DELETE FROM desktop_items WHERE job_id=?", (identifier,))
                    db.execute("DELETE FROM desktop_jobs WHERE id=?", (identifier,))
                result["deleted_job"] = True
            self.sink({"event": "library", "payload": {"deleted": True}})
            return result
