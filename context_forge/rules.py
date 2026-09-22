from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    path: Path
    reason: str
    instruction: str


def glob_match(path_str: str, pattern: str) -> bool:
    """Match a path against a glob pattern.

    `**` matches zero or more path segments (including none); single `*`
    matches one segment without separators. Other characters match
    literally.
    """
    import re
    regex_parts = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                if i + 2 < len(pattern) and pattern[i + 2] == "/":
                    regex_parts.append(r"(?:.*/)?")
                    i += 3
                    continue
                regex_parts.append(r".*")
                i += 2
                continue
            regex_parts.append(r"[^/]*")
            i += 1
        elif c == "?":
            regex_parts.append(r"[^/]")
            i += 1
        elif c in ".+(){}|^$\\":
            regex_parts.append(re.escape(c))
            i += 1
        else:
            regex_parts.append(re.escape(c))
            i += 1
    return re.fullmatch("".join(regex_parts), path_str) is not None


_LIST_KEYS = {"paths", "sources", "topics"}


def _frontmatter(content: str) -> tuple[dict[str, object], str]:
    if not content.startswith("---\n"):
        return {}, content
    marker = content.find("\n---", 4)
    if marker < 0:
        return {}, content
    values: dict[str, object] = {}
    for line in content[4:marker].splitlines():
        if not line.strip() or line.startswith(" ") or line.startswith("\t"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in _LIST_KEYS and value and "," in value:
            values[key] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            values[key] = value
    return values, content[marker + 4:].strip()


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _sort_key(paths_hit: bool, metadata: dict[str, object], path: Path
              ) -> tuple[int, int, int, float, str]:
    """Rank: path hit, confidence, enabled status, newest file, then id.

    Path hit is the only relevance signal available here, so it leads;
    mtime is a tiebreak and must never look like relevance. An unstated
    confidence ranks as `medium` (the domain default), never below `low`.
    """
    confidence = str(metadata.get("confidence", "")).strip().lower()
    return (
        0 if paths_hit else 1,
        -_CONFIDENCE_RANK.get(confidence, _CONFIDENCE_RANK["medium"]),
        0 if metadata.get("status") == "enabled" else 1,
        -path.stat().st_mtime,
        str(metadata.get("id", path.stem)),
    )


def _path_patterns(metadata: dict[str, object]) -> list[str]:
    raw = metadata.get("paths", [])
    values = [raw] if isinstance(raw, str) else list(raw or [])
    return [text for text in (str(v).strip() for v in values) if text]


class RuleMatcher:
    def __init__(self, vault_root: Path):
        self.root = vault_root.resolve()

    def match(self, project: str, changed_path: str | None = None,
              limit: int = 8, legacy_filter: bool = False) -> list[RuleMatch]:
        """Rules enabled for `project`, ranked by path hit then confidence.

        `limit` caps the result (0 = no cap) so one project's rules can
        no longer flood the context. `legacy_filter=True` restores the
        old unranked, uncapped dump for callers that need every rule.
        """
        ranked: list[tuple[tuple[int, int, int, float, str], RuleMatch]] = []
        for path in self.root.glob("rules/**/*.md"):
            metadata, instruction = _frontmatter(path.read_text(encoding="utf-8"))
            if metadata.get("status") != "enabled":
                continue
            if metadata.get("project") != project:
                continue
            patterns = _path_patterns(metadata)
            rule_id = str(metadata.get("id", path.stem))
            paths_hit = bool(changed_path) and any(
                glob_match(changed_path, p) for p in patterns)
            if patterns and not paths_hit:
                continue
            reason = f"project={project}"
            if paths_hit:
                reason = (f"project={project}, path={changed_path} matches "
                          f"{', '.join(patterns)}")
            ranked.append((_sort_key(paths_hit, metadata, path),
                           RuleMatch(rule_id, path, reason, instruction)))
        if legacy_filter:
            return [match for _, match in ranked]
        ranked.sort(key=lambda item: item[0])
        matches = [match for _, match in ranked]
        return matches if limit <= 0 else matches[:limit]
