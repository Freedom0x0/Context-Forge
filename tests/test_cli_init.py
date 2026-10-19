from pathlib import Path

from context_forge import cli
from context_forge.config import load_settings


def test_init_writes_settings_and_memory_db(tmp_path: Path, monkeypatch) -> None:
    """`forge init` is the documented first step; it must work from declared deps."""
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()

    cli.init(vault)

    settings = load_settings()
    assert settings is not None
    assert settings.vault_path == vault.resolve()
    assert (tmp_path / "home" / "config.toml").is_file()
    assert (tmp_path / "home" / "memory.db").is_file()
    assert (vault / "context-forge").is_dir()
