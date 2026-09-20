"""Keep Context Forge hooks alive across cc-switch provider switches.

cc-switch rewrites `~/.claude/settings.json` from a per-provider
template stored in `~/.cc-switch/cc-switch.db`
(`providers.settings_config`). Anything it doesn't know about —
including our `hooks` block — is dropped on every switch.

Fix: mirror the hooks block into every Claude provider's
`settings_config` so cc-switch writes them back every time.

All functions are no-ops when cc-switch isn't installed.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def default_db_path() -> Path:
    return Path.home() / ".cc-switch" / "cc-switch.db"


def is_available(db_path: Path | None = None) -> bool:
    return (db_path or default_db_path()).exists()


def _backup(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = db.with_name(f"cc-switch.db.context-forge-{stamp}.bak")
    shutil.copy2(db, target)
    return target


def sync_hooks(hooks: dict, db_path: Path | None = None) -> dict[str, int]:
    """Merge `hooks` into every Claude provider's settings_config.

    Returns {'providers': N, 'updated': M}. `updated` counts the
    providers whose config actually changed.
    """
    db = db_path or default_db_path()
    if not db.exists():
        return {"providers": 0, "updated": 0}

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute(
            "SELECT id, settings_config FROM providers WHERE app_type='claude'"
        ))
        if not rows:
            return {"providers": 0, "updated": 0}
        _backup(db)
        updated = 0
        for row in rows:
            try:
                cfg = json.loads(row["settings_config"] or "{}")
            except json.JSONDecodeError:
                cfg = {}
            if cfg.get("hooks") == hooks:
                continue
            cfg["hooks"] = hooks
            con.execute(
                "UPDATE providers SET settings_config=? WHERE id=?",
                (json.dumps(cfg, ensure_ascii=False), row["id"]),
            )
            updated += 1
        con.commit()
        return {"providers": len(rows), "updated": updated}
    finally:
        con.close()


def remove_hooks(db_path: Path | None = None) -> dict[str, int]:
    """Strip our `hooks` key from every Claude provider's config."""
    db = db_path or default_db_path()
    if not db.exists():
        return {"providers": 0, "updated": 0}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute(
            "SELECT id, settings_config FROM providers WHERE app_type='claude'"
        ))
        if not rows:
            return {"providers": 0, "updated": 0}
        _backup(db)
        updated = 0
        for row in rows:
            try:
                cfg = json.loads(row["settings_config"] or "{}")
            except json.JSONDecodeError:
                continue
            if "hooks" not in cfg:
                continue
            cfg.pop("hooks")
            con.execute(
                "UPDATE providers SET settings_config=? WHERE id=?",
                (json.dumps(cfg, ensure_ascii=False), row["id"]),
            )
            updated += 1
        con.commit()
        return {"providers": len(rows), "updated": updated}
    finally:
        con.close()