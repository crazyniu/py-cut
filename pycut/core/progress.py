"""Structured task progress; enabled only by the terminal runner."""
import json
import os

PREFIX = '@pycut-progress '


def progress(stage: str, completed: int, total: int, **fields) -> None:
    if os.environ.get('PYCUT_PROGRESS') == '1':
        print(PREFIX + json.dumps(dict(stage=stage, completed=completed, total=total, **fields),
                                  ensure_ascii=False), flush=True)


def parse(line: str) -> dict | None:
    if not line.startswith(PREFIX):
        return None
    try:
        value = json.loads(line[len(PREFIX):])
        if (isinstance(value, dict) and isinstance(value.get('stage'), str)
                and all(isinstance(value.get(key, 0), int) and value.get(key, 0) >= 0
                        for key in ('completed', 'total', 'success', 'failed', 'skipped', 'review'))
                and all(isinstance(value.get(key, ''), str) for key in ('detail', 'output'))):
            return value
    except ValueError:
        pass
    return None


def completed_futures(tasks, on_wait):
    """Yield completed futures while emitting progress during slow background requests."""
    from concurrent.futures import wait, FIRST_COMPLETED
    pending = set(tasks)
    while pending:
        done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
        if not done:
            on_wait()
        yield from done
