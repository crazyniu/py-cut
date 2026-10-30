"""Copy source bytes unchanged and append 50–500 KiB of random trailing data.

Keep the original extension and number outputs without replacing existing files.
Private copy files are removed on failure or cancellation; no media is re-encoded.
"""
from pathlib import Path
import os
import secrets
import tempfile

from .runtime import check_stop


def copy_with_random_tail(source: Path, output_dir: Path) -> Path:
    check_stop()
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fission-", suffix=".tmp", dir=output_dir)
    try:
        with os.fdopen(fd, "wb") as output:
            with source.open("rb") as original:
                while chunk := original.read(1024 * 1024):
                    check_stop()
                    output.write(chunk)
            output.write(os.urandom((50 + secrets.randbelow(451)) * 1024))
        check_stop()
        number = 1
        target = output_dir / f"{source.stem}_{number}{source.suffix}"
        while target.exists():
            number += 1
            target = output_dir / f"{source.stem}_{number}{source.suffix}"
        os.replace(temporary, target)
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
