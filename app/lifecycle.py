"""Exact managed-data reset and conservative retention; never clean external roots."""
from __future__ import annotations
import copy
import os
import stat
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from .config import ROOT, DEFAULTS, VERSION, read_json, write_json, settings

INITIALIZATION_LOCK = threading.RLock()


def managed_log_path(path):
    path, boundary = Path(path).absolute(), (ROOT / "logs").absolute()
    if path.parent != boundary:
        raise ValueError("日志仅写入自管 logs 目录")
    for item in (boundary, path):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("日志不跟随链接或联接点")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("日志不跟随重解析路径")
    return path


class ManagedLogHandler(RotatingFileHandler):
    """Bounded size and daily rolling; old closed files also obey the day TTL."""
    def __init__(self, path):
        path = managed_log_path(path)
        self.day = datetime.fromtimestamp(Path(path).stat().st_mtime).date() if Path(path).exists() else datetime.now().date()
        super().__init__(path, maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8")

    def shouldRollover(self, record):
        today = datetime.now().date()
        if today != self.day:
            self.day = today
            return True
        return super().shouldRollover(record)


def capture_process_log(process, path):
    """Drain this owned child's pipe in bounded chunks, without retaining output."""
    handler = ManagedLogHandler(path)
    def drain():
        try:
            while True:
                block = process.stdout.readline(8192)
                if not block:
                    break
                handler.emit(logging.LogRecord("managed-service", logging.INFO, "", 0,
                    block.decode("utf-8", errors="replace").rstrip("\r\n"), (), None))
        finally:
            process.stdout.close()
            handler.close()
    thread = threading.Thread(target=drain, name="managed-log-reader", daemon=True)
    thread.start()
    return thread

def _checked_remove_path(path, parent):
    """Validate the lexical boundary before resolving or deleting any node."""
    path, parent, root = Path(path).absolute(), Path(parent).absolute(), ROOT.absolute()
    if (".." in path.parts or ".." in parent.parts or path == parent
            or not parent.is_relative_to(root) or not path.is_relative_to(parent)):
        raise ValueError("清理目标越过管理范围")
    cursor = path
    while True:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise ValueError("清理不跟随链接/联接点")
        try:
            attrs = getattr(cursor.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attrs = 0
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("清理不跟随重解析点")
        if cursor == root:
            return path
        cursor = cursor.parent


def safe_remove(path, parent):
    path = _checked_remove_path(path, parent)
    if not path.exists():
        return
    if path.is_dir():
        for child in path.iterdir():
            safe_remove(child, parent)
        path.rmdir()
    else:
        path.unlink()

def reset_managed(reason="manual"):
    if reason not in ("manual", "version-test"):
        raise ValueError("初始化原因无效")
    from . import store
    with store.LOCK:
        # All paths are application-owned; never use configured filesystem roots.
        folders = [(ROOT / "data" / name, ROOT / "data") for name in
                   ("attachments", "exports", "python_runs", "backups", "trash", "memory", "knowledge", "skills", "snapshots")]
        folders += [(ROOT / "cache/office-previews", ROOT / "cache")]
        folders += [(ROOT / "plugins/imported", ROOT / "plugins"), (ROOT / "config/tools", ROOT / "config")]
        files = [(ROOT / "data" / name, ROOT / "data") for name in ("access.db", "access.db-wal", "access.db-shm")]
        files += [(ROOT / "config" / name, ROOT / "config") for name in
                  ("settings.json", "tools.json", "secrets.json", "first-start.json", "auth.json")]
        files += [(ROOT / "cache" / name, ROOT / "cache") for name in
                  ("file-reads.json", "ui-show.json", "download-history.json")]
        # Check parents and fixed SQLite/marker paths before opening the DB or
        # writing defaults. Recursive removal checks every descendant as well.
        for target, boundary in folders + files + [(ROOT / "data" / name, ROOT / "data") for name in
                ("chat.db", "chat.db-wal", "chat.db-shm")] + [(ROOT / "config/version.json", ROOT / "config")]:
            _checked_remove_path(target, boundary)
        with store.connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM conversations")
            db.commit()
            db.execute("VACUUM")
        for target, boundary in folders:
            safe_remove(target, boundary)
            target.mkdir(parents=True, exist_ok=True)
        for target, boundary in files:
            safe_remove(target, boundary)
        write_json(ROOT / "config/settings.json", copy.deepcopy(DEFAULTS))
        write_json(ROOT / "config/tools.json", {})
        write_json(ROOT / "config/secrets.json", {})
        from .upgrades import SCHEMA_VERSION
        write_json(ROOT / "config/version.json", {"version": VERSION, "schema_version": SCHEMA_VERSION,
                   "reset_at": time.time(), "reset_reason": reason, "backup": False})
    return {"ok": True, "reset": True, "backup": None, "models_preserved": True, "runtime_preserved": True,
            "note": "应用数据已直接初始化一次，未创建备份；9B/4B/0.6B 与运行库保留。"}

def initialize_version(policy=None):
    """Apply an explicit release policy once, independently of code replacement."""
    from . import upgrades
    mode = upgrades.resolve_policy(policy)
    with INITIALIZATION_LOCK:
        previous = read_json(ROOT / "config/version.json", {})
        if not isinstance(previous, dict):
            raise ValueError("版本标记不是有效对象，未自动清理资料")
        if previous.get("version") == VERSION:
            return {"ok": True, "reset": False, "upgrade_mode": mode}
        if mode == "preserve":
            return upgrades.preserve(previous)
        result = reset_managed(reason="version-test")
        marker = read_json(ROOT / "config/version.json", {})
        marker.update(upgrade_mode=mode, previous_version=previous.get("version"))
        write_json(ROOT / "config/version.json", marker)
        return {**result, "upgrade_mode": mode, "previous_version": previous.get("version")}

def cleanup():
    from . import store
    from . import audit
    audit.cleanup()
    cfg = settings()
    if not cfg["cleanup_enabled"]:
        return {"removed": 0, "enabled": False}
    removed, errors = 0, []
    now = time.time()
    def remove(path, parent):
        nonlocal removed
        try:
            safe_remove(path, parent)
            removed += 1
        except (ValueError, OSError) as error:
            errors.append(type(error).__name__)
    # References include all branches and trashed conversations until actually purged.
    with store.LOCK, store.connect() as db:
        # Stream one conversation at a time; never materialize the whole chat DB.
        rows = db.execute("SELECT id,payload FROM conversations")
        import json
        from . import knowledge
        from . import turn_queue
        referenced, referenced_runs = knowledge.referenced_attachments() | turn_queue.referenced_attachments(), set()
        for ident, payload in rows:
            conv = json.loads(payload)
            if conv.get("deleted"):
                from datetime import datetime
                # Legacy deleted records are anchored once; edits in the trash
                # must not restart their thirty-day retention window.
                deleted_at = conv.get("deleted_at") or conv["updated"]
                if not conv.get("deleted_at"):
                    conv["deleted_at"] = deleted_at
                    db.execute("UPDATE conversations SET payload=? WHERE id=?", (json.dumps(conv, ensure_ascii=False), ident))
                age = now - datetime.fromisoformat(deleted_at).timestamp()
                if age > cfg["trash_retention_days"] * 86400:
                    db.execute("DELETE FROM conversations WHERE id=?", (ident,))
                    removed += 1
                    continue
            for message in conv.get("messages", []):
                referenced.update(a["id"] for a in message.get("attachments", []) if "id" in a)
                referenced.update(e.get("result", {}).get("attachment_id") for e in message.get("interactions", []) if e.get("name") == "read_file")
                referenced_runs.update(e.get("result", {}).get("id") for e in message.get("interactions", []) if e.get("name") == "python_run")
        folder = ROOT / "data/attachments"
        for child in folder.iterdir():
            if child.name not in referenced and len(child.name) == 32 and not child.is_symlink():
                if now - child.stat().st_mtime > 7 * 86400 and (child / "metadata.json").is_file():
                    remove(child, folder)
    from . import python_lab
    active = python_lab.active_ids()
    folder = ROOT / "data/python_runs"
    folder.mkdir(exist_ok=True)
    runs = [p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and (p / "run.json").is_file()]
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for index, child in enumerate(runs):
        if child.name not in active and child.name not in referenced_runs and (index >= cfg["run_retention_count"] or now - child.stat().st_mtime > cfg["run_retention_days"] * 86400):
            remove(child, folder)
    from . import knowledge
    with knowledge.connect() as db:
        db.execute("DELETE FROM builds WHERE status NOT IN ('queued','processing','embedding') AND id NOT IN (SELECT id FROM builds ORDER BY created DESC LIMIT 100)")
    logs = cleanup_logs(cfg.get("log_retention_days", 7))
    removed += logs["removed"]
    errors.extend(logs["errors"])
    return {"removed": removed, "errors": errors[:10], "enabled": True}


def cleanup_logs(days=7):
    """Only bounded software-owned log names; never walk user/tool directories."""
    folder = ROOT / "logs"
    removed, errors = 0, []
    if not folder.is_dir() or folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
        return {"removed": 0, "errors": []}
    bases = ("app.log", "server.log", "ollama.log", "ollama-embedding.log")
    cutoff = time.time() - max(1, int(days)) * 86400
    for path in folder.iterdir():
        if not any(path.name == name or path.name.startswith(name + ".") for name in bases):
            continue
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            continue
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                safe_remove(path, folder)
                removed += 1
            except (OSError, ValueError) as error:
                errors.append(type(error).__name__)
    return {"removed": removed, "errors": errors}


def rotate_closed_log(path, size=10 * 1024 * 1024):
    """Only call before starting a new owned process, never rotate its live handle."""
    path = managed_log_path(path)
    if path.is_file() and path.stat().st_size > size:
        oldest = path.with_name(path.name + ".3")
        if oldest.exists():
            safe_remove(oldest, ROOT / "logs")
        for index in (2, 1):
            previous = path.with_name(path.name + "." + str(index))
            if previous.exists():
                previous.replace(path.with_name(path.name + "." + str(index + 1)))
        path.replace(path.with_name(path.name + ".1"))
