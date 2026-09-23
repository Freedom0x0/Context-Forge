"""Markdown vault: drafts, knowledge, rules.

Auto-loop writes here. The worker auto-publishes up to two files per
session: an `accepted/` knowledge item, and an `enabled` rule.

User actions are file-based: edit content directly, archive by
deleting, enable rules by changing `status: proposed` to `enabled`
in the frontmatter. The worker never auto-enables a rule.

`user_owned: true` in frontmatter protects a hand-written entry from
being overwritten by a future auto-write with the same id.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from .domain import (
    KnowledgeItem,
    RuleExtraction,
    SessionArtifacts,
)
from .index import iter_vault_documents, parse_frontmatter, yaml_scalar


class Vault:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("path escapes vault root")
        return target

    @staticmethod
    def _atomic_write(target: Path, content: str) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".forge-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    @staticmethod
    def _frontmatter_lines(values: dict[str, object]) -> str:
        return "\n".join(f"{k}: {yaml_scalar(v)}" for k, v in values.items())

    # -- read helpers -----------------------------------------------------

    def is_user_owned(self, path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        front, _ = parse_frontmatter(text)
        return str(front.get("user_owned", "")).lower() == "true"

    def list_conflicts(self) -> list[Path]:
        return sorted(self.root.rglob("*.conflict-*.md"))

    def list_rules(self, status: str | None = None) -> list[Path]:
        results: list[Path] = []
        for path in sorted(self.root.glob("rules/**/*.md")):
            if status is None:
                results.append(path)
                continue
            try:
                front, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if front.get("status") == status:
                results.append(path)
        return results

    def read_rule(self, rule_id: str) -> tuple[Path, dict[str, str], str] | None:
        for path in sorted(self.root.glob(f"rules/**/{rule_id}.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            front, body = parse_frontmatter(text)
            return path, front, body
        return None

    # -- writes ----------------------------------------------------------

    def write_knowledge_auto(self, session_id: str, knowledge: KnowledgeItem,
                              artifacts: SessionArtifacts) -> Path:
        """Auto-publish to `knowledge/accepted/`. Skipped if user-owned.

        Returns the path if written, raises FileExistsError if the
        existing file is user-owned (which we must not overwrite).
        """
        target = self._safe(f"knowledge/accepted/{knowledge.id}.md")
        if target.exists() and self.is_user_owned(target):
            raise FileExistsError(
                f"knowledge {knowledge.id} is user_owned; skip"
            )
        front = {
            "type": "knowledge",
            "id": knowledge.id,
            "session_id": session_id,
            "status": "accepted",
            "confidence": knowledge.confidence,
            "auto_generated": "true",
            "role": knowledge.role,
            "technologies": ",".join(knowledge.technologies),
            "domains": ",".join(knowledge.domains),
            "exclusions": ",".join(knowledge.counterexamples),
            "topics": ",".join(knowledge.topics),
            "sources": ",".join(knowledge.sources),
        }
        body = self._render_artifacts_body(artifacts, knowledge_only=knowledge)
        return self._atomic_write(target, f"---\n{self._frontmatter_lines(front)}\n---\n\n{body}")

    def write_rule_candidate(self, session_id: str, knowledge_id: str,
                              project: str, rule: RuleExtraction) -> Path:
        """Auto-write to `rules/proposals/` with status=enabled.

        Rules auto-enable because the model judged the knowledge good
        enough to publish — the gate is disabling, not enabling. Users
        can change `status: enabled` → `disabled` or delete the file.
        """
        rule_id = f"rule-{knowledge_id}"
        target = self._safe(f"rules/proposals/{rule_id}.md")
        if target.exists() and self.is_user_owned(target):
            raise FileExistsError(f"rule {rule_id} is user_owned; skip")
        front = {
            "type": "rule",
            "id": rule_id,
            "session_id": session_id,
            "source_knowledge_id": knowledge_id,
            "project": project,
            "status": "enabled",
            "confidence": rule.confidence,
            "auto_generated": "true",
            "role": rule.role,
            "technologies": ",".join(rule.technologies),
            "domains": ",".join(rule.domains),
            "exclusions": ",".join(rule.counterexamples),
            "paths": ",".join(rule.paths),
        }
        return self._atomic_write(
            target,
            f"---\n{self._frontmatter_lines(front)}\n---\n\n{rule.instruction.strip()}\n",
        )

    # -- housekeeping -----------------------------------------------------

    def list_knowledge(self, status: str | None = None) -> list[Path]:
        results: list[Path] = []
        for path in sorted(self.root.glob("knowledge/**/*.md")):
            if status is None:
                results.append(path)
                continue
            try:
                front, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if front.get("status") == status:
                results.append(path)
        return results

    def update_with_expected_hash(self, relative: str, content: str,
                                  expected_hash: str) -> Path:
        target = self._safe(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if current_hash != expected_hash:
                from datetime import datetime
                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                conflict = target.with_suffix(target.suffix + f".conflict-{stamp}")
                conflict.write_text(content, encoding="utf-8", newline="\n")
                raise FileExistsError(
                    f"vault file changed; conflict copy at {conflict}"
                )
        return self._atomic_write(target, content)

    def scan(self) -> dict[str, int]:
        from .index import DocumentIndex, write_index
        DocumentIndex(self._index_path()).rebuild(self.root)
        write_index(self.root)
        self._scan_rule_feedback()
        return {"files": self._count_markdown(), "index": 1}

    def _scan_rule_feedback(self) -> None:
        """Read `feedback:` from rule frontmatter and emit to the feedback table."""
        from .config import runtime_dir
        from .queue import JobStore
        store = JobStore(runtime_dir() / "queue.db")
        for path in self.root.glob("rules/**/*.md"):
            text = path.read_text(encoding="utf-8")
            front, _ = parse_frontmatter(text)
            fb = str(front.get("feedback", "")).strip().lower()
            if fb not in ("helpful", "harmful", "irrelevant"):
                continue
            rule_id = str(front.get("id", path.stem))
            store.add_rule_feedback(rule_id, fb, note="from frontmatter scan")
            text = text.replace(f"feedback: {fb}", "feedback: ", 1)
            path.write_text(text, encoding="utf-8", newline="\n")

    def _count_markdown(self) -> int:
        return sum(1 for _ in iter_vault_documents(self.root))

    def _index_path(self) -> Path:
        from .index import index_path_for
        return index_path_for(self.root)

    # -- rendering -------------------------------------------------------

    @staticmethod
    def _render_artifacts_body(artifacts: SessionArtifacts,
                                 knowledge_only: KnowledgeItem | None = None) -> str:
        parts: list[str] = [f"# {artifacts.title}", ""]
        if not knowledge_only:
            if artifacts.problem:
                parts += ["## 问题", "", artifacts.problem, ""]
            if artifacts.attempts:
                parts.append("## 尝试")
                parts.append("")
                for attempt in artifacts.attempts:
                    parts.append(f"- {attempt.summary}: {attempt.result}")
                parts.append("")
            if artifacts.outcome:
                parts += ["## 结果", "", artifacts.outcome, ""]
            if artifacts.claims:
                parts.append("## Claims")
                parts.append("")
                for claim in artifacts.claims:
                    evid = ", ".join(claim.evidence_ids) or "none"
                    parts.append(
                        f"- [{claim.kind}] {claim.text} (evidence: {evid})"
                    )
                parts.append("")
            if artifacts.uncertainties:
                parts.append("## 不确定性")
                parts.append("")
                parts += [f"- {u}" for u in artifacts.uncertainties]
                parts.append("")
        if artifacts.knowledge is not None:
            parts += ["## Knowledge", "", artifacts.knowledge.body, ""]
        if artifacts.rule_candidate is not None:
            parts += ["## Rule candidate", "", artifacts.rule_candidate.instruction,
                       ""]
        item = artifacts.rule_candidate or artifacts.knowledge
        if item is not None:
            if item.applicability:
                parts += ["## 适用条件", ""]
                parts += [f"- ({condition.result}) {condition.condition}" for condition in item.applicability]
                parts.append("")
            if item.counterexamples:
                parts += ["## 反例", ""]
                parts += [f"- {text}" for text in item.counterexamples]
                parts.append("")
        return "\n".join(parts).rstrip() + "\n"
