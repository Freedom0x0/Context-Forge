"""MCP server for Claude Desktop / Claude Code to query the vault.

Tools:
  context_forge_search(query, project)  full-text over knowledge + rules
  context_forge_get(path)            read a vault file (path-validated)
  context_forge_match_rules(...)     explain rule hits for a project
  context_forge_recent_knowledge(N)  last N auto-published knowledge items
  context_forge_recent_rules(N)      last N rule candidates (any status)
  context_forge_record_feedback(...) log helpful/harmful/irrelevant on a rule
  context_forge_memory_feedback(...) log feedback on one long-term memory
"""

from __future__ import annotations

from .rules import RuleMatcher


def _memory():
    from .config import runtime_dir
    from .memory import MemoryStore
    from .config import load_settings
    settings = load_settings()
    embedder = None
    if settings and settings.embedding_model:
        from .embedding import LocalEmbedder
        embedder = LocalEmbedder(settings.embedding_model,
                                 dimensions=settings.embedding_dimensions)
    return MemoryStore(runtime_dir() / "memory.db", readonly=False, embedder=embedder)


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


def record_memory_feedback(memory_id: str, outcome: str, *, note: str = "",
                           request_id: str = "") -> dict:
    """Record model-observed feedback on a Gen2 memory.

    `harmful` parks the record as `conflicted` (pending a human review) via the
    absorbing-status guard in `MemoryStore._put`; it never deletes text.

    `request_id` is required: without it every call minted a fresh idempotency
    key, so a retried call counted twice -- and a retried call is exactly what a
    flaky host does. The sibling `context_forge_memory_mark` has always required
    one; two tools on the same store must not disagree about idempotency.
    """
    store = _memory()
    record = store.get(memory_id)
    if record is None:
        raise KeyError(memory_id)
    if not request_id:
        raise ValueError("request_id is required (a retry must not double-count)")
    updated = store.record_feedback(memory_id, outcome=outcome, note=note,
                                    idempotency_key=f"mcp:feedback:{request_id}", actor="model")
    return {"memory_id": updated.id, "version": updated.version,
            "status": updated.draft.status, "weight": store.weight(updated.id)}


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

        A high-risk rule nobody has approved comes back without its text: the item carries
        `confirmation_required`, a summary and a handle. A human answers the handle in their own
        terminal (`forge approve`); only then does `context_forge_memory_confirm` release it.
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
    def context_forge_memory_confirm(token: str) -> dict:
        """Trade a high-risk handle for the rule text recall withheld.

        The handle comes from a recall, and it only works after a human answered it in their own
        terminal with `forge approve`. Answering is not this call: possession of the handle means
        a rule was shown to you, not that a person agreed to it. Each handle releases its text
        once; a second call is refused and counted, and an expired or re-versioned handle is dead.
        """
        return _memory().consume_confirmation(token)

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
    def context_forge_memory_feedback(memory_id: str, outcome: str,
                                      request_id: str, note: str = "") -> dict:
        """Report revoked / harmful / irrelevant on one long-term memory.

        `harmful` parks the record as `conflicted` for a human review — a host
        can flag, never clear a conflict. Suppression and restoration are
        terminal-only (`forge memory suppress` / `restore`), not exposed here.

        `request_id` is required: it makes a retried call a no-op instead of a
        second vote on the same memory.
        """
        from .memory import FEEDBACK_OUTCOMES

        if outcome not in FEEDBACK_OUTCOMES:
            raise ValueError(f'outcome must be one of {", ".join(FEEDBACK_OUTCOMES)}')
        return record_memory_feedback(memory_id, outcome, note=note, request_id=request_id)

    def _correction_target(memory_id: str, version: int = 0):
        """Resolve a memory and the lifecycle, for the correction tools below."""
        from .memory_lifecycle import MemoryLifecycle

        store = _memory()
        record = store.get(memory_id, version or None)
        if record is None:
            raise KeyError(memory_id)
        return store, record, MemoryLifecycle(store)

    @server.tool()
    def context_forge_memory_scope(memory_id: str, visibility: str,
                                   version: int = 0) -> dict:
        """Change who may see a memory, without deleting it.

        `visibility` is `owner` or `shared`. The host may narrow access; it may not
        widen it -- that is the subject's boundary, not the host's call.
        """
        from .memory import access_broader
        from .memory_lifecycle import MemoryLifecycle
        from .memory_models import AccessPolicy

        if visibility not in ('owner', 'shared'):
            raise ValueError('visibility must be owner or shared')
        store = _memory()
        record = store.get(memory_id, version or None)
        if record is None:
            raise KeyError(memory_id)
        access = AccessPolicy(visibility=visibility)
        if access_broader(record.draft, record.draft.model_copy(update={'access': access})):
            raise ValueError('the host may narrow access but not widen it')
        revised = MemoryLifecycle(store).scope(
            memory_id, access, expected_version=record.version,
            idempotency_key=f"mcp:scope:{memory_id}:{record.version}",
            reason='host narrowed access', actor='model')
        return {'memory_id': revised.id, 'version': revised.version,
                'visibility': revised.draft.access.visibility}

    @server.tool()
    def context_forge_memory_support(memory_id: str, excerpt: str, identity: str = "",
                                     kind: str = "observation", version: int = 0) -> dict:
        """Attach supporting evidence to a memory.

        The evidence level rises at most to `supported`: a host may never promote a
        memory to `verified`, which only a real tool observation earns.
        """
        from .memory_lifecycle import source_from_text

        store, record, lifecycle = _correction_target(memory_id, version)
        source = source_from_text(excerpt, identity=identity, kind=kind)
        revised = lifecycle.support(memory_id, source, reason='host supporting evidence',
                                    expected_version=record.version,
                                    idempotency_key=f"mcp:support:{memory_id}:{record.version}",
                                    actor='model')
        return {'memory_id': revised.id, 'version': revised.version,
                'evidence_level': revised.draft.evidence_level,
                'proof_count': revised.draft.proof_count}

    @server.tool()
    def context_forge_memory_weaken(memory_id: str, condition: str, excerpt: str,
                                    identity: str = "", version: int = 0) -> dict:
        """Narrow when a memory applies, without deleting it.

        Use this when the memory is right but its applicability was too broad.
        """
        from .memory_lifecycle import source_from_text

        store, record, lifecycle = _correction_target(memory_id, version)
        source = source_from_text(excerpt, identity=identity, kind='observation')
        revised = lifecycle.weaken(memory_id, source, condition, reason='host narrowing',
                                   expected_version=record.version,
                                   idempotency_key=f"mcp:weaken:{memory_id}:{record.version}",
                                   actor='model')
        return {'memory_id': revised.id, 'version': revised.version,
                'applicability': len(revised.draft.applicability)}

    @server.tool()
    def context_forge_memory_extend(memory_id: str, target_id: str, kind: str,
                                    excerpt: str, identity: str = "",
                                    version: int = 0) -> dict:
        """Link a memory to the one it refines, or that is an exception to it.

        `kind` is `refines` or `exception_to`. The target is not rewritten.
        """
        from .memory_lifecycle import source_from_text

        store, record, lifecycle = _correction_target(memory_id, version)
        source = source_from_text(excerpt, identity=identity, kind='observation')
        revised = lifecycle.extend(memory_id, target_id=target_id, kind=kind, source=source,
                                   reason='host extension', expected_version=record.version,
                                   idempotency_key=f"mcp:extend:{memory_id}:{record.version}",
                                   actor='model')
        return {'memory_id': revised.id, 'version': revised.version, 'kind': kind}

    @server.tool()
    def context_forge_memory_supersede(memory_id: str, body: str, project: str = "",
                                       version: int = 0) -> dict:
        """Replace a memory with a new one. The old one becomes `stale`, not deleted."""
        from .memory_lifecycle import candidate_from_body

        store, record, lifecycle = _correction_target(memory_id, version)
        replacement = candidate_from_body(f"{memory_id}:v{record.version + 1}", body,
                                          project=project or None)
        created = lifecycle.supersede(memory_id, replacement, expected_version=record.version,
                                      idempotency_key=f"mcp:supersede:{memory_id}:{record.version}",
                                      reason='host supersession', actor='model')
        return {'memory_id': created.id, 'version': created.version,
                'superseded': memory_id}

    @server.tool()
    def context_forge_memory_overturn(memory_id: str, body: str, excerpt: str,
                                      identity: str = "", project: str = "",
                                      version: int = 0) -> dict:
        """Record that a memory is wrong: a contradicting memory is created.

        The refuted memory is never deleted and never overwritten -- the conflict
        stays visible until a human settles it.
        """
        from .memory_lifecycle import candidate_from_body, source_from_text

        store, record, lifecycle = _correction_target(memory_id, version)
        source = source_from_text(excerpt, identity=identity, kind='observation')
        replacement = candidate_from_body(f"{memory_id}:overturn:{record.version}", body,
                                          project=project or None, sources=(source,))
        created = lifecycle.overturn(memory_id, replacement, source, reason='host overturn',
                                     expected_version=record.version,
                                     idempotency_key=f"mcp:overturn:{memory_id}:{record.version}",
                                     actor='model')
        return {'memory_id': created.id, 'version': created.version, 'contradicts': memory_id}

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
        from .index import parse_frontmatter

        for path in sorted(v.root.glob("rules/**/*.md"), reverse=True):
            if status:
                values, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
                if values.get("status") != status:
                    continue
            out.append({"path": str(path.relative_to(v.root)),
                        "name": path.stem})
            if len(out) >= limit:
                break
        return out

    @server.tool()
    def context_forge_record_feedback(rule_id: str, outcome: str,
                                      note: str = "", request_id: str = "",
                                      session_id: str = "") -> str:
        if outcome not in {"helpful", "harmful", "irrelevant", "unknown"}:
            raise ValueError("invalid feedback outcome")
        _store().add_rule_feedback(rule_id, outcome, note or None,
                                   session_id=session_id or None,
                                   request_id=request_id or None)
        return "recorded"

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
