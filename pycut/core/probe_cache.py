"""Disposable ffprobe metadata cache; files and tool identity invalidate results."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import sqlite3

from .. import config


def _stamp(path: Path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _valid(payload: str) -> bool:
    try:
        data = json.loads(payload)
        streams = data.get('streams', [])
        video = next(stream for stream in streams if stream.get('codec_type') == 'video')
        duration = video.get('duration')
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = float(data.get('format', {}).get('duration', 0))
        return (int(video.get('width', 0)) > 0 and int(video.get('height', 0)) > 0
                and math.isfinite(duration) and duration > 0)
    except (ValueError, TypeError, OverflowError, AttributeError, StopIteration, RecursionError):
        return False


def cached_probe(path: Path, args: list[str], timeout: int, run) -> tuple[int, str, str]:
    """Cache successful metadata only; database failures never fail a media task."""
    path = Path(path)
    try:
        resolved = path.resolve()
        executable = Path(shutil.which(args[0]) or args[0]).resolve()
        signature = json.dumps([_stamp(resolved), str(executable), _stamp(executable), args[:-1]])
    except OSError:
        return run(args, timeout=timeout)
    database = Path(config.MONTAGE_TMP) / 'probe-cache-v1.sqlite3'
    connection = None
    try:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database), timeout=.2)
        connection.execute('CREATE TABLE IF NOT EXISTS probes (path TEXT PRIMARY KEY, signature TEXT NOT NULL, payload TEXT NOT NULL)')
        row = connection.execute('SELECT signature, payload FROM probes WHERE path=?', (str(resolved),)).fetchone()
        if row and row[0] == signature and _valid(row[1]):
            return 0, row[1], ''
    except (OSError, sqlite3.Error):
        pass
    finally:
        if connection is not None:
            connection.close()
    result = run(args, timeout=timeout)
    if result[0] != 0 or not _valid(result[1]):
        return result
    try:
        # A probe racing a modification must not publish metadata for the old file.
        after = json.dumps([_stamp(resolved), str(executable), _stamp(executable), args[:-1]])
        if after != signature:
            return result
        connection = sqlite3.connect(str(database), timeout=.2)
        try:
            with connection:
                connection.execute('INSERT OR REPLACE INTO probes VALUES (?, ?, ?)',
                                   (str(resolved), signature, result[1]))
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        pass
    return result
