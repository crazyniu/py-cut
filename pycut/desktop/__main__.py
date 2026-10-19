"""JSON-lines desktop service and private disposable processing worker entry."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading


def data_directory():
    if os.environ.get("PYCUT_DATA_DIR"):
        return Path(os.environ["PYCUT_DATA_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/com.pycut.desktop"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "pycut"


def worker(identifier):
    from .engine import emit, run_job
    from .store import Store
    from .jobs import sanitize
    store = Store(data_directory())
    try:
        with store.connect() as db:
            row = db.execute("SELECT request_json FROM desktop_jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        request = json.loads(row[0])
        result = run_job(request, store.data_dir, store)
    except InterruptedError:
        result = {"status": "cancelled"}
    except (Exception, SystemExit) as exc:
        result = {"status": "failed", "error": sanitize(str(exc) or type(exc).__name__)}
    emit("result", result)
    return 0 if result["status"] in ("completed", "review") else 130 if result["status"] == "cancelled" else 2


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return worker(sys.argv[2])
    if len(sys.argv) > 1:
        raise SystemExit("Unknown private desktop invocation")
    from .service import Service
    from .jobs import sanitize
    lock = threading.Lock()
    def send(value):
        with lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    service = Service(data_directory(), send)
    def dispatch(request):
        identity = request.get("id")
        try:
            result = service.dispatch(request["method"], request.get("params", {}))
            send({"id": identity, "result": result})
        except Exception as exc:
            send({"id": identity, "error": {"message": sanitize(str(exc))}})
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="desktop-rpc")
    try:
        while not service.closing.is_set():
            line = sys.stdin.readline(2 * 1024 * 1024)
            if not line:
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or "id" not in request or not isinstance(request.get("method"), str):
                    raise ValueError("Invalid request")
            except ValueError:
                send({"id": None, "error": {"message": "无效的桌面请求"}})
                continue
            if request["method"] == "app.shutdown":
                dispatch(request)
                break
            executor.submit(dispatch, request)
    finally:
        service.close()
        executor.shutdown(wait=True, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
