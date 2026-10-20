"""Small explicit desktop RPC surface over the local catalog and serial queue.

Only indexed assets and recorded outputs can become media URLs. Native dialogs
provide roots; no shell commands, credentials, or arbitrary methods are accepted.
"""
from __future__ import annotations

from pathlib import Path
import threading

from .engine import option_specs
from .jobs import JobManager
from .library import LibraryIndexer
from .media import MediaCache
from .store import Store


class Service:
    def __init__(self, data_dir, sink=lambda event: None, *, start_jobs=True):
        self.store = Store(Path(data_dir))
        self.indexer = LibraryIndexer(self.store, lambda value: sink({"event": "library", "payload": value}))
        self.media = MediaCache(self.store)
        self.jobs = JobManager(self.store, sink, start=start_jobs)
        self.closing = threading.Event()

    def _libraries(self, project_id):
        libraries = self.store.libraries(project_id)
        for item in libraries:
            item["root_status"] = item["roots"]
            item["roots"] = [root["path"] for root in item["roots"]]
        return libraries

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise ValueError("请求参数必须为对象")
        if self.closing.is_set() and method != "app.shutdown":
            raise RuntimeError("程序正在退出")
        p = params
        if method == "app.ping":
            return {"ready": True, "version": "0.1.0"}
        if method == "projects.list":
            return self.store.projects()
        if method == "projects.create":
            return self.store.create_project(p["name"], p.get("output_dir", ""))
        if method == "projects.update":
            return self.store.update_project(p["project_id"], **{k: v for k, v in p.items() if k != "project_id"})
        if method == "libraries.list":
            return self._libraries(p["project_id"])
        if method == "libraries.add_roots":
            return self.store.add_roots(p["library_id"], p["roots"])
        if method == "libraries.remove_root":
            return self.store.remove_root(p["library_id"], p["path"])
        if method == "libraries.scan":
            return self.indexer.scan(p["library_id"], p.get("roots"))
        if method == "assets.list":
            return self.store.assets(**p)
        if method == "assets.set_excluded":
            return self.store.set_excluded(p["library_id"], p["asset_id"], p["excluded"])
        if method in ("assets.remove", "assets.restore"):
            return self.store.set_removed(p["library_id"], p["asset_ids"], method == "assets.remove")
        if method == "assets.detail":
            return self.store.asset_detail(p["asset_id"], p.get("project_id"))
        if method == "media.thumbnail":
            return {"path": self.media.thumbnail(p["asset_id"])}
        if method == "media.preview":
            return {"path": self.media.preview(p["asset_id"])}
        if method in ("media.output_preview", "media.output_thumbnail"):

            with self.store.connect() as db:
                output = self.store.require(db, "outputs", p["output_id"])
                render = self.media.thumbnail_path if method == "media.output_thumbnail" else self.media.preview_path
                return {"path": render(Path(output["path"]))}
        if method == "media.details":
            if p.get("output_id"):
                with self.store.connect() as db:
                    value = self.store.require(db, "outputs", p["output_id"])
                    path = value["path"]
            else:
                path = self.store.asset_detail(p["asset_id"])["path"]
            return self.media.details_path(Path(path))
        if method == "jobs.options":
            return option_specs(p["kind"])
        if method == "jobs.submit":
            return self.jobs.submit(p)
        if method == "jobs.list":
            return self.jobs.list(p["project_id"])
        if method == "jobs.detail":
            return self.jobs.detail(p["job_id"])
        if method == "jobs.cancel":
            return self.jobs.cancel(p["job_id"])
        if method == "outputs.list":
            return [{**row, "name": Path(row["path"]).name} for row in self.store.outputs(p["project_id"])]
        if method == "agents.detect":
            from .local_vision import detect_agents
            return detect_agents()
        if method == "agents.models":
            from .agent_catalog import list_models
            return list_models(p.get("provider", "codex"))
        if method == "settings.get":
            return {"vision_provider": "codex", "agent_path": "", "vision_model": "", **self.store.settings()}
        if method == "workbench.get":
            if p["kind"] not in ("montage", "cover", "dedup"):
                raise ValueError("未知任务类型")
            return self.store.settings(p["project_id"]).get("workbench", {}).get(p["kind"], {})
        if method == "workbench.save":
            if p["kind"] not in ("montage", "cover", "dedup") or not isinstance(p["values"], dict):
                raise ValueError("工作台设置无效")
            with self.jobs._lock:
                current = self.store.settings(p["project_id"]).get("workbench", {})
                self.store.save_settings({"workbench": {**current, p["kind"]: p["values"]}}, p["project_id"])
            return p["values"]
        if method == "settings.save":
            values = p["values"]
            if set(values) - {"vision_provider", "agent_path", "vision_model"}:
                raise ValueError("不支持的设置项")
            if values.get("vision_provider", "codex") != "codex":
                raise ValueError("未知本地 Agent")
            return self.store.save_settings(values)
        if method == "app.shutdown":
            self.closing.set()
            return {"closing": True}
        raise ValueError("未知桌面接口")

    def close(self):
        self.closing.set()
        self.jobs.shutdown()
