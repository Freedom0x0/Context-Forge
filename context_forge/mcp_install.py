"""Register the Context Forge MCP server with the host (Claude Code).

We do **not** hand-write the host's config file. Claude Code owns
`~/.claude.json` — its location (relocated by `CLAUDE_CONFIG_DIR`), its lock,
and its guard against wiping auth (GH #3117) — so resolving that path and
dumping JSON ourselves was a standing source of drift. Instead we shell out to
the host's own CLI:

    claude mcp add --scope user context_forge -e PYTHONPATH=<repo> -- python -m context_forge.mcp_server
    claude mcp remove context_forge -s user

`PYTHONPATH` (not `cwd`) makes the package importable from any directory;
`memory.db` resolves via `config.runtime_dir()`, not cwd, so nothing depends on
where the server starts.

The one file we still touch by hand is the *inert* legacy
`~/.claude/settings.json`: an older install wrote `mcpServers.context_forge`
there and the host never reads `mcpServers` from it, so the host's own `remove`
cannot find it. `_migrate_legacy` drops just that key, with a backup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .host_config import load_config, save_config

MCP_KEY = "context_forge"
SERVER_ARGS = ("-m", "context_forge.mcp_server")
PYTHON = "python"


class McpInstallError(RuntimeError):
    """The host CLI is missing, or rejected the command."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _claude() -> str:
    found = shutil.which("claude")
    if not found:
        raise McpInstallError(
            "cannot find the `claude` CLI on PATH; install Claude Code, or run "
            "`claude mcp add --scope user context_forge "
            f"-e PYTHONPATH={_repo_root()} -- {PYTHON} {' '.join(SERVER_ARGS)}` manually"
        )
    return found


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([_claude(), *args], capture_output=True, text=True)


def add_command() -> list[str]:
    """The argv Claude Code's CLI prints for its own registration (minus `claude`)."""
    return ["mcp", "add", "--scope", "user", MCP_KEY,
            "-e", f"PYTHONPATH={_repo_root()}",
            "--", PYTHON, *SERVER_ARGS]


def remove_command() -> list[str]:
    return ["mcp", "remove", MCP_KEY, "-s", "user"]


def _output(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).strip()


def _intended_entry() -> dict:
    """The `mcpServers.context_forge` value our argv would produce.

    Derived from `add_command()` so the drift comparison can never disagree
    with what we install. `type` ("stdio") is added by the host and is
    deliberately absent — comparing it would make every live entry look
    drifted.
    """
    argv = add_command()
    split = argv.index("--")
    command, *args = argv[split + 1:]
    env: dict[str, str] = {}
    for i, token in enumerate(argv[:split]):
        if token == "-e":
            key, _, value = argv[i + 1].partition("=")
            env[key] = value
    return {"command": command, "args": args, "env": env}


def _stored_entry() -> dict | None:
    """The user-scope `context_forge` entry, or None if absent/malformed.

    Presence comes from the config file the host reads, not from CLI output:
    the host's "already exists" message is prose we must not depend on.
    """
    payload = _load(claude_config_path())
    if not isinstance(payload, dict):
        return None
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(MCP_KEY)
    return entry if isinstance(entry, dict) else None


def _entry_matches(stored: dict) -> bool:
    """Does the stored entry already say what we intend to install?

    Only the fields we set are compared (`command`/`args`/`env`), so
    host-added fields and key ordering cannot force a spurious refresh.
    """
    intended = _intended_entry()
    args = stored.get("args")
    env = stored.get("env")
    return (stored.get("command") == intended["command"]
            and isinstance(args, list) and args == intended["args"]
            and isinstance(env, dict) and env == intended["env"])


def install() -> dict:
    """Register the server at user scope, refreshing a drifted entry.

    Idempotence is decided from the config file, never from the CLI's
    duplicate-`add` error text (which is locale-dependent prose). Absent →
    add; present-and-equal → no subprocess at all; present-but-different
    (moved repo, changed interpreter, stale entry) → remove then add.
    """
    stored = _stored_entry()
    if stored is not None and _entry_matches(stored):
        # Never report "already registered" for an entry we cannot verify.
        migrated = _migrate_legacy()
        return {"installed": False, "already_installed": True,
                "refreshed": False, "migrated": migrated, "output": ""}

    refreshed = stored is not None
    if refreshed:
        remove = _run(*remove_command())
        if remove.returncode != 0:
            raise McpInstallError(
                f"`claude mcp remove` failed (exit {remove.returncode}): "
                f"{_output(remove)}")

    result = _run(*add_command())
    output = _output(result)
    if result.returncode != 0:
        raise McpInstallError(
            f"`claude mcp add` failed (exit {result.returncode}): {output}")
    migrated = _migrate_legacy()
    return {"installed": True, "already_installed": False,
            "refreshed": refreshed, "migrated": migrated, "output": output}


def uninstall() -> dict:
    """Remove the user-scope server. Absent is not an error.

    Absence is read from the config file, so no CLI message matching is
    needed; a non-zero exit from a `remove` we did expect to succeed is a
    real error.
    """
    if _stored_entry() is None:
        return {"removed": 0, "output": ""}
    result = _run(*remove_command())
    output = _output(result)
    if result.returncode != 0:
        raise McpInstallError(
            f"`claude mcp remove` failed (exit {result.returncode}): {output}")
    return {"removed": 1, "output": output}


# --- Claude Code's own config-path resolution -----------------------------
#
# Verbatim from the installed binary, so we stop re-deriving it:
#
#   Zn = () => (process.env.CLAUDE_CONFIG_DIR ?? path.join(os.homedir(), ".claude"))
#   I0 = () => fs.existsSync(path.join(Zn(), ".config.json"))
#             ? path.join(Zn(), ".config.json")
#             : path.join(process.env.CLAUDE_CONFIG_DIR || os.homedir(), `.claude${vRt()}.json`)
#
# The join uses the **raw** env var, so with `CLAUDE_CONFIG_DIR=D:\\cfg` the
# file is `D:\\cfg\\.claude.json` — a *child* of the config dir — whereas
# `~/.claude.json` is a *sibling* of `~/.claude`. `vRt()` is "" on prod builds.


def _config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else Path.home() / ".claude"


def claude_config_path() -> Path:
    """The file Claude Code reads `mcpServers` from (user scope)."""
    dot_config = _config_dir() / ".config.json"
    if dot_config.exists():
        return dot_config
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home())
    return Path(base) / ".claude.json"


def project_config_paths(start: Path) -> list[Path]:
    """`<dir>/.mcp.json` from `start` up through its ancestors, nearest first.

    Mirrors the host's project-scope lookup, which walks up from the session
    cwd. We anchor on `start` (the repo) because that is the project this
    server belongs to.
    """
    here = Path(start).resolve()
    return [parent / ".mcp.json"
            for parent in (here, *here.parents)
            if (parent / ".mcp.json").exists()]


# --- legacy inert file ----------------------------------------------------

def _legacy_settings_path() -> Path:
    """The file older installs wrote to; the host does not read it.

    The old writer used `CLAUDE_CONFIG_DIR ?? ~/.claude` joined with
    `settings.json` — the same `Zn()` base the host uses for hooks. Deriving it
    the same way means a legacy entry is still found when the config dir was
    relocated, instead of only ever checking `~/.claude/settings.json`.
    """
    return _config_dir() / "settings.json"


def _load(path: Path) -> dict:
    return load_config(path)


def _save(path: Path, payload: dict) -> None:
    save_config(path, payload)




def _migrate_legacy() -> str | None:
    """Drop a stale `context_forge` entry from the inert legacy file.

    Claude Code does not read `mcpServers` from `~/.claude/settings.json`, so
    the host's own `remove` can never find an entry there — we do. Only our own
    key is touched (and `mcpServers` popped if it empties); backed up first.
    """
    path = _legacy_settings_path()
    if not path.exists():
        return None
    payload = _load(path)
    servers = payload.get("mcpServers", {})
    if not isinstance(servers, dict) or MCP_KEY not in servers:
        return None
    servers.pop(MCP_KEY, None)
    if not servers:
        payload.pop("mcpServers", None)
    _save(path, payload)
    return str(path)
