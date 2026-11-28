"""Explicit, reversible config migration. No host-native settings writes."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from .convo_contract import merge
from .setup_store import lock, _check_development_mode


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".convo-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_config(path):
    if not path.exists():
        return {}
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Plugin configuration must be an object")
    return result


def migrate(user_plugin_root, apply=False):
    with lock():
        return _migrate(user_plugin_root, apply)


def _migrate(user_plugin_root, apply=False):
    root = Path(user_plugin_root)
    source = root / "_enhanced_speech" / "config.json"
    target = root / "_convo" / "config.json"
    journal = root / "_convo" / "data" / "migration.json"
    before = read_config(target)
    combined = merge(read_config(source), before)
    changed = combined != before
    if not apply:
        return {"changed": changed, "source_exists": source.exists(), "target_exists": target.exists()}
    if journal.exists():
        record = read_config(journal)
        if record.get("complete"):
            return {"changed": False, "already_migrated": True}
        current_hash = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
        original = json.loads(record["before"]) if record["before"] is not None else {}
        if before != original and current_hash != record["after_hash"]:
            raise ValueError("Settings changed during interrupted migration")
        _check_development_mode(before, record["after"])
        atomic_json(target, record["after"])
        atomic_json(journal, {**record, "complete": True})
        return {"changed": True, "recovered": True}
    # Retain exact prior bytes; unknown settings and secret values never reach an API response.
    old = target.read_text(encoding="utf-8") if target.exists() else None
    record = {"before": old, "after": combined, "after_hash": hashlib.sha256(json.dumps(combined, sort_keys=True).encode()).hexdigest(), "complete": False}
    _check_development_mode(before, combined)
    atomic_json(journal, record)
    atomic_json(target, combined)
    atomic_json(journal, {**record, "complete": True})
    return {"changed": changed, "already_migrated": False}


def rollback(user_plugin_root):
    with lock():
        return _rollback(user_plugin_root)


def _rollback(user_plugin_root):
    root = Path(user_plugin_root) / "_convo"
    target, journal = root / "config.json", root / "data" / "migration.json"
    record = read_config(journal)
    if not record:
        return {"changed": False}
    if hashlib.sha256(json.dumps(read_config(target), sort_keys=True).encode()).hexdigest() != record["after_hash"]:
        raise ValueError("Convo settings changed after migration; refusing to overwrite them")
    if record["before"] is None:
        _check_development_mode(read_config(target), {})
        target.unlink(missing_ok=True)
    else:
        _check_development_mode(read_config(target), json.loads(record["before"]))
        atomic_json(target, json.loads(record["before"]))
    journal.rename(root / "data" / ("migration-rolled-back-" + uuid.uuid4().hex + ".json"))
    return {"changed": True}
