import json
import subprocess
import tomllib
from pathlib import Path

from context_forge import doctor as diag
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

    from context_forge import watcher

    def boom(*args, **kwargs):
        raise RuntimeError("disk fault")

    monkeypatch.setattr(runtime_sync, "sync", boom)
    watcher._sync_after_pass(vault)  # must not raise

    assert "disk fault" in (runtime_sync.read_failure() or "")
    warnings = diag.check_memory_sync(vault)
    assert any("last sync failed" in line for line in warnings)


def test_mcp_install_only_adds_and_uninstall_restores(tmp_path):
    from context_forge import mcp_install

    settings = tmp_path / "settings.json"
    original = {
        "mcpServers": {
            "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
        },
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "keep"}]}]},
    }
    settings.write_text(json.dumps(original, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    mcp_install.install(settings)
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["mcpServers"]["playwright"] == original["mcpServers"]["playwright"]
    assert set(after["mcpServers"]) == {"playwright", "context_forge"}
    assert after["hooks"] == original["hooks"]
    assert after["mcpServers"]["context_forge"]["cwd"] == str(REPO)
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 1

    mcp_install.install(settings)  # idempotent: still exactly one key
    again = json.loads(settings.read_text(encoding="utf-8"))
    assert set(again["mcpServers"]) == {"playwright", "context_forge"}

    mcp_install.uninstall(settings)
    assert json.loads(settings.read_text(encoding="utf-8")) == original


def test_worker_vault_write_logic_and_dependencies_untouched():
    diff = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "context_forge/worker.py"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    assert diff.stdout.strip() == ""

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == [
        "pydantic>=2.0,<3.0", "typer>=0.12,<1.0", "python-frontmatter>=1.1,<2.0",
    ]
    assert project["optional-dependencies"]["mcp"] == ["mcp>=1.0,<2.0"]
