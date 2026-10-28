# Host Integration Contracts (Claude Code MCP registration)

## Scope / Trigger

Applies to `context_forge/mcp_install.py` and `doctor.check_mcp()` — everything that reads or writes the Claude Code host's MCP configuration, or reports on it. Trigger: the host owns this file and its write protocol; we are a guest in it. Any change here must be reasoned from the host's own rule (recorded below), not from what the file looks like on one machine.

## Signatures

- `add_command() -> list[str]` / `remove_command() -> list[str]` — the exact host argv; **the single definition of what "installed" means**. The drift comparison is derived from this, never from a second hand-maintained literal.
- `claude_config_path() -> Path` — **the single implementation** of the host's config-file rule.
- `_stored_entry() -> dict | None` — the entry the host currently stores; `None` for absent or malformed.
- `_entry_matches(stored: dict) -> bool` — intent comparison over `command` / `args` / `env` only.
- `install() -> dict` — `installed` / `already_installed` / `refreshed` / `migrated` / `output`.
- `uninstall() -> dict` — `removed` / `output`.
- `_legacy_settings_path() -> Path` — the inert file the *old* writer used; still migrated on install.
- `forge mcp-install [--uninstall]` — no `--target`: with the write delegated there is no target file to redirect.
- `doctor` row `mcp` → `(name, ok, detail)`.

## Contracts

Locations the host reads, and its precedence (most specific first):

| Scope | File | Key | Approval needed |
|---|---|---|---|
| enterprise | `managed-mcp.json` | top-level `mcpServers` | — |
| local | `claude_config_path()` | `projects["<abs session cwd>"].mcpServers` | — |
| project | `.mcp.json` in the cwd **and every ancestor directory** | top-level `mcpServers` | **required** |
| user | `claude_config_path()` | top-level `mcpServers` | — |

Host lookup order: enterprise → local → project (*only when approved*) → user. An unapproved project entry does not shadow user scope.

The config-file rule (the host's `I0()`): if `<config_dir>/.config.json` exists it wins; otherwise join the **raw `CLAUDE_CONFIG_DIR`** (or the home directory) with `.claude.json`.

> **Warning**: `CLAUDE_CONFIG_DIR` moves the config file *into* the config directory — the join uses the raw env var, not the resolved config dir.
>
> Unset: `~/.claude.json` — a **sibling** of `~/.claude/`. Set to `D:\cfg`: `D:\cfg\.claude.json` — a **child** of it. Anyone reasoning "the file is always a sibling" writes the wrong path for every relocated-config user, which is the same class of silent failure as writing the wrong file entirely.

`settings.json`'s `mcpServers` is **inert**: it is not in the host's settings schema. Only the approval flags live there — `enableAllProjectMcpServers`, `enabledMcpjsonServers`, `disabledMcpjsonServers`.

We install at **user scope** (no approval needed) and identify the package with `env.PYTHONPATH` rather than `cwd`, because `context_forge` is not globally installed while `memory.db` resolves through `config.runtime_dir()` and never through the working directory.

### Writing a host config: one reader/writer, and never over an unreadable file

`forge install-hook` and `forge mcp-install` both edit `~/.claude/settings.json`, and both go
through `host_config.load_config` / `save_config`.

- **A file that exists but does not parse is not empty.** `load_config` raises
  `ConfigUnreadable`; the command reports it and exits non-zero. Reading it as `{}` and writing
  back replaced `env`, `permissions` and every other tool's hooks with a file containing only
  this tool's entries.
- **`save_config` is atomic** (mkstemp + `os.replace`), backs the file up first, and preserves
  its newline style and trailing-newline presence. Text-mode `write_text` rewrote every CRLF
  line as LF and appended a newline, churning the user's whole global config.
- `install_hook.status()` reports an unreadable config as `{"ok": false, "error": …}` instead of
  raising: a diagnostic that raises through its own check is worse than no check.

## Validation & Error Matrix

| Input/state | Required behavior |
|---|---|
| Entry absent | run `add`; `installed: True` |
| Entry present **and equal to intent** | **no subprocess call at all**; `already_installed: True` |
| Entry present but different (moved repo, stale `cwd`-era entry) | `remove` then `add`; report `refreshed: True` — never a bare "already registered" |
| `mcpServers` not a dict, entry not a dict, file missing, invalid JSON, top-level scalar | treat as absent; **never raise** |
| `claude` missing from PATH | `McpInstallError` naming the exact manual command |
| A command we expected to succeed exits non-zero | `McpInstallError` |
| `uninstall` for an absent server | `removed: 0` with **zero subprocess calls** |
| Legacy `context_forge` key in `settings.json` | backed up, then removed on install; reported as `migrated` |

No branch may match on the host CLI's message text. Duplicate `add` is a hard error (`exit 1`), and keying on its English wording would break on any reword or localization.

## Good / Base / Bad Cases

Good: re-run `forge mcp-install` after moving the repo → the stored `env.PYTHONPATH` no longer matches intent → the entry is refreshed and the output says so, instead of claiming it was already registered.

Base: fresh install → `add` only; `doctor` reports `registered in <file> (user scope)`.

Bad: treat the host's duplicate-`add` error as success without comparing the stored entry. A moved repo then prints "already registered" forever while the host silently has no tools and `doctor` still shows `[OK]`.

## Tests Required

- Exact argv equality, full-list not prefix: add (`--scope user`, `-e PYTHONPATH=<repo>`, `--`, `python -m context_forge.mcp_server`) and remove (`-s user`). Mutation: drop `--scope user` → RED.
- Present-and-equal → assert the stub recorded **zero** calls (this is what makes "no spurious refresh" real).
- Drifted `env.PYTHONPATH`, and a stale `cwd`-era entry with no `env` → assert the exact `[remove, add]` order.
- Malformed shapes (`mcpServers` not a dict; entry a str / list / `None` / int; missing file; invalid JSON; top-level scalar) → `None`, no raise.
- `uninstall` absent → zero calls, `removed: 0`.
- The path rule in both forms: default and `CLAUDE_CONFIG_DIR`-relocated.
- **No test may reach the real `~/.claude.json`, `~/.claude/settings.json`, or the live runtime directory.** Redirect the config paths; do not `CLAUDE_CONFIG_DIR`-sandbox the suite's happy path and call it isolation.

Assertion points, not "install works".

## Wrong vs Correct

Wrong — re-implement the host's path rule and write its file ourselves:

```python
payload.setdefault("mcpServers", {})[MCP_KEY] = entry()
_save(Path.home() / ".claude.json", payload)
# wrong file whenever CLAUDE_CONFIG_DIR is set, and it bypasses the host's
# own guard: "re-saving config is missing auth that cache has; refusing to
# write to avoid wiping ~/.claude.json. See GH #3117."
```

Correct — delegate the write, and decide presence from the config file:

```python
stored = _stored_entry()
if stored is None:
    run(add_command())
elif _entry_matches(stored):
    pass                                    # idempotent; no subprocess
else:
    run(remove_command())                   # drift: move/rename, changed interpreter,
    run(add_command())                      # or a pre-PYTHONPATH cwd-era entry
```

## Shipped defects behind these contracts

### Don't: write another tool's config file by hand

2026-09-23. `mcp_install` wrote `mcpServers.context_forge` into `~/.claude/settings.json`. That key is not in the host's settings schema, so **the server was never registered at all** — `claude mcp get context_forge` reported `No MCP server named "context_forge"` — while `forge doctor` printed `[OK] mcp: mcp package available`, a check that only tested whether the `mcp` **Python package** imports.

The entire host exposure was inert, and the diagnostic built to watch it could not see it. Two lessons, recorded separately because they generalize apart: a guest does not own the host's config file (this section), and a check that cannot fail is not a check (`quality-guidelines.md`).

Delegating also inherits protections we cannot reproduce: the config lock, the auth-loss guard quoted above, the `.config.json` precedence, and the release-dependent filename suffix.

### Gotcha: the host adds fields, so equality must ignore them

The stored entry carries `"type": "stdio"`, which our argv never emits. Strict dict equality would classify the **live, correct** entry as drifted and reinstall it on every run. Compare only `command` / `args` / `env`, and derive that comparison shape from `add_command()`'s argv so the two cannot drift apart. Verified both directions: the live entry matches, and a stale `PYTHONPATH`, a `cwd`-era entry and different `args` all fail to match.

### Design Decision: delegate the write, keep the check

**Context**: D1 moved our write from `settings.json` (no credentials) to the host's global config (holds them), which enlarged the blast radius of hand-rolling the dump.

**Options**: (a) keep hand-writing the JSON, having fixed only the path rule; (b) delegate to `claude mcp add` / `claude mcp remove`.

**Decision**: (b). It inherits the config lock, the GH #3117 auth guard, the path rule and the suffix handling, so none of that is ours to maintain forever. The cost is a dependency on the `claude` binary on PATH — acceptable, because the feature's entire purpose is registering with that host.

Presence and intent stay ours: they are read from `claude_config_path()` with the module's own `_load`, so no host output is ever parsed. That removes the English-substring dependency from idempotency (`"already exists"`) and from absent-detection (`"No MCP server named"`), which would otherwise turn a benign re-install into an exception.

**Extensibility**: if a future release needs to install at project scope, the approval flags above become load-bearing and `check_mcp`'s project branch stops being unreachable.

### Known limitation (open — stronger signal available)

`check_mcp` verifies the entry's **name** is registered, not that the stored entry is *correct* or *connectable*. So between a repo move and the next `forge mcp-install`, a drifted entry still reports `[OK] mcp: registered in … (user scope)` even though the host would fail to start it. `install()` now self-heals that drift, but the diagnostic does not detect it on its own.

The stronger signal is the host's own verdict — `claude mcp get context_forge` returns `Scope: User config` and `Status: ✔ Connected`, which exercises the stored entry rather than grepping the JSON for a key. Not adopted yet: it costs a subprocess in `doctor`, and the file-based check is offline and cheap.

Also unreachable today, and it should stay honest if that changes: a project-scope-only hit returns `ok=True` with a detail noting approval is required; no `.mcp.json` exists anywhere in this repo's ancestry, so that branch is exercised only by tests.
