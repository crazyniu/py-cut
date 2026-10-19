"""Recursive material indexing without copying media or changing membership removals."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import uuid

from .. import config
from ..core.scan import VIDEO_EXTS, _is_system_dir
from .media import PROBE_ARGS, probe_metadata, tool_identity
from .store import Store, canonical, now, stamp

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def media_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix == ".srt":
        return "subtitle"
    return None


class LibraryIndexer:
    """Update UI metadata; offline media and errors remain visible in the catalog."""

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()

    @staticmethod
    def _walk(root: Path, errors: list[dict]):
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as listing:
                    entries = sorted(list(listing), key=lambda entry: entry.name.casefold())
            except OSError as error:
                errors.append({"path": str(directory), "error": str(error)})
                continue
            subtitles = {Path(entry.name).stem.casefold(): str(Path(entry.path).resolve())
                         for entry in entries if Path(entry.name).suffix.lower() == ".srt"}
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not _is_system_dir(Path(entry.path)):
                            pending.append(Path(entry.path))
                    elif entry.is_file() and media_kind(Path(entry.path)):
                        path = Path(entry.path).resolve()
                        yield path, subtitles.get(path.stem.casefold()) if path.suffix.lower() in VIDEO_EXTS else None
                except OSError as error:
                    errors.append({"path": entry.path, "error": str(error)})

    def scan(self, library_id: str) -> dict:
        with self._lock:
            with self.store.connect() as db:
                self.store.require(db, "libraries", library_id)
                roots = [dict(row) for row in db.execute("SELECT * FROM roots WHERE library_id=?", (library_id,))]
            errors, indexed = [], set()
            try:
                probe_identity = tool_identity(config.FFPROBE) + str(PROBE_ARGS)
            except OSError as error:
                probe_identity = ""
                errors.append({"path": config.FFPROBE, "error": str(error)})
            for root in roots:
                folder = Path(root["path"])
                root_errors = []
                try:
                    if folder.is_symlink():
                        raise ValueError("目录已变成软链接，请选择实际目录")
                    with os.scandir(folder):
                        pass
                    available = True
                except (OSError, ValueError) as error:
                    available = False
                    root_errors.append({"path": str(folder), "error": str(error)})
                if available:
                    for path, subtitle in self._walk(folder, root_errors):
                        try:
                            identifier, error = self._index(path, subtitle, probe_identity)
                            with self.store.connect() as db:
                                # A root removed during scanning must stay removed.
                                if not db.execute("SELECT 1 FROM roots WHERE id=?", (root["id"],)).fetchone():
                                    break
                                db.execute("INSERT OR IGNORE INTO memberships VALUES (?,?,0)",
                                           (library_id, identifier))
                                db.execute("INSERT OR IGNORE INTO asset_roots VALUES (?,?)", (root["id"], identifier))
                            indexed.add(identifier)
                            if error:
                                root_errors.append({"path": str(path), "error": error})
                        except OSError as error:
                            root_errors.append({"path": str(path), "error": str(error)})
                with self.store.connect() as db:
                    db.execute("UPDATE roots SET available=?,error=?,scanned_at=? WHERE id=?",
                               (int(available), "\n".join(item["error"] for item in root_errors), now(), root["id"]))
                errors.extend(root_errors)
            offline = 0
            with self.store.connect() as db:
                rows = db.execute("""SELECT DISTINCT a.id,a.path FROM assets a
                    JOIN asset_roots ar ON ar.asset_id=a.id JOIN roots r ON r.id=ar.root_id
                    WHERE r.library_id=?""", (library_id,)).fetchall()
                for row in rows:
                    try:
                        with Path(row["path"]).open("rb"):
                            pass
                        present = True
                    except OSError as error:
                        present = False
                        if not isinstance(error, FileNotFoundError):
                            errors.append({"path": row["path"], "error": str(error)})
                    db.execute("UPDATE assets SET available=? WHERE id=?", (int(present), row["id"]))
                    offline += not present
            return {"library_id": library_id, "indexed": len(indexed),
                    "available": len(rows) - offline, "offline": offline, "errors": errors}

    def _index(self, path: Path, subtitle: str | None, identity: str) -> tuple[str, str]:
        signature, location, kind = stamp(path), canonical(path), media_kind(path)
        with self.store.connect() as db:
            prior = db.execute("SELECT * FROM assets WHERE path=?", (location,)).fetchone()
        metadata = {"duration": 0, "width": 0, "height": 0}
        error = ""
        if prior and prior["signature"] == signature and prior["probe_identity"] == identity and not prior["error"]:
            metadata = {key: prior[key] for key in metadata}
        elif kind != "subtitle":
            try:
                metadata = probe_metadata(path, kind)
            except (OSError, ValueError, TimeoutError) as problem:
                error = str(problem)
            # subprocess.TimeoutExpired is a SubprocessError, not TimeoutError.
            except subprocess.TimeoutExpired:
                error = "读取媒体信息超时"
        if stamp(path) != signature:
            raise OSError("素材在扫描时发生变化，请重新扫描")
        identifier = prior["id"] if prior else uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("""INSERT INTO assets
                (id,path,name,kind,signature,probe_identity,duration,width,height,available,error,srt_path,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET name=excluded.name,kind=excluded.kind,signature=excluded.signature,
                    probe_identity=excluded.probe_identity,duration=excluded.duration,width=excluded.width,
                    height=excluded.height,available=1,error=excluded.error,srt_path=excluded.srt_path,updated_at=excluded.updated_at
                """, (identifier, location, path.name, kind, signature, identity,
                      metadata["duration"], metadata["width"], metadata["height"], error,
                      subtitle, now(), now()))
            identifier = db.execute("SELECT id FROM assets WHERE path=?", (location,)).fetchone()[0]
        return identifier, error
