"""Item-scoped desktop activity and FFmpeg encoding telemetry.

Counts describe only the current encoding stage, never whole-video completion.
The caller owns publication and terminal status. Telemetry owns its temporary
file and reporter; unavailable telemetry leaves the encoding command unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import tempfile
import threading

from .progress import progress


_item = threading.local()
_output_lock = threading.Lock()


def bind_item(item_id: str | None) -> None:
    """Bind a processing worker thread; pass None when leaving its item scope."""
    _item.identifier = item_id or ""


def current_item() -> str:
    return getattr(_item, "identifier", "")


def _enabled(item_id: str) -> bool:
    return bool(item_id) and os.environ.get("PYCUT_DESKTOP_JOB") == "1" and os.environ.get("PYCUT_PROGRESS") == "1"


def report(completed: int, total: int, label: str = "生成中", *, item_id: str | None = None) -> None:
    """Emit measured stage units; explicit item IDs support reporter threads."""
    identifier = current_item() if item_id is None else item_id
    if _enabled(identifier):
        with _output_lock:
            progress(label, max(0, int(completed)), max(0, int(total)),
                     item_progress=True, item_id=identifier)


def activity(label: str) -> None:
    """Emit indeterminate activity without inventing elapsed-work percentages."""
    report(0, 0, label)


def _microseconds(path: Path) -> int | None:
    try:
        with path.open("rb") as stream:
            # Read only a bounded tail; incomplete records never count as work.
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - 65536))
            data = stream.read().decode("ascii", "replace")
    except OSError:
        return None
    values = re.findall(r"(?m)^out_time_us=(\d+)\r?\n", data)
    return int(values[-1]) if values else None


@contextmanager
def encoding_progress(cmd: list[str], total_seconds: float):
    """Yield FFmpeg progress-file arguments and clean up on every context exit.

    The original process runner retains timeout, cancellation and diagnostics.
    ``out_time_us`` is FFmpeg output media time, compared with the known expected
    encoding duration. It is unrelated to wall-clock time or publication success.
    """
    identifier = current_item()
    if not _enabled(identifier):
        yield cmd
        return
    activity("生成中")
    total = int(total_seconds * 1_000_000)
    if total <= 0 or not cmd:
        yield cmd
        return
    try:
        descriptor, name = tempfile.mkstemp(prefix="pycut-encode-", suffix=".progress")
    except OSError:
        yield cmd
        return
    os.close(descriptor)
    path = Path(name)
    finished = threading.Event()
    previous = -1

    def sample():
        nonlocal previous
        measured = _microseconds(path)
        if measured is not None and measured > previous:
            previous = measured
            report(measured, total, item_id=identifier)

    def monitor():
        while not finished.wait(.2):
            sample()

    reporter = threading.Thread(target=monitor, name="pycut-encoding-progress", daemon=True)
    try:
        reporter.start()
    except RuntimeError:
        path.unlink(missing_ok=True)
        yield cmd
        return
    try:
        yield [*cmd[:-1], "-stats_period", "0.5", "-progress", str(path), cmd[-1]]
    finally:
        finished.set()
        reporter.join()
        try:
            sample()
        finally:
            path.unlink(missing_ok=True)
