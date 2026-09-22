"""Markdown vault and external-edit aware index.

Markdown is the source of truth for content. The SQLite FTS5 index is
rebuildable and only used for search. `index.md` is a generated nav file
and must never be edited by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class VaultDocument:
    """Parsed view of a Markdown file in the vault."""

    path: Path
    frontmatter: dict[str, str]
    body: str
    content_hash: str


class DocumentIndex:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._ensure_documents()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS rebuild_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
            "finished_at TEXT, files INTEGER, ok INTEGER NOT NULL)"
        )
        self.db.commit()

    def _ensure_documents(self) -> None:
        """Create the FTS table, recreating an older shape if found.

        The index is derived and rebuildable, so dropping an outdated
        shape is safe: the next `rebuild()` refills it.
        """
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(documents)")}
        if columns and "project" not in columns:
            self.db.execute("DROP TABLE documents")
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5("
            "id UNINDEXED, path UNINDEXED, project UNINDEXED, content)"
        )

    def rebuild(self, root: Path) -> int:
        documents = self._walk(root)
        started = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute("DELETE FROM documents")
            self.db.executemany(
                "INSERT INTO documents(id, path, project, content) VALUES (?, ?, ?, ?)",
                [
                    (str(doc.path.relative_to(root)), str(doc.path.relative_to(root)),
                     doc.frontmatter.get("project", ""), doc.body)
                    for doc in documents
                ],
            )
            finished = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "INSERT INTO rebuild_history(started_at, finished_at, "
                "files, ok) VALUES (?, ?, ?, 1)",
                (started, finished, len(documents)),
            )
        return len(documents)

    def rebuild_stats(self, days: int = 7) -> dict[str, int | float]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS ok_count "
            "FROM rebuild_history WHERE started_at >= ?", (cutoff,),
        ).fetchone()
        total = row["total"] or 0
        ok_count = row["ok_count"] or 0
        return {
            f"rebuilds_{days}d": total,
            f"rebuilds_ok_{days}d": ok_count,
            f"rebuild_success_rate_{days}d": ok_count / total if total else 0.0,
        }

    def search(self, query: str, limit: int = 20,
               project: str | None = None) -> list[sqlite3.Row]:
        """Full-text hits, optionally narrowed to one project.

        `project=None` keeps the old unfiltered behaviour; `limit` <= 0
        lifts the cap, as in `RuleMatcher.match`.
        """
        # snippet() takes a 0-based column index; a name string coerces to 0
        sql = ("SELECT path, snippet(documents, 3, '[', ']', '...', 24) AS snippet "
               "FROM documents WHERE documents MATCH ?")
        params: list[object] = [query]
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.db.execute(sql, params))

    @staticmethod
    def _walk(root: Path) -> list[VaultDocument]:
        docs: list[VaultDocument] = []
        for path in root.rglob("*.md"):
            if path.name == "index.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            front, body = parse_frontmatter(text)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            docs.append(VaultDocument(path, front, body, digest))
        return docs


def yaml_scalar(value: object) -> str:
    """Render one frontmatter value as a YAML-safe plain or quoted scalar.

    A plain scalar silently breaks the whole block when it contains `: `
    or ` #`, ends with `:`, or when it starts with an indicator (`paths: **/*`
    parses as an alias, `sources: a quote: x` as a mapping, `x: |pipe` as a
    block scalar). Empty is quoted so it stays a string, not null. Values that
    are already a flow collection (`[...]`, `{...}`) are left alone.
    """
    text = "" if value is None else str(value)
    if text and text[:1] in "[{" and text[-1:] in "]}":
        return text
    if (not text or text[0] in "-?:,[]{}#&*!|>'\"%@`"
            or re.search(r"(:\s)|(\s#)|(:$)|[\n\r]", text)):
        return json.dumps(text, ensure_ascii=False)
    return text


def parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Tiny YAML frontmatter reader; avoids pulling PyYAML for an MVP.

    Returns (values, body). Values cover `key: value` lines only; lists are
    not expanded. Good enough for status / id / type / project checks; the
    compiler will use python-frontmatter when a stricter parser is needed.
    """
    if not content.startswith("---\n"):
        return {}, content
    marker = content.find("\n---", 4)
    if marker < 0:
        return {}, content
    values: dict[str, str] = {}
    for line in content[4:marker].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values, content[marker + 4:].lstrip("\n")


def write_index(vault_root: Path) -> Path:
    """Generate the nav `index.md`. The file is owned by the system."""
    target = vault_root / "index.md"
    lines = ["---", "type: index", "generated_at: " + _today(), "---", "",
             "# Context Forge Index", ""]
    for section in ("reviews", "knowledge", "rules"):
        sub = vault_root / section
        if not sub.exists():
            continue
        lines.append(f"## {section}")
        lines.append("")
        for doc in sorted(sub.rglob("*.md")):
            try:
                front, _ = parse_frontmatter(doc.read_text(encoding="utf-8"))
            except OSError:
                continue
            status = front.get("status", "unknown")
            doc_id = front.get("id", doc.stem)
            rel = doc.relative_to(vault_root)
            lines.append(f"- [{doc_id}]({rel.as_posix()}) — {status}")
        lines.append("")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    return target


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
