# MCP transport for Context Forge

The optional MCP server (`context_forge.mcp_server`) ships five tools
listed in §15 of the technical design:

- `context_forge_search(query)` — FTS5 lookup over the vault.
- `context_forge_get(path)` — read a Markdown file (path-validated).
- `context_forge_pending_reviews()` — list drafts awaiting approval.
- `context_forge_match_rules(project, changed_path)` — explain rule hits.
- `context_forge_record_feedback(rule_id, outcome, note)` — log outcome.

The server uses stdio transport. It runs from the user shell with
`python -m context_forge.mcp_server` and never opens a network port.

## Claude Desktop

Add this block to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "context_forge": {
      "command": "python",
      "args": ["-m", "context_forge.mcp_server"],
      "cwd": "/path/to/your/repo"
    }
  }
}
```

The `cwd` should point at the repository that contains `context_forge/`.
Claude Desktop will launch the server with that working directory so
that `_store` and `_vault` resolve to the right per-user locations.

## Claude Code

User-scope servers live in `~/.claude.json` (relocated by `CLAUDE_CONFIG_DIR`).
Do **not** hand-edit that file: Claude Code owns its location, its lock, and a
guard (GH #3117) that refuses a write which would drop auth still in its
in-memory cache. Register through the host's own CLI instead:

```bash
claude mcp add --scope user context_forge \
  -e PYTHONPATH=/path/to/your/repo -- python -m context_forge.mcp_server
```

`forge mcp-install` runs exactly that command for you, and
`forge mcp-install --uninstall` runs `claude mcp remove context_forge -s user`.
`PYTHONPATH` rather than `cwd` makes the package importable from any directory;
`memory.db` resolves from the user runtime dir, not cwd. Both are idempotent: a
repeat add of an existing name is treated as success, and removing an absent
one reports `removed: 0` without raising. A stale `context_forge` entry in the
inert `~/.claude/settings.json` (the host does not read `mcpServers` there) is
dropped on install, with a backup.

The MCP server shares the application service with the CLI — no
business logic is duplicated. The five legacy tools are read-mostly: only
`context_forge_record_feedback` writes to SQLite. Approving knowledge
or enabling rules still requires the CLI so humans stay in the loop.

## Proven vs not proven

The stdio transport is verified end to end by
`tests/test_mcp_transport.py`, which launches the server as a **child
process** and speaks newline-delimited JSON-RPC 2.0 over its stdio.

| Proven | Evidence |
|--------|----------|
| Tools are registered and enumerable | e2e `tools/list` returns the tool names |
| The real stdio transport works | e2e child-process `initialize` ↔ response round-trip |
| A real `memory.db` returns real memories | e2e `tools/call` asserts the returned body text written to a temp database |
| The host registers the entry | `forge mcp-install` tests assert the exact `claude mcp add` argv (the host owns the write, so there is no file for us to corrupt); live `claude mcp get context_forge` reports `Status: ✔ Connected` |

| **Not proven** | Why |
|----------------|-----|
| The model will call a tool *when it needs to* | Behavioural; depends on the model and its prompt, not falsifiable by a test |
| Recall quality is better on real tasks | Needs live samples; the P2 experiment is a proxy on a deterministic corpus |

The second row is not a formality: it means the 09-21 acceptance
criterion (“prove the host retrieves when needed; proving the MCP is
callable is not enough”) **stays open**. This task proves the transport and
the data, not the model's choice to use them.

## Verifying the install

```bash
python -c "from context_forge.mcp_server import create_server; \
    srv = create_server(); print(sorted(srv._tool_manager._tools))"
```

Should print the tool names above. If `mcp` is not installed,
install it with `pip install mcp` (already listed under
`[project.optional-dependencies]`). `forge doctor` reports whether the
`mcp` package is importable.
