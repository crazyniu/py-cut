"""Persistent serial desktop queue; each processing job runs in its own process.

Freeze catalog membership before enqueueing. Child cancellation is explicit and
publication recovery uses the prepared output journal rather than file existence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .engine import signature, recover_publications

ACTIVE = ("queued", "running", "cancelling")


def planned_items(request):
    if request["kind"] == "montage":
        return [(f"{asset['id']}:{variant}", f"{Path(asset['path']).stem} · 第 {variant} 支", asset["path"])
                for asset in request["references"] for variant in range(1, request["fission"] + 1)]
    return [(asset["id"], Path(asset["path"]).name, asset["path"]) for asset in request["inputs"]]


def now():
    return datetime.now(timezone.utc).isoformat()


def sanitize(text):
    for key, value in os.environ.items():
        if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and value:
            text = text.replace(value, "[已脱敏]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[已脱敏]", text)
    return text[:4096]


class JobManager:
    def __init__(self, store, sink, *, start=True):
        self.store, self.data_dir, self.sink = store, store.data_dir, sink
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._process = None
        self._active = None
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS desktop_jobs (
                    id TEXT PRIMARY KEY,project_id TEXT NOT NULL,kind TEXT NOT NULL,
                    status TEXT NOT NULL,request_json TEXT NOT NULL,stats_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,finished_at TEXT,error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS desktop_items (
                    job_id TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,
                    status TEXT NOT NULL,output_id TEXT NOT NULL DEFAULT '',progress_json TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(job_id,id)
                );
                CREATE TABLE IF NOT EXISTS desktop_logs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,line TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS desktop_log_job ON desktop_logs(job_id,seq);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(desktop_items)")}
            for name, default in (("output_id", "''"), ("progress_json", "'{}'")):
                if name not in columns:
                    db.execute(f"ALTER TABLE desktop_items ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}")
            for job in db.execute("SELECT id,request_json,status FROM desktop_jobs"):
                request = json.loads(job["request_json"])
                state = "queued" if job["status"] in ACTIVE else "unprocessed"
                db.executemany("INSERT OR IGNORE INTO desktop_items(job_id,id,name,path,status) VALUES (?,?,?,?,?)",
                               [(job["id"], *item, state) for item in planned_items(request)])
            # Older montage journals did not carry the item identifier. Recover it
            # from the exact reference usage and the variant suffix, never row order.
            for row in db.execute("SELECT o.id,o.job_id,o.path,u.asset_id FROM outputs o JOIN usage u ON u.output_id=o.id WHERE u.role='reference'"):
                match = re.search(r"_(\d+)\.mp4$", row["path"], re.IGNORECASE)
                if match:
                    db.execute("UPDATE desktop_items SET output_id=?,status='completed' WHERE job_id=? AND id=? AND output_id=''",
                               (row["id"], row["job_id"], f"{row['asset_id']}:{int(match[1])}"))
        recover_publications(store, self.data_dir)
        with store.connect() as db:
            db.execute("UPDATE desktop_jobs SET status='interrupted',finished_at=?,error=? "
                       "WHERE status IN ('running','cancelling')", (now(), "程序上次退出时任务未完成"))
            db.execute("UPDATE desktop_items SET status='interrupted' WHERE status IN ('queued','running') "
                       "AND job_id IN (SELECT id FROM desktop_jobs WHERE status='interrupted')")
            montage_ids = [row[0] for row in db.execute("SELECT id FROM desktop_jobs WHERE kind='montage'")]
        for job_id in montage_ids:
            self._update(job_id, completed=len(self.detail(job_id)["outputs"]))
        self._thread = threading.Thread(target=self._loop, daemon=True, name="desktop-queue")
        if start:
            self._thread.start()

    def _folder_inputs(self, folders, kind, options):
        from ..core.scan import list_videos_recursive
        from ..core.output_paths import without_outputs
        from .library import LibraryIndexer
        if not isinstance(folders, list) or not folders or any(
                not isinstance(folder, str) or not folder.strip() for folder in folders):
            raise ValueError("请选择需要处理的文件夹")
        roots = []
        for folder in folders:
            path = Path(folder).expanduser()
            if path.is_symlink():
                raise ValueError("请选择实际文件夹，不使用目录软链接")
            with os.scandir(path):
                pass
            roots.append(str(path.resolve()))
        roots = list(dict.fromkeys(roots))
        paths = [path for path in list_videos_recursive(roots, max_depth=None, raise_on_error=True)
                 if path.suffix.lower() == ".mp4"]
        paths = without_outputs(paths, roots, "字幕查重" if kind == "cover" else "去重", options.get("out_dir", ""))
        return LibraryIndexer(self.store).index_files(paths), roots

    def _snapshot(self, request):
        project = next((p for p in self.store.projects() if p["id"] == request["project_id"]), None)
        if project is None:
            raise ValueError("项目不存在")
        libraries = {item["id"]: item for item in self.store.libraries(project["id"])}
        all_assets = {}
        def pool(identifier):
            if identifier not in libraries:
                raise ValueError("请选择当前项目的素材库")
            for root in roots(identifier):
                with os.scandir(root):
                    pass
            items = self.store.snapshot(identifier)
            all_assets.update((a["id"], a) for a in items)
            return items
        def roots(identifier):
            return [item["path"] if isinstance(item, dict) else item
                    for item in libraries[identifier]["roots"]]
        def role_pool(role, identifier=None):
            identifiers = ([identifier] if identifier else
                           [key for key, library in libraries.items() if library["role"] == role])
            entries, paths = {}, []
            for key in identifiers:
                for asset in pool(key):
                    entries[asset["id"]] = asset
                paths.extend(roots(key))
            return list(entries.values()), list(dict.fromkeys(paths))
        options = request.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("任务参数必须为对象")
        options = dict(options)
        kind = request["kind"]
        if kind not in ("montage", "cover", "dedup"):
            raise ValueError("未知任务类型")
        frozen = {"id": uuid4().hex, "project_id": project["id"], "kind": kind, "options": options}
        if kind == "montage":
            options.setdefault("intro_enabled", any(library["role"] == "intro" and library["roots"]
                                                     for library in libraries.values()))
            ids = request.get("reference_ids")
            if "reference_ids" in request and (not isinstance(ids, list) or not ids):
                raise ValueError("请至少选择一个话术视频")
            with self.store.connect() as db:
                references = [dict(row) for row in db.execute("""
                    SELECT DISTINCT a.* FROM assets a JOIN memberships m ON m.asset_id=a.id
                    JOIN libraries l ON l.id=m.library_id WHERE l.project_id=?
                    AND l.role='reference' AND m.removed=0 AND m.excluded=0 AND EXISTS (
                        SELECT 1 FROM asset_roots ar JOIN roots r ON r.id=ar.root_id
                        WHERE ar.asset_id=a.id AND r.library_id=l.id)
                """, (project["id"],))]
            by_id = {a["id"]: a for a in references if a["kind"] == "video"}
            if ids is None:
                ids = list(by_id)
            if not ids:
                raise ValueError("话术库没有未排除的视频，请先在素材库选中话术")
            if any(identifier not in by_id for identifier in ids):
                raise ValueError("话术已排除、移除、离线或不属于当前项目")
            count = options.get("fission", 1)
            if type(count) is not int or not 1 <= count <= 99:
                raise ValueError("每个话术生成数量必须为 1～99")
            selected = [dict(by_id[i]) for i in dict.fromkeys(ids)]
            active_paths = {a["path"] for a in references}
            for item in selected:
                custom = options.get("srt", "auto")
                disabled = options.get("no_sub", not options.get("use_sub", True))
                subtitle = "" if disabled else item.get("srt_path") if custom == "auto" else custom
                item["subtitle_path"] = subtitle if subtitle and (custom != "auto" or subtitle in active_paths) else ""
                if item["subtitle_path"]:
                    all_assets["srt:" + item["subtitle_path"]] = {"path": item["subtitle_path"]}
            background_id = request.get("background_library_id")
            background, background_roots = role_pool("background", background_id)
            background = [a for a in background if a["kind"] == "video"]
            if not background:
                raise ValueError("背景素材库没有可用视频")
            pools, root_paths = {"background": background}, {"background": background_roots}
            for role in ("pip", "intro", "sticker"):
                identifier = request.get(role + "_library_id")
                enabled = (options.get("intro_enabled", False) if role == "intro" else
                           not options.get("no_pip", not options.get("use_pip", True)) if role == "pip" else
                           not options.get("no_sticker", not options.get("use_sticker", True)))
                if enabled:
                    entries, paths = role_pool(role, identifier)
                    entries = [a for a in entries if a["kind"] == ("image" if role == "sticker" else "video")]
                    pools[role], root_paths[role] = entries, paths
                else:
                    pools[role], root_paths[role] = [], []
            options.setdefault("no_sticker", not bool(pools["sticker"]))
            output_dir = options.get("out_dir") or project["output_dir"]
            if not output_dir:
                raise ValueError("请设置项目的成片输出目录")
            frozen.update(references=selected, fission=count, planned=len(selected) * count,
                          pools=pools, roots=root_paths, output_dir=str(Path(output_dir).expanduser().resolve()),
                          sticker_library_id=request.get("sticker_library_id") or
                          ("configured" if pools.get("sticker") else None))
            used = [*selected, *sum(pools.values(), [])]
            used.extend(a for key, a in all_assets.items() if key.startswith("srt:"))
        else:
            identifier = request.get("library_id")
            if "input_dirs" in request:
                inputs, input_roots = self._folder_inputs(request["input_dirs"], kind, options)
            else:
                entries, input_roots = role_pool("background", identifier)
                inputs = [a for a in entries if a["kind"] == "video" and Path(a["path"]).suffix.lower() == ".mp4"]
            ids = request.get("asset_ids", [])
            if ids:
                selected_ids = set(ids)
                if selected_ids - {a["id"] for a in inputs}:
                    raise ValueError("所选视频已移除、离线或不属于当前素材库")
                inputs = [a for a in inputs if a["id"] in selected_ids]
            if not inputs:
                raise ValueError("所选素材库没有可处理的 MP4 视频")
            frozen.update(inputs=inputs, input_roots=input_roots, planned=len(inputs),
                          agent_settings=self.store.settings())
            used = inputs
        # Desktop batches own separate folders; CLI sibling output behavior stays in the commands.
        from ..core.output_paths import sibling
        output_root = (Path(frozen["output_dir"]) if kind == "montage" else
                       Path(options["out_dir"]).expanduser().resolve() if options.get("out_dir") else
                       sibling(Path(frozen["input_roots"][0]), "字幕查重" if kind == "cover" else "去重"))
        label = {"montage": "混剪", "cover": "字幕擦除", "dedup": "视频去重"}[kind]
        frozen["batch_dir"] = str(output_root / (label + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + frozen["id"][:8]))
        snapshots = {}
        for asset in used:
            with Path(asset["path"]).open("rb"):
                pass
            snapshots[asset["path"]] = {"path": asset["path"], "signature": signature(asset["path"])}
        frozen["snapshot"] = list(snapshots.values())
        return frozen

    def submit(self, request):
        from .engine import parse_options
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("程序正在退出")
            frozen = self._snapshot(request)
            parse_options(frozen["kind"], frozen["options"])
            stats = {"planned": frozen["planned"], "completed": 0, "failed": 0, "review": 0,
                     "stage": "等待执行", "progress": {"stage": "等待执行", "completed": 0, "total": 0}}
            with self.store.connect() as db:
                db.execute("INSERT INTO desktop_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                           (frozen["id"], frozen["project_id"], frozen["kind"], "queued",
                            json.dumps(frozen, ensure_ascii=False), json.dumps(stats, ensure_ascii=False), now(), None, ""))
                db.executemany("INSERT INTO desktop_items(job_id,id,name,path,status) VALUES (?,?,?,?,?)",
                               [(frozen["id"], *item, "queued") for item in planned_items(frozen)])
            job = self.detail(frozen["id"])
            self.sink({"event": "job", "job_id": job["id"], "payload": job})
            self._wake.set()
            return job

    def detail(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM desktop_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            job = dict(row)
            job.update(json.loads(job.pop("stats_json")))
            frozen = json.loads(job.pop("request_json"))
            job["batch_dir"] = frozen.get("batch_dir", "")
            job["batch_available"] = bool(job["batch_dir"] and Path(job["batch_dir"]).is_dir())
            job["items"] = []
            for item in db.execute("SELECT * FROM desktop_items WHERE job_id=? ORDER BY rowid", (job_id,)):
                value = dict(item)
                value["progress"] = json.loads(value.pop("progress_json"))
                value.pop("job_id")
                job["items"].append(value)
            job["logs"] = [r[0] for r in db.execute("SELECT line FROM desktop_logs WHERE job_id=? ORDER BY seq DESC LIMIT 1000", (job_id,))][::-1]
        job["outputs"] = [{**o, "name": Path(o["path"]).name}
                          for o in self.store.outputs(job["project_id"]) if o["job_id"] == job_id]
        return job

    def list(self, project_id):
        with self.store.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM desktop_jobs WHERE project_id=? ORDER BY created_at DESC", (project_id,))]
        return [self.detail(i) for i in ids]

    def _update(self, job_id, *, status=None, error=None, **stats):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM desktop_jobs WHERE id=?", (job_id,)).fetchone()
            value = {**json.loads(row["stats_json"]), **stats}
            state = status or row["status"]
            if state not in ACTIVE:
                pending = state if state in ("cancelled", "interrupted") else "unprocessed"
                db.execute("UPDATE desktop_items SET status=? WHERE job_id=? AND status IN ('queued','running')",
                           (pending, job_id))
            db.execute("UPDATE desktop_jobs SET stats_json=?,status=?,error=?,finished_at=? WHERE id=?",
                       (json.dumps(value, ensure_ascii=False), state, error if error is not None else row["error"],
                        now() if state not in ACTIVE else None, job_id))
        self.sink({"event": "job", "job_id": job_id, "payload": self.detail(job_id)})

    def cancel(self, job_id):
        with self._lock:
            job = self.detail(job_id)
            if job["status"] == "queued":
                self._update(job_id, status="cancelled")
            elif job["status"] in ("running", "cancelling"):
                self._update(job_id, status="cancelling")
                if self._active == job_id and self._process and self._process.stdin:
                    try:
                        self._process.stdin.write("cancel\n")
                        self._process.stdin.flush()
                    except (OSError, ValueError):
                        pass
        return self.detail(job_id)

    def _command(self, job_id):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--worker", job_id]
        root = Path(__file__).resolve().parents[2]
        return (["cmd", "/d", "/c", str(root / "run.cmd"), "desktop-worker", job_id]
                if os.name == "nt" else [str(root / "run.sh"), "desktop-worker", job_id])

    def _line(self, job_id, line):
        from ..core.progress import parse
        if line.startswith("@pycut-desktop "):
            event = json.loads(line[len("@pycut-desktop "):])
            data = event["payload"]
            if event["event"] == "item":
                if data["status"] not in ("running", "completed", "review", "failed", "skipped", "cancelled"):
                    raise ValueError("未知视频状态")
                with self.store.connect() as db:
                    db.execute("UPDATE desktop_items SET status=? WHERE job_id=? AND id=?",
                               (data["status"], job_id, data["id"]))
                self.sink({"event": "job", "job_id": job_id, "payload": self.detail(job_id)})
            elif event["event"] == "counts":
                self._update(job_id, **data)
            elif event["event"] == "result":
                self._update(job_id, **data)
            elif event["event"] == "failure":
                self._update(job_id, error=sanitize(str(data["message"])))
            self.sink({"event": event["event"], "job_id": job_id, "payload": data})
            return
        progress = parse(line)
        if progress and progress.get("item_progress") and progress.get("item_id"):
            with self.store.connect() as db:
                db.execute("UPDATE desktop_items SET progress_json=? WHERE job_id=? AND id=?",
                           (json.dumps(progress, ensure_ascii=False), job_id, progress["item_id"]))
            self.sink({"event": "job", "job_id": job_id, "payload": self.detail(job_id)})
            return
        if progress:
            self._update(job_id, stage=progress["stage"], progress=progress)
            self.sink({"event": "progress", "job_id": job_id, "payload": progress})
            return
        safe = sanitize(line.rstrip())
        if safe:
            with self.store.connect() as db:
                db.execute("INSERT INTO desktop_logs(job_id,line) VALUES (?,?)", (job_id, safe))
                db.execute("DELETE FROM desktop_logs WHERE job_id=? AND seq NOT IN "
                           "(SELECT seq FROM desktop_logs WHERE job_id=? ORDER BY seq DESC LIMIT 10000)", (job_id, job_id))
            self.sink({"event": "log", "job_id": job_id, "payload": safe})

    def _loop(self):
        while not self._closed.is_set():
            with self._lock:
                with self.store.connect() as db:
                    row = db.execute("SELECT id FROM desktop_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row is not None:
                    self._active = row[0]
                    self._update(row[0], status="running", stage="启动处理引擎")
                    try:
                        env = {**os.environ, "PYCUT_DATA_DIR": str(self.data_dir), "PYTHONUNBUFFERED": "1"}
                        self._process = subprocess.Popen(self._command(row[0]), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env,
                            creationflags=0x08000000 if os.name == "nt" else 0)
                    except OSError as exc:
                        self._update(row[0], status="failed", error=str(exc))
                        self._active = None
                        continue
            if row is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                for line in self._process.stdout:
                    try:
                        self._line(row[0], line)
                    except (ValueError, KeyError) as exc:
                        self._line(row[0], "处理事件无效：" + str(exc))
                code = self._process.wait()
                recover_publications(self.store, self.data_dir, row[0])
                job = self.detail(row[0])
                completed = len(job["outputs"]) if job["kind"] == "montage" else job["completed"]
                if job["status"] in ("running", "cancelling"):
                    state = "cancelled" if job["status"] == "cancelling" else "failed"
                    self._update(row[0], status=state, completed=completed,
                                 error=job["error"] or (f"处理进程退出，未返回完整结果（{code}）" if state == "failed" else ""))
                elif job["kind"] == "montage":
                    self._update(row[0], completed=completed)
            except Exception as exc:
                self._update(row[0], status="failed", error=sanitize(str(exc)))
            finally:
                with self._lock:
                    self._active = self._process = None

    def shutdown(self):
        self._closed.set()
        for project in self.store.projects():
            for job in self.list(project["id"]):
                if job["status"] in ACTIVE:
                    self.cancel(job["id"])
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)
        if self._process and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)
