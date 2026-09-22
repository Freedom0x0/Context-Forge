"""Compose a single-context view for AI or human consumption.

This module is the single place that knows the shape — matched rules,
recent accepted knowledge, and recent reviews, scoped by project and
optional path — so adding or removing a section happens here only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContextSection:
    title: str
    body: str


def build_context(vault_root: Path, project: str,
                   changed_path: str | None = None,
                   knowledge_limit: int = 5,
                   review_limit: int = 5,
                   rule_limit: int = 8) -> list[ContextSection]:
    """Return ordered context sections for a project + optional path.

    `rule_limit` caps the matched-rule section (0 = no cap).
    """
    from .index import parse_frontmatter
    from .rules import RuleMatcher

    sections: list[ContextSection] = []
    rule_lines = []
    for match in RuleMatcher(vault_root).match(project, changed_path,
                                               limit=rule_limit):
        rule_lines.append(f"- [{match.rule_id}] {match.reason}\n  {match.instruction}")
    if rule_lines:
        sections.append(ContextSection("Matched rules", "\n".join(rule_lines)))
    else:
        sections.append(ContextSection("Matched rules", "(none)"))

    knowledge_lines: list[str] = []
    for path in sorted(vault_root.glob("knowledge/accepted/*.md"),
                       reverse=True)[:knowledge_limit]:
        front, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        title = front.get("id", path.stem)
        knowledge_lines.append(f"- {title}: {path.relative_to(vault_root)}")
    sections.append(ContextSection(
        "Recent knowledge",
        "\n".join(knowledge_lines) if knowledge_lines else "(none)",
    ))

    review_lines: list[str] = []
    for path in sorted(vault_root.glob("reviews/**/*.md"),
                       reverse=True)[:review_limit]:
        front, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        title = front.get("id", path.stem)
        snippet = body.strip().splitlines()[0][:120] if body.strip() else ""
        review_lines.append(f"- {title}: {snippet}")
    sections.append(ContextSection(
        "Recent reviews",
        "\n".join(review_lines) if review_lines else "(none)",
    ))

    return sections


def render(sections: list[ContextSection]) -> str:
    """Format sections as plain text suitable for LLM prompts."""
    parts = [f"# Context: {sections and sections[0].title or 'unknown'}"]
    for section in sections:
        parts.append(f"\n## {section.title}\n\n{section.body}")
    return "\n".join(parts).rstrip() + "\n"
