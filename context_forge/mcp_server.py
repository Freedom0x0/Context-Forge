"""MCP server for Claude Desktop / Claude Code to query the vault.

Tools:
  context_forge_search(query, project)  full-text over knowledge + rules
  context_forge_get(path)            read a vault file (path-validated)
  context_forge_match_rules(...)     explain rule hits for a project
  context_forge_recent_knowledge(N)  last N auto-published knowledge items
  context_forge_recent_rules(N)      last N rule candidates (any status)
  context_forge_record_feedback(...) log helpful/harmful/irrelevant on a rule
"""

from __future__ import annotations

from .rules import RuleMatcher


def _memory():
    from .config import runtime_dir
    from .memory import MemoryStore
    return MemoryStore(runtime_dir() / "memory.db", readonly=False)


def _recall():
    from .memory_recall import MemoryRecall
    return MemoryRecall(_memory())


def _tags(raw: str) -> tuple[str, ...]:
    return tuple(tag.strip() for tag in raw.split(",") if tag.strip())


def _stack(root: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from pathlib import Path

    from .tech_detect import detect

    profile = detect(Path(root) if root else Path.cwd())
    return tuple(sorted(profile.technologies)), tuple(sorted(profile.domains))


def _store():
    from .cli import _store as cli_store
    return cli_store()


def _vault():
    from .cli import _vault as cli_vault
    return cli_vault()


def _index():
    from .index import DocumentIndex
    return DocumentIndex(_vault()._index_path())


def create_server():
    """Create the optional MCP server; importing MCP is a deployment choice."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support requires the optional mcp package") from exc

    server = FastMCP("context-forge")

    @server.tool()
    def context_forge_search(query: str,
                             project: str = "") -> list[dict[str, str]]:
        """Full-text lookup; `project` narrows to one project ("" = all)."""
        return [dict(row) for row in _index().search(query, project=project or None)]

    @server.tool()
    def context_forge_memory_search(query: str, project: str,
                                    changed_path: str = "", limit: int = 8,
                                    request_id: str = "", technologies: str = "",
                                    domains: str = "", root: str = "",
                                    legacy_filter: bool = False) -> list[dict]:
        """Scoped recall with a four-part explanation.

        `technologies` / `domains` are comma-separated context tags used for
        applicability scoring and candidate-pool admission. Leave both empty to
        auto-detect them from `root` (the project root, default cwd).
        `legacy_filter` reverts scoring and top-K only (the rollback hatch).
        """
        techs, doms = _tags(technologies), _tags(domains)
        if not techs and not doms:
            techs, doms = _stack(root)
        results = _recall().search(
            query, project=project or None, changed_path=changed_path or None,
            limit=None if limit == 0 else limit, request_id=request_id or None,
            stage="tool_returned",
            technologies=frozenset(techs), domains=frozenset(doms),
            legacy_filter=legacy_filter,
        )
        return [result.as_dict() for result in results]

    @server.tool()
    def context_forge_memory_get(memory_id: str, version: int = 0) -> dict:
        result = _recall().get(memory_id, version=version or None)
        if result is None:
            raise KeyError(memory_id)
        return result.as_dict()

    @server.tool()
    def context_forge_memory_mark(memory_id: str, request_id: str, stage: str,
                                  version: int = 0, session_id: str = "",
                                  note: str = "") -> dict:
        """Report what the host did with a memory: referenced | applied | validated.

        Exposure stages (`retrieved` / `tool_returned` / `presented`) are refused on
        purpose — being shown a memory is not a vote. Re-sending the same
        (request_id, stage, memory, version) is a no-op, so a retry cannot double-count.
        The reply carries the current weight so the caller sees whether the mark moved
        anything: `applied` and `validated` only count once an application record
        attributes them to an outcome.
        """
        from .memory import MARKABLE_STAGES

        store = _memory()
        if stage not in MARKABLE_STAGES:
            raise ValueError(f"stage must be one of {', '.join(MARKABLE_STAGES)}")
        if not request_id:
            raise ValueError("request_id is required (tie the mark to one recall)")
        record = store.get(memory_id, version or None)
        if record is None:
            raise KeyError(memory_id)
        store.record_retrieval(
            record.id, record.version, request_id=request_id, stage=stage,
            session_id=session_id or None,
            idempotency_key=f'{request_id}:{stage}:{record.id}:{record.version}',
            explanation={'source': 'host_mark', **({'note': note} if note else {})},
        )
        return {'memory_id': record.id, 'version': record.version, 'stage': stage,
                'request_id': request_id, 'weight': store.weight(record.id)}

    @server.tool()
    def context_forge_memory_apply(memory_id: str, task_id: str, action: str,
                                   expected_outcome: str, request_id: str,
                                   version: int = 0, actual_outcome: str = "",
                                   validation_source_ids: str = "") -> dict:
        """Report that acting on a memory produced the outcome it promised.

        Attribution is derived here, never taken from the caller: `confirmed` needs a
        reported outcome, evidence identities belonging to that version, and an earlier
        `validated` mark under the same request_id. Anything weaker is stored as
        `plausible` and does not move weight — `withheld_reason` in the reply says which
        condition was missing.
        """
        application, withheld = _memory().record_host_application(
            memory_id=memory_id, task_id=task_id, action=action,
            expected_outcome=expected_outcome, request_id=request_id,
            version=version or None, actual_outcome=actual_outcome,
            validation_source_ids=_tags(validation_source_ids),
        )
        return {'application_id': application.application_id,
                'memory_id': application.memory_id, 'version': application.memory_version,
                'attribution': application.attribution, 'withheld_reason': withheld,
                'weight': _memory().weight(application.memory_id)}

    @server.tool()
    def context_forge_memory_report(similarity_threshold: float = 0.6) -> dict:
        from .memory_merge import MemoryMerge
        return MemoryMerge(_memory()).report(similarity_threshold=similarity_threshold).as_dict()

    @server.tool()
    def context_forge_memory_effectiveness(days: int = 90) -> dict:
        """The three P3 reports: applications, anomaly and tidy candidates."""
        from .memory_effectiveness import MemoryEffectiveness
        return MemoryEffectiveness(_memory(), _store()).report(days=days).as_dict()

    @server.tool()
    def context_forge_memory_hygiene(days: int = 90, min_sources: int = 3,
                                     threshold: float = 0.9) -> dict:
        """The P4 report: consolidation, decay, merge and conflict candidates."""
        from .memory_lifecycle import MemoryLifecycle
        return MemoryLifecycle(_memory()).hygiene_report(
            days=days, min_sources=min_sources, threshold=threshold).as_dict()

    @server.tool()
    def context_forge_get(path: str) -> str:
        target = (_vault().root / path).resolve()
        if target != _vault().root and _vault().root not in target.parents:
            raise ValueError("path escapes vault root")
        return target.read_text(encoding="utf-8")

    @server.tool()
    def context_forge_match_rules(project: str, changed_path: str = "",
                                  limit: int = 8,
                                  legacy_filter: bool = False) -> list[dict[str, str]]:
        """Ranked rule hits for a project (0 = no cap; legacy_filter = old dump)."""
        return [
            {"rule_id": m.rule_id, "reason": m.reason, "instruction": m.instruction}
            for m in RuleMatcher(_vault().root).match(
                project, changed_path or None, limit=limit,
                legacy_filter=legacy_filter)
        ]

    @server.tool()
    def context_forge_recent_knowledge(limit: int = 5) -> list[dict[str, str]]:
        v = _vault()
        out: list[dict[str, str]] = []
        for path in sorted(v.root.glob("knowledge/accepted/*.md"),
                          reverse=True)[:limit]:
            out.append({"path": str(path.relative_to(v.root)),
                        "name": path.stem})
        return out

    @server.tool()
    def context_forge_recent_rules(limit: int = 10,
                                   status: str = "") -> list[dict[str, str]]:
        v = _vault()
        out: list[dict[str, str]] = []
        for path in sorted(v.root.glob("rules/**/*.md"), reverse=True):
            if status and f"status: {status}" not in path.read_text(encoding="utf-8"):
                continue
            out.append({"path": str(path.relative_to(v.root)),
                        "name": path.stem})
            if len(out) >= limit:
                break
        return out

    @server.tool()
    def context_forge_record_feedback(rule_id: str, outcome: str,
                                      note: str = "") -> str:
        if outcome not in {"helpful", "harmful", "irrelevant", "unknown"}:
            raise ValueError("invalid feedback outcome")
        _store().add_rule_feedback(rule_id, outcome, note or None)
        return "recorded"

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
