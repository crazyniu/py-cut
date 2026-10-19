"""Small explicit desktop RPC surface over the local catalog and serial queue.

Only indexed assets and recorded outputs can become media URLs. Native dialogs
provide roots; no shell commands, credentials, or arbitrary methods are accepted.
"""
from __future__ import annotations

import json
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
        if method == "libraries.create":
            return self.store.create_library(p["project_id"], p["name"], p.get("role", "background"))
        if method == "libraries.add_roots":
            return self.store.add_roots(p["library_id"], p["roots"])
        if method == "libraries.remove_root":
            return self.store.remove_root(p["library_id"], p["path"])
        if method == "libraries.scan":
            return self.indexer.scan(p["library_id"])
        if method == "assets.list":
            return self.store.assets(**p)
        if method in ("assets.remove", "assets.restore"):
            return self.store.set_removed(p["library_id"], p["asset_ids"], method == "assets.remove")
        if method == "assets.detail":
            return self.store.asset_detail(p["asset_id"], p.get("project_id"))
        if method == "media.thumbnail":
            return {"path": self.media.thumbnail(p["asset_id"])}
        if method == "media.preview":
            return {"path": self.media.preview(p["asset_id"])}
        if method == "media.output_preview":
            with self.store.connect() as db:
                output = self.store.require(db, "outputs", p["output_id"])
                return {"path": self.media.preview_path(Path(output["path"]))}
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
            if values.get("vision_provider", "codex") not in ("codex", "claude", "pi"):
                raise ValueError("未知本地 Agent")
            return self.store.save_settings(values)
        if method == "settings.import_legacy":
            return self._import_legacy(p)
        if method == "app.shutdown":
            self.closing.set()
            return {"closing": True}
        raise ValueError("未知桌面接口")

    def _import_legacy(self, params):
        """Import directory settings only; local Agent auth replaces gateway setup."""
        from .. import config, settings
        source = Path(params.get("path") or config.PROJECT_ROOT / ".env").expanduser()
        if not source.is_file():
            raise ValueError("请选择旧程序的 .env 配置文件")
        values = {}
        for raw in source.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value.strip('"')
            else:
                value = value.strip("'")
            values[key.strip()] = value
        project_id = params.get("project_id")
        if not project_id:
            raise ValueError("请先选择要导入的项目")
        mapping = {"reference": "PYCUT_REFERENCE_DIR", "background": "PYCUT_CLIP_DIR",
                   "pip": "PYCUT_PIP_DIR", "intro": "PYCUT_INTRO_DIR", "sticker": "PYCUT_STICKER_DIR"}
        imported = 0
        for library in self.store.libraries(project_id):
            value = values.get(mapping[library["role"]], "")
            if value:
                paths = json.loads(value) if value.startswith("[") else [value]
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                    raise ValueError("旧配置中的目录格式无效")
                roots = settings.directories([str(path if path.is_absolute() else source.parent / path)
                                              for raw in paths if raw.strip()
                                              for path in [Path(raw).expanduser()]])
                self.store.add_roots(library["id"], [str(root) for root in roots])
                imported += len(roots)
        output = values.get("PYCUT_OUTPUT_DIR", "")
        if output:
            path = Path(output).expanduser()
            self.store.update_project(project_id, output_dir=str(path if path.is_absolute() else source.parent / path))
        return {"imported_directories": imported, "message": "已导入目录配置；识别复用本地 Agent 登录，无需导入密钥"}

    def close(self):
        self.closing.set()
        self.jobs.shutdown()
