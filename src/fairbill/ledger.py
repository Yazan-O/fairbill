"""Append-only ledger: every action Fairbill takes, why, and how to take it back.

One JSON-lines file per session at $FAIRBILL_LEDGER_DIR/<sha256(session_id)[:24]>.jsonl
(default _runs/ledger/); the digest keeps two session ids that differ only in punctuation
in two different files. Rows are never rewritten in place: an undo appends a reversal row
and rewrites only the `undone` flag of the row it reverses, through a temp file and
os.replace. Every write holds a per-file threading.Lock (this process) and an OS file lock
on a sidecar .lock file (other processes).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fairbill.schema import LedgerEntry

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO_ROOT / "_runs" / "ledger"

veto_window_minutes = 10

# actions the agent takes silently get a veto window; a read or a user tap is final at once
_SILENT = {"decision", "letter", "calendar"}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def ledger_dir() -> Path:
    return Path(os.environ.get("FAIRBILL_LEDGER_DIR") or DEFAULT_DIR)


def _path(session_id: str) -> Path:
    digest = hashlib.sha256((session_id or "anon").encode("utf-8")).hexdigest()[:24]
    d = ledger_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.jsonl"


def _thread_lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


@contextlib.contextmanager
def _exclusive(path: Path):
    """Serialize every write to one ledger file: threads here, processes on the OS lock."""
    with _thread_lock(path):
        lock_path = path.with_name(path.name + ".lock")
        fh = open(lock_path, "a+b")  # noqa: SIM115 - closed in the finally below
        try:
            fh.seek(0)
            if msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            elif fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fh.seek(0)
                if msvcrt is not None:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_entry(session_id: str, action: str, why: str = "", *, bill_id: str | None = None,
              evidence: list[str] | None = None, undo: str | None = None,
              actor: str = "agent") -> LedgerEntry:
    """Build a row (not yet written). Silent agent actions carry a 10-minute veto window."""
    ts = _now()
    final_after = None
    if actor == "agent" and action in _SILENT and undo is not None:
        final_after = (ts + timedelta(minutes=veto_window_minutes)).isoformat()
    return LedgerEntry(
        id=uuid.uuid4().hex[:12], ts=ts.isoformat(), session_id=session_id, bill_id=bill_id,
        action=action, why=why, evidence=evidence or [], undo=undo, final_after=final_after,
        actor=actor,  # type: ignore[arg-type]
    )


def _write_row(path: Path, entry: LedgerEntry) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(entry.model_dump_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def append(session_id: str, entry: LedgerEntry) -> LedgerEntry:
    path = _path(session_id)
    with _exclusive(path):
        _write_row(path, entry)
    return entry


def record(session_id: str, action: str, why: str = "", **kw) -> LedgerEntry:
    """new_entry + append, the shape every pipeline step uses."""
    return append(session_id, new_entry(session_id, action, why, **kw))


def _read_rows(path: Path) -> list[LedgerEntry]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(LedgerEntry(**json.loads(line)))
    return out


def list(session_id: str) -> list[LedgerEntry]:  # noqa: A001 - the spec names it list
    return _read_rows(_path(session_id))


def _rewrite(path: Path, rows: list[LedgerEntry]) -> None:
    """Replace the file atomically: a reader never sees a half-written ledger."""
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("".join(r.model_dump_json() + "\n" for r in rows))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def undo(session_id: str, entry_id: str) -> LedgerEntry:
    """Mark a row undone and append the reversal row (actor user)."""
    path = _path(session_id)
    with _exclusive(path):
        rows = _read_rows(path)
        target = next((r for r in rows if r.id == entry_id), None)
        if target is None:
            raise KeyError(f"no ledger entry {entry_id} in session {session_id}")
        if target.undo is None:
            raise ValueError(f"entry {entry_id} ({target.action}) cannot be undone")
        if target.undone:
            raise ValueError(f"entry {entry_id} is already undone")
        target.undone = True
        reversal = new_entry(
            session_id, f"undo:{target.action}", target.undo, bill_id=target.bill_id,
            evidence=[f"reverses {target.id}"], undo=None, actor="user")
        rows.append(reversal)
        _rewrite(path, rows)
    return reversal
