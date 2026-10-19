import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from context_forge import doctor as diag
from context_forge import mcp_install
from context_forge import runtime_sync
from context_forge.queue import JobStore
from context_forge import status as status_mod

REPO = Path(__file__).resolve().parents[1]


def write_doc(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {path.stem}\nproject: demo\nstatus: accepted\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_sync_is_idempotent_and_does_not_touch_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    vault = tmp_path / "vault"
    source = write_doc(vault, "knowledge/accepted/a.md", "alpha content")
    before = (source.read_bytes(), source.stat().st_mtime_ns)

    first = runtime_sync.sync(vault, dry_run=False)
    assert (first.created, first.updated, first.unchanged) == (1, 0, 0)
    assert (first.vault_documents, first.db_records) == (1, 1)

    second = runtime_sync.sync(vault, dry_run=False)
    assert (second.created, second.updated) == (0, 0)
    assert second.unchanged == 1

    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_snapshot_order_counter_only_moves_on_change(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    vault = tmp_path / "vault"
    source = write_doc(vault, "knowledge/accepted/a.md", "alpha")

    runtime_sync.sync(vault, dry_run=False)
    counter = tmp_path / "sync-order"
    after_create = counter.read_text(encoding="utf-8").strip()

    runtime_sync.sync(vault, dry_run=False)
    assert counter.read_text(encoding="utf-8").strip() == after_create

    source.write_text(source.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    changed = runtime_sync.sync(vault, dry_run=False)
    assert changed.updated == 1
    assert int(counter.read_text(encoding="utf-8").strip()) > int(after_create)


def test_doctor_and_status_report_unsynced_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    vault = tmp_path / "vault"
    write_doc(vault, "knowledge/accepted/a.md", "alpha")
    write_doc(vault, "knowledge/accepted/b.md", "beta")
    runtime_sync.sync(vault, dry_run=False)
    write_doc(vault, "knowledge/accepted/c.md", "gamma")

    warnings = diag.check_memory_sync(vault)
    assert any("not imported" in line for line in warnings)

    rows = {name: (ok, detail) for name, ok, detail in diag.run_all()}
    assert rows["memory-sync"][0] is False
    assert "not imported" in rows["memory-sync"][1]

    snapshot = status_mod.collect(JobStore(tmp_path / "queue.db"))
    assert snapshot["memory_vault_documents"] == 3
    assert snapshot["memory_db_records"] == 2
    assert snapshot["memory_unsynced"] == 1
    assert "not in memory.db" in status_mod.render_text(snapshot)


def test_sync_failure_is_surfaced_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    vault = tmp_path / "vault"
    write_doc(vault, "knowledge/accepted/a.md", "alpha")

    from context_forge import memory_import, watcher

    def boom(*args, **kwargs):
        raise RuntimeError("disk fault")

    monkeypatch.setattr(memory_import, "import_vault", boom)
    watcher._sync_after_pass(vault)  # must not raise

    assert "disk fault" in (runtime_sync.read_failure() or "")
    warnings = diag.check_memory_sync(vault)
    assert any("last sync failed" in line for line in warnings)


def test_clean_sync_clears_stale_failure_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    vault = tmp_path / "vault"
    write_doc(vault, "knowledge/accepted/a.md", "alpha")
    runtime_sync.record_failure("feedback 1: missing or ambiguous legacy id rule-k1")
    assert (tmp_path / "sync-error").exists()

    report = runtime_sync.sync(vault, dry_run=False)

    assert not report.diagnostics
    assert runtime_sync.read_failure() is None
    assert not (tmp_path / "sync-error").exists()


def test_sync_diagnostics_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    monkeypatch.setattr(
        runtime_sync, "_sync",
        lambda *a, **k: runtime_sync.SyncReport(
            0, 0, 0, 0, 0, 0, 0,
            ("feedback 1: missing or ambiguous legacy id rule-k1",)),
    )

    report = runtime_sync.sync(tmp_path / "vault", dry_run=False)

    assert report.diagnostics
    assert "rule-k1" in (runtime_sync.read_failure() or "")


def test_dry_run_leaves_failure_marker_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    vault = tmp_path / "vault"
    write_doc(vault, "knowledge/accepted/a.md", "alpha")

    runtime_sync.record_failure("kept")
    runtime_sync.sync(vault, dry_run=True)
    assert runtime_sync.read_failure() == "kept"

    runtime_sync.clear_failure()
    runtime_sync.sync(vault, dry_run=True)
    assert runtime_sync.read_failure() is None


def _stub_cli(monkeypatch, results):
    """Record every `_run(*args)` and replay `results` (a list of CompletedProcess)."""
    calls: list[tuple] = []

    def fake_run(*args):
        calls.append(args)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(mcp_install, "_run", fake_run)
    return calls


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# What `add_command()` intends, minus the host-added `type: stdio`.
INTENDED = {"command": "python", "args": ["-m", "context_forge.mcp_server"],
            "env": {"PYTHONPATH": str(REPO)}}


def _point_config(monkeypatch, cfg: Path) -> Path:
    """Redirect both config resolvers into tmp_path — never the real files."""
    monkeypatch.setattr(mcp_install, "claude_config_path", lambda: cfg)
    monkeypatch.setattr(mcp_install, "_legacy_settings_path",
                        lambda: cfg.parent / "settings.json")
    return cfg


def _write_entry(path: Path, entry: object) -> None:
    path.write_text(json.dumps({"mcpServers": {"context_forge": entry}}),
                    encoding="utf-8")


def test_mcp_install_uses_claude_cli_add(tmp_path, monkeypatch):
    from context_forge import mcp_install

    _point_config(monkeypatch, tmp_path / "claude.json")   # absent
    calls = _stub_cli(monkeypatch, [_completed(0, "Added stdio MCP server")])

    result = mcp_install.install()

    assert calls == [("mcp", "add", "--scope", "user", "context_forge",
                      "-e", f"PYTHONPATH={REPO}",
                      "--", "python", "-m", "context_forge.mcp_server")]
    assert result["installed"] is True and result["already_installed"] is False
    assert result["refreshed"] is False


def test_mcp_install_equal_entry_needs_no_subprocess(tmp_path, monkeypatch):
    """An entry that already matches (host-added `type` ignored) is a no-op."""
    from context_forge import mcp_install

    cfg = _point_config(monkeypatch, tmp_path / "claude.json")
    _write_entry(cfg, {**INTENDED, "type": "stdio"})
    calls = _stub_cli(monkeypatch, [_completed(0)])

    result = mcp_install.install()

    assert calls == []                       # no CLI call at all
    assert result["already_installed"] is True
    assert result["installed"] is False


def test_mcp_install_refreshes_drifted_env(tmp_path, monkeypatch):
    """A moved repo / changed interpreter must be refreshed, not called OK."""
    from context_forge import mcp_install

    cfg = _point_config(monkeypatch, tmp_path / "claude.json")
    _write_entry(cfg, {**INTENDED, "env": {"PYTHONPATH": r"C:\stale\old-repo"}})
    calls = _stub_cli(monkeypatch, [_completed(0, "Removed"), _completed(0, "Added")])

    result = mcp_install.install()

    assert calls == [("mcp", "remove", "context_forge", "-s", "user"),
                     ("mcp", "add", "--scope", "user", "context_forge",
                      "-e", f"PYTHONPATH={REPO}",
                      "--", "python", "-m", "context_forge.mcp_server")]
    assert result["installed"] is True
    assert result["refreshed"] is True
    assert result["already_installed"] is False


def test_mcp_install_refreshes_stale_entry_without_env(tmp_path, monkeypatch):
    """A `cwd`-era entry (no `env`) is drift, not a match."""
    from context_forge import mcp_install

    cfg = _point_config(monkeypatch, tmp_path / "claude.json")
    _write_entry(cfg, {"command": "python",
                       "args": ["-m", "context_forge.mcp_server"],
                       "cwd": r"C:\stale\old-repo"})
    calls = _stub_cli(monkeypatch, [_completed(0), _completed(0)])

    result = mcp_install.install()

    assert [c[1] for c in calls] == ["remove", "add"]     # remove then add
    assert result["refreshed"] is True


def test_mcp_install_treats_malformed_entry_as_absent(tmp_path, monkeypatch):
    from context_forge import mcp_install

    cfg = _point_config(monkeypatch, tmp_path / "claude.json")
    cfg.write_text(json.dumps({"mcpServers": {"context_forge": "not-a-dict"}}),
                   encoding="utf-8")
    calls = _stub_cli(monkeypatch, [_completed(0, "Added")])

    result = mcp_install.install()

    assert calls == [("mcp", "add", "--scope", "user", "context_forge",
                      "-e", f"PYTHONPATH={REPO}",
                      "--", "python", "-m", "context_forge.mcp_server")]
    assert result["installed"] is True


def test_mcp_install_raises_when_cli_missing(tmp_path, monkeypatch):
    from context_forge import mcp_install

    _point_config(monkeypatch, tmp_path / "claude.json")   # absent
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(mcp_install.McpInstallError, match="claude` CLI on PATH"):
        mcp_install.install()


def test_mcp_install_raises_on_real_failure(tmp_path, monkeypatch):
    from context_forge import mcp_install

    _point_config(monkeypatch, tmp_path / "claude.json")   # absent
    _stub_cli(monkeypatch, [_completed(2, "", "boom")])
    with pytest.raises(mcp_install.McpInstallError, match="exit 2"):
        mcp_install.install()


def test_mcp_uninstall_uses_claude_cli_remove(tmp_path, monkeypatch):
    from context_forge import mcp_install

    cfg = _point_config(monkeypatch, tmp_path / "claude.json")
    _write_entry(cfg, {**INTENDED, "type": "stdio"})
    calls = _stub_cli(monkeypatch, [_completed(0, "Removed MCP server context_forge")])
    assert mcp_install.uninstall()["removed"] == 1
    assert calls == [("mcp", "remove", "context_forge", "-s", "user")]


def test_mcp_uninstall_absent_is_not_an_error(tmp_path, monkeypatch):
    """Absent is read from the config file — no CLI call, no message matching."""
    from context_forge import mcp_install

    _point_config(monkeypatch, tmp_path / "claude.json")   # absent
    calls = _stub_cli(monkeypatch, [_completed(1, "whatever the host says")])
    assert mcp_install.uninstall() == {"removed": 0, "output": ""}
    assert calls == []


def test_mcp_config_path_honours_claude_config_dir(tmp_path, monkeypatch):
    from context_forge import mcp_install

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert mcp_install.claude_config_path() == tmp_path / ".claude.json"

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    # child of the config dir, not a sibling of home
    assert mcp_install.claude_config_path() == cfg / ".claude.json"

    (cfg / ".config.json").write_text("{}", encoding="utf-8")
    assert mcp_install.claude_config_path() == cfg / ".config.json"


def test_mcp_legacy_settings_path_follows_config_dir(tmp_path, monkeypatch):
    """A relocated config dir must still find the legacy entry it wrote."""
    from context_forge import mcp_install

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert mcp_install._legacy_settings_path() == tmp_path / ".claude" / "settings.json"

    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert mcp_install._legacy_settings_path() == cfg / "settings.json"


def test_mcp_install_migrates_legacy_inert_entry(tmp_path, monkeypatch):
    from context_forge import mcp_install

    legacy = tmp_path / ".claude" / "settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps(
        {"mcpServers": {"context_forge": {"command": "old"},
                        "playwright": {"command": "npx"}}}), encoding="utf-8")
    monkeypatch.setattr(mcp_install, "claude_config_path",
                        lambda: tmp_path / "claude.json")      # absent
    monkeypatch.setattr(mcp_install, "_legacy_settings_path", lambda: legacy)
    _stub_cli(monkeypatch, [_completed(0)])

    result = mcp_install.install()

    assert result["migrated"] == str(legacy)
    after = json.loads(legacy.read_text(encoding="utf-8"))
    assert after["mcpServers"] == {"playwright": {"command": "npx"}}


def test_mcp_uninstall_leaves_legacy_alone(tmp_path, monkeypatch):
    from context_forge import mcp_install

    legacy = tmp_path / "settings.json"
    stale = json.dumps(
        {"mcpServers": {"context_forge": {"command": "old"}}},
        indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    legacy.write_bytes(stale)
    cfg = _point_config(monkeypatch, tmp_path / "claude.json")
    _write_entry(cfg, {**INTENDED, "type": "stdio"})
    _stub_cli(monkeypatch, [_completed(0)])

    mcp_install.uninstall()
    assert legacy.read_bytes() == stale          # uninstall never touches legacy


def test_worker_uses_receipt_finalization_without_legacy_runtime_sync():
    worker_source = (REPO / "context_forge" / "worker.py").read_text(encoding="utf-8")
    assert "process_pending_finalizations" in worker_source
    assert "runtime_sync" not in worker_source

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == [
        "pydantic>=2.0,<3.0", "typer>=0.12,<1.0", "python-frontmatter>=1.1,<2.0",
        "httpx>=0.27,<1.0", "tomli-w>=1.0,<2.0",
    ]
    assert project["optional-dependencies"]["mcp"] == ["mcp>=1.0,<2.0"]
    assert project["optional-dependencies"]["liveness"] == ["psutil>=5.9,<8"]
    assert project["optional-dependencies"]["watch"] == ["watchdog>=4,<7"]
