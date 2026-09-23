"""One-way sync from the Gen1 vault into the Gen2 `memory.db`.

`memory.db` is the Gen2 authority; the vault is the Gen1 fact source. This
module only reads the vault and writes the database — never the reverse. It
reuses `import_vault` (already incremental and idempotent) instead of
re-implementing the import.

`snapshot_order` is a persisted counter, never a timestamp: a wall clock
that moves backwards would make a changed snapshot look older than the one
already imported, which `import_vault` rejects.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .index import iter_vault_documents


@dataclass(frozen=True)
class SyncReport:
    created: int
    updated: int
    unchanged: int
    protected: int
    vault_documents: int
    db_records: int
    snapshot_order: int
    diagnostics: tuple[str, ...]


def _home() -> Path:
    from .config import runtime_dir
    return runtime_dir()


def _db_path(home: Path) -> Path:
    return home / "memory.db"


def _feedback_path(home: Path) -> Path | None:
    path = home / "queue.db"
    return path if path.exists() else None


def _counter_path(home: Path) -> Path:
    return home / "sync-order"


def _error_path(home: Path) -> Path:
    return home / "sync-error"


def _read_counter(home: Path) -> int:
    path = _counter_path(home)
    if not path.exists():
        return 0
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (ValueError, OSError):
        return 0


def _write_counter(home: Path, value: int) -> None:
    path = _counter_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8", newline="\n")


def record_failure(message: str) -> None:
    """Persist the last sync failure where `forge doctor` can see it."""
    path = _error_path(_home())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message, encoding="utf-8", newline="\n")


def clear_failure() -> None:
    _error_path(_home()).unlink(missing_ok=True)


def read_failure() -> str | None:
    path = _error_path(_home())
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def vault_document_count(root: Path | None) -> int:
    """Markdown documents import_vault would consider, without parsing them."""
    if root is None:
        return 0
    root = Path(root)
    if not root.is_dir():
        return 0
    return sum(1 for _ in iter_vault_documents(root))


def _prev_snapshot_order(db_path: Path, namespace: str) -> int | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            row = connection.execute(
                "SELECT snapshot_order FROM import_snapshots WHERE namespace=?", (namespace,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _db_record_count(db_path: Path, namespace: str) -> int:
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM import_items WHERE namespace=?", (namespace,)).fetchone()
    except sqlite3.Error:
        return 0
    return row[0] if row else 0


def consistency(root: Path | None) -> dict:
    """Compare the vault against `memory.db`; read-only, never raises on bad data."""
    home = _home()
    db_path = _db_path(home)
    documents = vault_document_count(root)
    namespace = str(Path(root).resolve()) if root is not None else ""
    records = _db_record_count(db_path, namespace) if root is not None else 0
    return {
        "missing": not db_path.exists(),
        "vault_documents": documents,
        "db_records": records,
        "failure": read_failure(),
    }


def _is_stale(report: dict) -> bool:
    return any("stale or changed snapshot" in line for line in report["diagnostics"])


def sync(root: Path, *, dry_run: bool = True) -> SyncReport:
    """Import `root` (a Gen1 vault) into `memory.db`; incremental and idempotent."""
    from .memory import MemoryStore
    from .memory_import import import_vault

    root = Path(root).resolve()
    home = _home()
    db_path = _db_path(home)
    feedback = _feedback_path(home)
    namespace = str(root)
    documents = vault_document_count(root)

    if not db_path.exists():
        if dry_run:
            return SyncReport(0, 0, 0, 0, documents, 0, _read_counter(home),
                              ("memory.db missing; run forge sync (without --dry-run)",))
        store = MemoryStore.initialize(db_path)
    else:
        store = MemoryStore(db_path, readonly=False)

    previous = _prev_snapshot_order(db_path, namespace)
    order = max(previous if previous is not None else 0, _read_counter(home))
    report = import_vault(store, root, feedback_db=feedback, dry_run=dry_run,
                          snapshot_order=order)
    if _is_stale(report):
        order = (previous if previous is not None else -1) + 1
        report = import_vault(store, root, feedback_db=feedback, dry_run=dry_run,
                              snapshot_order=order)

    changed = report["created"] + report["updated"] > 0
    if not dry_run and changed:
        _write_counter(home, order + 1)

    return SyncReport(
        created=report["created"], updated=report["updated"],
        unchanged=report["unchanged"], protected=report["protected"],
        vault_documents=documents,
        db_records=_db_record_count(db_path, namespace) if db_path.exists() else 0,
        snapshot_order=order, diagnostics=tuple(report["diagnostics"]),
    )
