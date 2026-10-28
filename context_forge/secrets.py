"""One definition of what counts as a credential.

Two paths ask that question — the outbound sanitizer (whatever leaves this machine
for a model) and the vault privacy tag — and they used to answer it with different
patterns. The weaker answer sat on the outbound path: bare key shapes such as
`sk-…`, `ghp_…` and `AKIA…` were caught before a vault tag but shipped verbatim to
the remote model.

The only real difference between the two callers is the assignment threshold. A
redacted token costs nothing, so the outbound path catches any `key=value`; tagging
a whole document private is heavier, so it wants a value long enough that Chinese
prose after a colon does not trip it.
"""

from __future__ import annotations

import re

_ASSIGNMENT = r"(?i)(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[\"']?"


def _patterns(min_assignment_value: int) -> tuple[tuple[re.Pattern[str], str], ...]:
    return (
        (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"), r"\1[REDACTED]"),
        (re.compile(_ASSIGNMENT + r"[^\s,;'\"]{%d,}" % min_assignment_value),
         r"\1=[REDACTED]"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                    re.S), "[REDACTED:private_key]"),
        (re.compile(r"(?i)(postgres|mysql|mongodb)(://)[^\s\"']+"), r"\1\2[REDACTED]"),
        # Key shapes that need no `key=` prefix: they appear bare in tool output and
        # in a shell history, which is exactly what a transcript slice holds.
        (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED:api_key]"),
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github_token]"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
         "[REDACTED:jwt]"),
    )


_REDACT_PATTERNS = _patterns(1)
_DETECT_PATTERNS = _patterns(8)


def redact(text: str) -> str:
    """Replace credential-shaped content with a placeholder."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(text: str) -> bool:
    """True when the text carries something credential-shaped."""
    return any(pattern.search(text) for pattern, _ in _DETECT_PATTERNS)
