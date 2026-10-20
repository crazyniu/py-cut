"""Project-scoped material catalog and immutable successful-output usage ledger.

Each operation owns its SQLite transaction. Catalog metadata is for presentation;
processing must probe its frozen input paths through the normal core functions.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid


DEFAULT_LIBRARIES = (
    ("话术", "reference"), ("背景切片", "background"), ("画中画", "pip"),
    ("前贴", "intro"), ("贴纸", "sticker"),
)
ROLES = {role for _, role in DEFAULT_LIBRARIES}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def associated_path(path: str, known_paths: set[str]) -> str:
    """Keep a saved association address even when its filesystem target changes."""
    address = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
    return address if address in known_paths else canonical(path)


def stamp(path: Path) -> str:
    value = path.stat()
    return json.dumps([value.st_dev, value.st_ino, value.st_size,
                       value.st_mtime_ns, value.st_ctime_ns])


def _name(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError("名称不能为空")
    return value


class Store:
    """Own an application-data database, opening one connection per operation."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.data_dir / "catalog.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, output_dir TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS libraries (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS roots (
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
                    path TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', scanned_at TEXT,
                    UNIQUE(library_id, path)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    kind TEXT NOT NULL, signature TEXT NOT NULL DEFAULT '',
                    probe_identity TEXT NOT NULL DEFAULT '', duration REAL NOT NULL DEFAULT 0,
                    width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                    available INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    srt_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    library_id TEXT NOT NULL REFERENCES libraries(id),
                    asset_id TEXT NOT NULL REFERENCES assets(id), removed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(library_id, asset_id)
                );
                CREATE TABLE IF NOT EXISTS asset_roots (
                    root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL REFERENCES assets(id), PRIMARY KEY(root_id, asset_id)
                );
                CREATE TABLE IF NOT EXISTS outputs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    job_id TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage (
                    output_id TEXT NOT NULL REFERENCES outputs(id),
                    asset_id TEXT NOT NULL REFERENCES assets(id), role TEXT NOT NULL,
                    path TEXT NOT NULL, signature TEXT NOT NULL, details TEXT NOT NULL,
                    PRIMARY KEY(output_id, asset_id, role, details)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    scope TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS libraries_project ON libraries(project_id);
                CREATE INDEX IF NOT EXISTS usage_asset ON usage(asset_id, output_id);
                CREATE INDEX IF NOT EXISTS asset_roots_asset ON asset_roots(asset_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(memberships)")}
            if "excluded" not in columns:
                db.execute("ALTER TABLE memberships ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
            self._fixed_libraries(db)

    @staticmethod
    def _fixed_libraries(db):
        """Fold legacy named groups into the five system roles without reviving removals."""
        for project in db.execute("SELECT id FROM projects").fetchall():
            for label, role in DEFAULT_LIBRARIES:
                rows = db.execute("SELECT * FROM libraries WHERE project_id=? AND role=? ORDER BY created_at,id",
                                  (project["id"], role)).fetchall()
                if not rows:
                    db.execute("INSERT INTO libraries VALUES (?,?,?,?,?)",
                               (uuid.uuid4().hex, project["id"], label, role, now()))
                    continue
                target = next((row for row in rows if row["name"] == label), rows[0])
                db.execute("UPDATE libraries SET name=? WHERE id=?", (label, target["id"]))
                for row in rows:
                    if row["id"] == target["id"]:
                        continue
                    db.execute("""INSERT INTO memberships(library_id,asset_id,removed,excluded)
                        SELECT ?,asset_id,removed,excluded FROM memberships WHERE library_id=?
                        ON CONFLICT(library_id,asset_id) DO UPDATE SET removed=MAX(memberships.removed,excluded.removed),
                            excluded=MAX(memberships.excluded,excluded.excluded)
                        """, (target["id"], row["id"]))
                    for root in db.execute("SELECT * FROM roots WHERE library_id=?", (row["id"],)).fetchall():
                        prior = db.execute("SELECT id FROM roots WHERE library_id=? AND path=?",
                                           (target["id"], root["path"])).fetchone()
                        if prior:
                            db.execute("INSERT OR IGNORE INTO asset_roots SELECT ?,asset_id FROM asset_roots WHERE root_id=?",
                                       (prior["id"], root["id"]))
                            db.execute("DELETE FROM roots WHERE id=?", (root["id"],))
                        else:
                            db.execute("UPDATE roots SET library_id=? WHERE id=?", (target["id"], root["id"]))
                    db.execute("DELETE FROM memberships WHERE library_id=?", (row["id"],))
                    db.execute("DELETE FROM libraries WHERE id=?", (row["id"],))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS libraries_project_role ON libraries(project_id,role)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def require(db, table: str, identifier: str):
        # Table names are module constants, never user input.
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("记录不存在")
        return row

    def projects(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY created_at,id")]

    def create_project(self, name: str, output_dir: str = "") -> dict:
        project = dict(id=uuid.uuid4().hex, name=_name(name),
                       output_dir=canonical(output_dir) if output_dir else "", created_at=now())
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES (:id,:name,:output_dir,:created_at)", project)
            for label, role in DEFAULT_LIBRARIES:
                db.execute("INSERT INTO libraries VALUES (?,?,?,?,?)",
                           (uuid.uuid4().hex, project["id"], label, role, now()))
        return project

    def update_project(self, id: str, **fields) -> dict:
        if set(fields) - {"name", "output_dir"}:
            raise ValueError("不支持的项目设置")
        with self.connect() as db:
            project = dict(self.require(db, "projects", id))
            if "name" in fields:
                project["name"] = _name(fields["name"])
            if "output_dir" in fields:
                project["output_dir"] = canonical(fields["output_dir"]) if fields["output_dir"] else ""
            db.execute("UPDATE projects SET name=:name,output_dir=:output_dir WHERE id=:id", project)
            return project

    def libraries(self, project_id: str) -> list[dict]:
        with self.connect() as db:
            self.require(db, "projects", project_id)
            result = []
            for row in db.execute("SELECT * FROM libraries WHERE project_id=? ORDER BY created_at,id",
                                  (project_id,)):
                item = dict(row)
                item["roots"] = [dict(root) for root in db.execute("""
                    SELECT r.*,(SELECT COUNT(*) FROM asset_roots ar JOIN memberships m ON m.asset_id=ar.asset_id
                        WHERE ar.root_id=r.id AND m.library_id=r.library_id AND m.removed=0) AS active_count
                    FROM roots r WHERE r.library_id=? ORDER BY r.rowid
                    """, (row["id"],))]
                counts = db.execute("""
                    SELECT m.removed, COUNT(*) AS total FROM memberships m
                    WHERE m.library_id=? AND EXISTS (
                        SELECT 1 FROM asset_roots ar JOIN roots r ON r.id=ar.root_id
                        WHERE ar.asset_id=m.asset_id AND r.library_id=m.library_id)
                    GROUP BY m.removed
                """, (row["id"],)).fetchall()
                item.update(active_count=0, removed_count=0)
                for count in counts:
                    item["removed_count" if count["removed"] else "active_count"] = count["total"]
                video_counts = db.execute("""
                    SELECT COUNT(*), COALESCE(SUM(m.excluded=0),0) FROM memberships m JOIN assets a ON a.id=m.asset_id
                    WHERE m.library_id=? AND m.removed=0 AND a.kind='video' AND EXISTS (
                        SELECT 1 FROM asset_roots ar JOIN roots r ON r.id=ar.root_id
                        WHERE ar.asset_id=a.id AND r.library_id=m.library_id)
                """, (row["id"],)).fetchone()
                item["video_count"], item["included_video_count"] = video_counts
                result.append(item)
            order = {role: index for index, (_, role) in enumerate(DEFAULT_LIBRARIES)}
            return sorted(result, key=lambda item: order[item["role"]])

    def add_roots(self, library_id: str, roots: list[str]) -> list[dict]:
        paths = []
        for root in roots:
            if not isinstance(root, str) or not root.strip():
                raise ValueError("素材目录不能为空")
            path = Path(root).expanduser()
            if path.is_symlink():
                raise ValueError("请选择实际目录，不使用目录软链接")
            paths.append(canonical(path))
        with self.connect() as db:
            self.require(db, "libraries", library_id)
            for path in dict.fromkeys(paths):
                db.execute("INSERT OR IGNORE INTO roots (id,library_id,path) VALUES (?,?,?)",
                           (uuid.uuid4().hex, library_id, path))
            return [dict(row) for row in db.execute(
                "SELECT * FROM roots WHERE library_id=? ORDER BY rowid", (library_id,))]

    def remove_root(self, library_id: str, path: str) -> None:
        with self.connect() as db:
            self.require(db, "libraries", library_id)
            known = {row[0] for row in db.execute("SELECT path FROM roots WHERE library_id=?", (library_id,))}
            db.execute("DELETE FROM roots WHERE library_id=? AND path=?",
                       (library_id, associated_path(path, known)))

    def assets(self, library_id: str, query: str = "", removed: bool = False,
               sort: str = "name", root: str = "", offset: int = 0, limit: int = 100,
               kind: str = "") -> dict:
        ordering = {"name": "a.name COLLATE NOCASE,a.path", "duration": "a.duration DESC,a.path",
                    "size": "(a.width*a.height) DESC,a.path", "resolution": "(a.width*a.height) DESC,a.path",
                    "usage": "usage_count DESC,a.path", "usage_count": "usage_count DESC,a.path"}
        if sort not in ordering:
            raise ValueError("不支持的排序")
        if kind not in ("", "video", "image", "subtitle"):
            raise ValueError("不支持的素材类型")
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        with self.connect() as db:
            library = self.require(db, "libraries", library_id)
            args = [library_id, int(removed)]
            condition = """m.library_id=? AND m.removed=? AND EXISTS (
                SELECT 1 FROM asset_roots ar JOIN roots r ON r.id=ar.root_id
                WHERE ar.asset_id=a.id AND r.library_id=m.library_id"""
            if root:
                condition += " AND r.path=?"
                known = {row[0] for row in db.execute("SELECT path FROM roots WHERE library_id=?", (library_id,))}
                args.append(associated_path(root, known))
            condition += ")"
            if kind:
                condition += " AND a.kind=?"
                args.append(kind)
            if query:
                condition += " AND instr(lower(a.name),lower(?))>0"
                args.append(query)
            total = db.execute(f"SELECT COUNT(*) FROM assets a JOIN memberships m ON m.asset_id=a.id WHERE {condition}",
                               args).fetchone()[0]
            rows = db.execute(f"""
                SELECT a.*,m.removed,m.excluded,(SELECT COUNT(DISTINCT u.output_id) FROM usage u
                    JOIN outputs o ON o.id=u.output_id WHERE u.asset_id=a.id AND o.project_id=?) AS usage_count
                FROM assets a JOIN memberships m ON m.asset_id=a.id
                WHERE {condition} ORDER BY {ordering[sort]} LIMIT ? OFFSET ?
            """, [library["project_id"], *args, limit, offset]).fetchall()
            return {"items": [self._asset(row) for row in rows], "total": total}

    @staticmethod
    def _asset(row) -> dict:
        item = dict(row)
        item["available"] = bool(item["available"])
        if "removed" in item:
            item["removed"] = bool(item["removed"])
        if "excluded" in item:
            item["excluded"] = bool(item["excluded"])
        item.setdefault("usage_count", 0)
        return item

    def set_removed(self, library_id: str, asset_ids: list[str], removed: bool) -> None:
        with self.connect() as db:
            self.require(db, "libraries", library_id)
            for asset_id in dict.fromkeys(asset_ids):
                if db.execute("SELECT 1 FROM memberships WHERE library_id=? AND asset_id=?",
                              (library_id, asset_id)).fetchone() is None:
                    raise ValueError("素材不属于此素材库")
                db.execute("UPDATE memberships SET removed=? WHERE library_id=? AND asset_id=?",
                           (int(removed), library_id, asset_id))

    def set_excluded(self, library_id: str, asset_id: str, excluded: bool) -> None:
        """Persist the reference inclusion choice without hiding or deleting media."""
        if type(excluded) is not bool:
            raise ValueError("排除状态必须为布尔值")
        with self.connect() as db:
            library = self.require(db, "libraries", library_id)
            asset = self.require(db, "assets", asset_id)
            if library["role"] != "reference" or asset["kind"] != "video":
                raise ValueError("仅话术视频支持混剪排除")
            changed = db.execute("UPDATE memberships SET excluded=? WHERE library_id=? AND asset_id=?",
                                 (int(excluded), library_id, asset_id)).rowcount
            if not changed:
                raise ValueError("素材不属于此素材库")

    def asset_detail(self, asset_id: str, project_id: str | None = None) -> dict:
        with self.connect() as db:
            item = self._asset(self.require(db, "assets", asset_id))
            args = [asset_id]
            query = """SELECT u.*,o.project_id,o.job_id,o.path AS output_path,o.created_at
                       FROM usage u JOIN outputs o ON o.id=u.output_id WHERE u.asset_id=?"""
            if project_id is not None:
                self.require(db, "projects", project_id)
                if not db.execute("""SELECT 1 FROM memberships m JOIN libraries l ON l.id=m.library_id
                                     WHERE m.asset_id=? AND l.project_id=?""", (asset_id, project_id)).fetchone():
                    raise ValueError("素材不属于此项目")
                query += " AND o.project_id=?"
                args.append(project_id)
            item["active_library_ids"] = [row[0] for row in db.execute("""
                SELECT m.library_id FROM memberships m JOIN libraries l ON l.id=m.library_id
                WHERE m.asset_id=? AND m.removed=0 AND (? IS NULL OR l.project_id=?) AND EXISTS (
                    SELECT 1 FROM asset_roots ar JOIN roots r ON r.id=ar.root_id
                    WHERE ar.asset_id=m.asset_id AND r.library_id=m.library_id)
            """, (asset_id, project_id, project_id))]
            item["uses"] = [{**dict(row), "details": json.loads(row["details"])}
                            for row in db.execute(query + " ORDER BY o.created_at DESC", args)]
            item["usage_count"] = len({row["output_id"] for row in item["uses"]})
            return item

    def snapshot(self, library_id: str) -> list[dict]:
        """Freeze active membership in one read. Missing files are offline; access errors fail."""
        with self.connect() as db:
            library = self.require(db, "libraries", library_id)
            rows = db.execute("""
                SELECT a.*,m.removed,(SELECT COUNT(DISTINCT u.output_id) FROM usage u
                    JOIN outputs o ON o.id=u.output_id WHERE u.asset_id=a.id AND o.project_id=?) AS usage_count
                FROM assets a JOIN memberships m ON m.asset_id=a.id
                WHERE m.library_id=? AND m.removed=0 AND EXISTS (
                    SELECT 1 FROM asset_roots ar JOIN roots r ON r.id=ar.root_id
                    WHERE ar.asset_id=a.id AND r.library_id=m.library_id)
                ORDER BY a.path
            """, (library["project_id"], library_id)).fetchall()
        result = []
        for row in rows:
            item, path = self._asset(row), Path(row["path"])
            try:
                signature = stamp(path)
                with path.open("rb"):
                    pass
            except FileNotFoundError:
                continue
            item["signature"] = signature
            item["available"] = True
            result.append(item)
        return result

    def record_output(self, output_id: str, project_id: str, job_id: str,
                      path: str, uses: list[dict]) -> str:
        """Commit one published output and its actual inputs atomically and once.

        A use supplies asset_id or path, role, and optional interval fields. Paths
        and signatures are copied into the ledger so later rescans cannot rewrite history.
        """
        output_path = canonical(path)
        if not Path(output_path).is_file():
            raise FileNotFoundError("仅已发布成片可以记录素材使用")
        with self.connect() as db:
            self.require(db, "projects", project_id)
            existing = db.execute("SELECT * FROM outputs WHERE id=?", (output_id,)).fetchone()
            if existing:
                if existing["project_id"] != project_id or existing["path"] != output_path:
                    raise ValueError("成片标识已被其他输出使用")
                return output_id
            resolved = []
            for use in uses:
                if use.get("asset_id"):
                    asset = self.require(db, "assets", use["asset_id"])
                else:
                    asset = db.execute("SELECT * FROM assets WHERE path=?", (canonical(use["path"]),)).fetchone()
                    if asset is None:
                        # Downloaded stickers may not belong to a user library.
                        source = Path(use["path"])
                        source_path = canonical(source)
                        identifier = uuid.uuid4().hex
                        kind = "subtitle" if source.suffix.lower() == ".srt" else (
                            "image" if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} else "video")
                        db.execute("""INSERT INTO assets (id,path,name,kind,signature,available,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?)""",
                                   (identifier, source_path, source.name, kind,
                                    use.get("signature", ""), int(source.is_file()), now(), now()))
                        asset = self.require(db, "assets", identifier)
                resolved.append((asset, use))
            db.execute("INSERT INTO outputs VALUES (?,?,?,?,?)",
                       (output_id, project_id, job_id, output_path, now()))
            for asset, use in resolved:
                details = {key: value for key, value in use.items()
                           if key not in {"asset_id", "path", "signature", "role"}}
                db.execute("INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?)",
                           (output_id, asset["id"], str(use.get("role", "background")),
                            canonical(use["path"]) if use.get("path") else asset["path"],
                            use.get("signature", asset["signature"]),
                            json.dumps(details, ensure_ascii=False, sort_keys=True)))
        return output_id

    def outputs(self, project_id: str) -> list[dict]:
        with self.connect() as db:
            self.require(db, "projects", project_id)
            result = []
            for row in db.execute("SELECT * FROM outputs WHERE project_id=? ORDER BY created_at DESC", (project_id,)):
                item = dict(row)
                item["available"] = Path(item["path"]).is_file()
                item["uses"] = [{**dict(use), "details": json.loads(use["details"])} for use in db.execute(
                    "SELECT u.*,a.name,a.kind FROM usage u JOIN assets a ON a.id=u.asset_id WHERE output_id=?",
                    (row["id"],))]
                result.append(item)
            return result

    def settings(self, project_id: str | None = None) -> dict:
        with self.connect() as db:
            if project_id:
                self.require(db, "projects", project_id)
            row = db.execute("SELECT value FROM settings WHERE scope=?", (project_id or "global",)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_settings(self, values: dict, project_id: str | None = None) -> dict:
        def check(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = str(key).lower().replace("-", "_")
                    secret_words = ("password", "secret", "token", "credential", "api_key", "apikey", "llm_key")
                    if (any(word in normalized for word in secret_words)
                            or normalized == "key" or normalized.endswith("_key")):
                        raise ValueError("密钥不能保存在素材数据库")
                    check(nested)
            elif isinstance(value, list):
                for nested in value:
                    check(nested)
        if not isinstance(values, dict):
            raise ValueError("设置必须为对象")
        check(values)
        with self.connect() as db:
            if project_id:
                self.require(db, "projects", project_id)
            row = db.execute("SELECT value FROM settings WHERE scope=?", (project_id or "global",)).fetchone()
            merged = {**(json.loads(row[0]) if row else {}), **values}
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                       (project_id or "global", json.dumps(merged, ensure_ascii=False)))
            return merged
