"""Small persistent terminal-download history. Dismissal never removes model data."""
from __future__ import annotations
import threading
import time
from .config import ROOT, read_json, write_json

LOCK = threading.RLock()
PATH = ROOT / "cache/download-history.json"
FIELDS = ("id", "model", "status", "done", "total", "completed", "speed", "eta", "error", "created", "ended", "dismissed")


def _history():
    cutoff = time.time() - 7 * 86400
    values = read_json(PATH, [])
    return [v for v in values if isinstance(v, dict) and v.get("done") and v.get("ended", 0) > cutoff][-200:]


def remember(job):
    if not job.get("done"):
        return
    with LOCK:
        rows = [v for v in _history() if v["id"] != job["id"]]
        rows.append({k: job[k] for k in FIELDS if k in job})
        write_json(PATH, rows[-200:])


def listing(current):
    with LOCK:
        values = {v["id"]: v for v in _history()}
        values.update({k: {field: v[field] for field in FIELDS if field in v} for k, v in current.items()})
        return sorted((v for v in values.values() if not v.get("dismissed")), key=lambda v: v.get("created", 0), reverse=True)[:200]


def dismiss(ident, current):
    with LOCK:
        entry = current.get(ident) or next((v for v in _history() if v["id"] == ident), None)
        if not entry:
            raise KeyError("下载任务不存在")
        if not entry.get("done"):
            raise ValueError("下载仍在进行，请先取消后再关闭记录")
        saved = {**entry, "dismissed": True}
        saved.setdefault("ended", time.time())
        remember(saved)
        if ident in current:
            current[ident].update(saved)
        return {"ok": True, "id": ident, "dismissed": True, "model_data_deleted": False}


def dismiss_finished(current):
    ids = [v["id"] for v in listing(current) if v.get("done")]
    for ident in ids:
        dismiss(ident, current)
    return {"ok": True, "dismissed": len(ids), "model_data_deleted": False}
