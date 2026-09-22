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
                                    request_id: str = "") -> list[dict]:
        from .memory_recall import MemoryRecall
        results = MemoryRecall(_memory()).search(
            query, project=project or None, changed_path=changed_path or None,
            limit=None if limit == 0 else limit, request_id=request_id or None,
            stage="tool_returned",
        )
        return [result.as_dict() for result in results]

    @server.tool()
    def context_forge_memory_get(memory_id: str, version: int = 0) -> dict:
        from .memory_recall import MemoryRecall
        result = MemoryRecall(_memory()).get(memory_id, version=version or None)
        if result is None:
            raise KeyError(memory_id)
        return result.as_dict()

    @server.tool()
    def context_forge_memory_report(similarity_threshold: float = 0.6) -> dict:
        from .memory_merge import MemoryMerge
        return MemoryMerge(_memory()).report(similarity_threshold=similarity_threshold).as_dict()

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
