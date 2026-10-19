"""Exclusive state ownership and recoverable report repairs."""

from contextlib import closing, contextmanager
import datetime as dt
import os
from pathlib import Path
import shutil
import sqlite3


@contextmanager
def state_lock(state_root: Path):
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / 'pipeline.lock'
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another pipeline process owns this state directory. Stop it before starting a run or repair.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def backup_results(args) -> Path | None:
    database = args.state_root / 'progress.sqlite3'
    if not database.is_file():
        return None
    timestamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = args.state_root / 'backups' / timestamp
    destination.mkdir(parents=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        target = sqlite3.connect(destination / 'progress.sqlite3')
        try:
            source.backup(target)
        finally:
            target.close()
    if args.errors_root.exists():
        shutil.copytree(args.errors_root, destination / 'parsed-errors')
    if args.no_lean_errors_output.is_file():
        shutil.copy2(args.no_lean_errors_output, destination / 'pairs-without-lean-errors.jsonl')
    return destination


def archive_attempt_logs(directory: Path) -> None:
    """Preserve old evidence and start a retry without stale stable filenames."""
    if not directory.is_dir():
        return
    files = [path for path in directory.iterdir() if path.is_file()]
    if not files:
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = directory / 'history' / stamp
    destination.mkdir(parents=True)
    for path in files:
        path.rename(destination / path.name)
