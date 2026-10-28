from pathlib import Path

def _isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    return tmp_path / "runtime.log"


def test_report_reaches_the_file_a_daemon_can_read(tmp_path: Path, monkeypatch,
                                                   capsys) -> None:
    """`pythonw` has no stdout: a loop error used to leave no trace at all."""
    from context_forge import runtime_log

    log = _isolate(tmp_path, monkeypatch)
    runtime_log.report("[watcher] scan error: RuntimeError: disk fault")

    assert "scan error" in capsys.readouterr().out          # a terminal still sees it
    written = log.read_text(encoding="utf-8")
    assert "scan error: RuntimeError: disk fault" in written
    assert written[:4].isdigit()                            # timestamped


def test_the_log_rotates_at_its_size_limit(tmp_path: Path, monkeypatch) -> None:
    from context_forge import runtime_log

    log = _isolate(tmp_path, monkeypatch)
    runtime_log.MAX_LOG_BYTES = 200
    for index in range(20):
        runtime_log.append(f"line {index} " + "x" * 40)

    assert log.exists()
    assert (tmp_path / "runtime.log.1").exists()
    assert log.stat().st_size < runtime_log.MAX_LOG_BYTES


def test_a_log_write_failure_never_raises(tmp_path: Path, monkeypatch) -> None:
    """A diagnostic must not become the reason a pass fails."""
    from context_forge import runtime_log, config

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "runtime_dir", lambda: blocker / "sub")

    runtime_log.append("still silent")            # must not raise


def test_the_watcher_scan_error_lands_in_the_log(tmp_path: Path, monkeypatch) -> None:
    """The end-to-end path the review was worried about: a failing scan pass."""
    from context_forge import watcher, config

    log = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")
    monkeypatch.setattr(watcher, "scan_once",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk fault")))

    # run() loops forever; make the first sleep end the process (run catches it)
    monkeypatch.setattr(watcher.time, "sleep",
                        lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))
    watcher.run(_store(tmp_path), _vault(tmp_path), interval_seconds=0.01)

    assert "scan error: RuntimeError: disk fault" in log.read_text(encoding="utf-8")


def _store(tmp_path: Path):
    from context_forge.queue import JobStore

    return JobStore(tmp_path / "queue.db")


def _vault(tmp_path: Path):
    from context_forge.vault import Vault

    return Vault(tmp_path / "vault")


def test_a_repeated_notice_is_logged_once_per_window(tmp_path: Path, monkeypatch) -> None:
    """A duplicate launcher retries every 30 seconds; the file must not fill with it."""
    from context_forge import runtime_log

    log = _isolate(tmp_path, monkeypatch)

    for _ in range(3):
        runtime_log.report("[watcher] 已有实例在运行（pid=1）", dedupe_seconds=3600)

    assert log.read_text(encoding="utf-8").count("已有实例在运行") == 1


def test_a_different_message_is_never_deduped(tmp_path: Path, monkeypatch) -> None:
    from context_forge import runtime_log

    log = _isolate(tmp_path, monkeypatch)

    runtime_log.report("first", dedupe_seconds=3600)
    runtime_log.report("second", dedupe_seconds=3600)

    assert log.read_text(encoding="utf-8").count("\n") == 2
