"""P0 stop-the-bleeding checks: ranked/capped rules, project-scoped search.

The two regressions under guard: `RuleMatcher.match()` used to return every
enabled rule for a project (78 full instructions into the context), and
`DocumentIndex.search()` used to mix every project's hits into one list.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from context_forge.context import build_context
from context_forge.index import DocumentIndex
from context_forge.rules import RuleMatcher


def write_rule(root: Path, rule_id: str, project: str = "demo",
               confidence: str = "", body: str = "instruction",
               paths: str = "") -> Path:
    target = root / "rules" / "proposals" / f"{rule_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {rule_id}", "type: rule", f"project: {project}",
             "status: enabled"]
    if confidence:
        lines.append(f"confidence: {confidence}")
    lines += [f"paths: {paths}", "---", "", body, ""]
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return target


def write_doc(root: Path, name: str, project: str, body: str) -> Path:
    target = root / "knowledge" / "accepted" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nid: {name}\ntype: knowledge\nproject: {project}\n"
        f"status: accepted\n---\n\n{body}\n",
        encoding="utf-8", newline="\n")
    return target


def flood(root: Path, project: str = "platform-web-main", count: int = 78) -> None:
    for i in range(count):
        path = write_rule(root, f"rule-{i:03d}", project,
                          confidence="high" if i == 0 else "low")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))


def test_rules_limit(tmp_path: Path) -> None:
    """78 same-project rules must be ranked and capped at 8, not dumped."""
    flood(tmp_path)
    matches = RuleMatcher(tmp_path).match("platform-web-main")
    assert len(matches) == 8
    # confidence first (rule-000 is `high`), then newest mtime, then id
    assert [m.rule_id for m in matches] == (
        ["rule-000"] + [f"rule-{i:03d}" for i in range(77, 70, -1)])
    assert len(RuleMatcher(tmp_path).match("platform-web-main", limit=2)) == 2


def test_rules_legacy_filter(tmp_path: Path) -> None:
    """Escape hatch: legacy_filter / limit=0 still return every rule."""
    flood(tmp_path)
    matcher = RuleMatcher(tmp_path)
    assert len(matcher.match("platform-web-main", legacy_filter=True)) == 78
    assert len(matcher.match("platform-web-main", limit=0)) == 78


def test_rule_candidate_writes_confidence(tmp_path: Path) -> None:
    """Auto rules publish `confidence`, or ranking has nothing to read."""
    from context_forge.domain import RuleExtraction
    from context_forge.index import parse_frontmatter
    from context_forge.vault import Vault

    vault = Vault(tmp_path / "vault")
    vault.write_rule_candidate("s1", "k1", "demo",
                               RuleExtraction(instruction="x", confidence="high"))
    vault.write_rule_candidate("s1", "k2", "demo", RuleExtraction(instruction="y"))
    assert [parse_frontmatter(p.read_text(encoding="utf-8"))[0]["confidence"]
            for p in vault.list_rules()] == ["high", "medium"]
    assert RuleExtraction.model_validate(
        {"instruction": "z", "paths": [], "confidence": None}).confidence == "medium"


def test_rules_rank_path_hit_before_confidence(tmp_path: Path) -> None:
    """Relevance leads: a path hit beats a fresher, more confident rule.

    An empty `paths:` value means project-wide (it must not become a
    never-matching glob), and a non-matching glob stays out entirely.
    """
    hit = write_rule(tmp_path, "hit", confidence="low", paths="src/**/*.py")
    write_rule(tmp_path, "wide", confidence="high")
    write_rule(tmp_path, "miss", confidence="high", paths="docs/**")
    os.utime(hit, (1_000_000, 1_000_000))
    assert [m.rule_id for m in RuleMatcher(tmp_path).match(
        "demo", changed_path="src/a.py")] == ["hit", "wide"]


def test_rules_confidence_fallback_ranks_as_medium(tmp_path: Path) -> None:
    """Unstated confidence ranks as medium, never below an explicit low."""
    unstated = write_rule(tmp_path, "unstated")
    write_rule(tmp_path, "low", confidence="low")
    write_rule(tmp_path, "high", confidence="high")
    for name, stamp in (("unstated", 1_000_000), ("high", 2_000_000),
                        ("low", 3_000_000)):
        os.utime(tmp_path / "rules" / "proposals" / f"{name}.md",
                 (stamp, stamp))
    assert [m.rule_id for m in RuleMatcher(tmp_path).match("demo")] == (
        ["high", "unstated", "low"])


def test_search_project_filter(tmp_path: Path) -> None:
    """Hits from other projects must not dilute a project-scoped search."""
    root = tmp_path / "vault"
    write_doc(root, "a", "platform-web-main", "filterOption token for Select")
    write_doc(root, "b", "context-forge", "filterOption token for recall")
    index = DocumentIndex(tmp_path / "index.db")
    assert index.rebuild(root) == 2
    assert [row["path"] for row in index.search("filterOption",
                                               project="context-forge")] == [
        str(Path("knowledge") / "accepted" / "b.md")]
    assert len(index.search("filterOption")) == 2
    assert index.search("filterOption", project="missing") == []
    assert len(index.search("filterOption", limit=0)) == 2


def test_search_upgrades_old_index_shape(tmp_path: Path) -> None:
    """A pre-`project` FTS table is dropped on open and refilled on rebuild."""
    db_path = tmp_path / "index.db"
    old = sqlite3.connect(db_path)
    old.execute("CREATE VIRTUAL TABLE documents USING fts5("
                "id UNINDEXED, path UNINDEXED, content)")
    old.execute("INSERT INTO documents VALUES ('stale.md','stale.md','stale')")
    old.commit()
    old.close()

    root = tmp_path / "vault"
    write_doc(root, "a", "context-forge", "filterOption token")
    index = DocumentIndex(db_path)
    assert "project" in {r[1] for r in index.db.execute(
        "PRAGMA table_info(documents)")}
    assert index.rebuild(root) == 1
    assert [row["path"] for row in index.search("filterOption",
                                               project="context-forge")] == [
        str(Path("knowledge") / "accepted" / "a.md")]


def test_context_rule_limit(tmp_path: Path) -> None:
    """The Matched rules section honours rule_limit (default 8)."""
    flood(tmp_path, project="demo", count=20)

    def rule_lines(limit: int | None = None) -> list[str]:
        kwargs = {} if limit is None else {"rule_limit": limit}
        sections = build_context(tmp_path, "demo", **kwargs)
        body = next(s.body for s in sections if s.title == "Matched rules")
        return [line for line in body.splitlines() if line.startswith("- [")]

    assert len(rule_lines()) == 8
    assert len(rule_lines(3)) == 3
