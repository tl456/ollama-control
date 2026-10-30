from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .config import ROOT, settings

LOCK = threading.RLock()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def connect():
    db = sqlite3.connect(ROOT / "data/chat.db", timeout=20, factory=ClosingConnection)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, updated TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS chat_turns(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        conversation TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
        updated REAL NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
        UNIQUE(conversation, request_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS chat_turns_pending ON chat_turns(conversation,status,seq)")
    return db


def put(conv):
    with LOCK, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous_row = db.execute("SELECT payload FROM conversations WHERE id=?", (conv["id"],)).fetchone()
        previous = json.loads(previous_row[0]) if previous_row else None
        becoming_active = not conv.get("deleted") and (previous is None or previous.get("deleted"))
        if becoming_active:
            limit = int(settings().get("chat_limit", 30))
            count = db.execute("SELECT count(*) FROM conversations WHERE COALESCE(json_extract(payload,'$.deleted'),0)=0").fetchone()[0]
            if count >= limit:
                raise ValueError(f"活动对话已达 {limit} 个，请先手动将不需要的对话移入回收站，再新建、恢复或导入；不会自动删除旧对话")
        conv["updated"] = now()
        if conv.get("deleted"):
            conv["deleted_at"] = (previous or {}).get("deleted_at") or ((previous or {}).get("updated") if (previous or {}).get("deleted") else conv["updated"])
        else:
            conv.pop("deleted_at", None)
        payload = {k: v for k, v in conv.items() if not k.startswith("_")}
        if conv.get("_turn_snapshot"):
            # A running request has immutable options, while the user may choose
            # different defaults for a later turn. Never overwrite those defaults.
            row = db.execute("SELECT payload FROM conversations WHERE id=?", (conv["id"],)).fetchone()
            if row:
                payload["options"] = json.loads(row[0])["options"]
        db.execute("INSERT INTO conversations VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,payload=excluded.payload",
                   (conv["id"], conv["updated"], json.dumps(payload, ensure_ascii=False)))
    return conv


def get(ident):
    with connect() as db:
        row = db.execute("SELECT payload FROM conversations WHERE id=?", (ident,)).fetchone()
    if not row:
        raise KeyError("对话不存在")
    return json.loads(row[0])


def listing():
    with connect() as db:
        rows = db.execute("SELECT payload FROM conversations ORDER BY updated DESC").fetchall()
    return [{k: v for k, v in json.loads(r[0]).items() if k != "messages"} for r in rows]


def create(cfg):
    return put({"id": uid(), "title": "新对话", "created": now(), "updated": now(), "messages": [],
                "leaf": None, "deleted": False, "pinned": False, "options": {
                    "provider": cfg["provider"], "model": cfg["default_model"], "context": cfg["context"],
                    "tool_mode": cfg["tool_mode"], "tools": list(cfg["tools"]), "think": cfg["think"],
                    "inference_device": cfg.get("inference_device", "auto"),
                    "rag_enabled": False, "rag_ids": [], "rag_mode": "original"}})


def listing_page(page=1, page_size=20, search="", deleted=False):
    page, page_size = max(1, int(page)), max(5, min(50, int(page_size)))
    where = "COALESCE(json_extract(payload, '$.deleted'),0)=? AND instr(lower(json_extract(payload, '$.title')),lower(?))>0"
    args = (int(deleted), search[:200])
    with connect() as db:
        total = db.execute("SELECT count(*) FROM conversations WHERE " + where, args).fetchone()[0]
        page = min(page, max(1, (total + page_size - 1) // page_size))
        rows = db.execute("SELECT json_remove(payload, '$.messages') FROM conversations WHERE " + where +
                          " ORDER BY COALESCE(json_extract(payload, '$.pinned'),0) DESC, updated DESC LIMIT ? OFFSET ?",
                          (*args, page_size, (page - 1) * page_size)).fetchall()
    return {"items": [json.loads(r[0]) for r in rows], "total": total, "page": page, "page_size": page_size}


def path(conv, leaf=None):
    by_id = {m["id"]: m for m in conv["messages"]}
    ident = conv["leaf"] if leaf is None else leaf
    result, seen = [], set()
    while ident:
        if ident in seen or ident not in by_id:
            raise ValueError("无效会话分支")
        seen.add(ident)
        message = by_id[ident]
        result.append(message)
        ident = message.get("parent")
    return result[::-1]


def append(conv, role, content, **extra):
    message = {"id": uid(), "parent": conv["leaf"], "role": role, "content": content, "created": now(), **extra}
    conv["messages"].append(message)
    conv["leaf"] = message["id"]
    return message


def erase(ident):
    with LOCK, connect() as db:
        db.execute("DELETE FROM conversations WHERE id=?", (ident,))


def backup_db(target):
    with LOCK, connect() as source, sqlite3.connect(target, factory=ClosingConnection) as dest:
        source.backup(dest)


def window(conv, offset=0, limit=50):
    """Only active branch window on the wire; full branch graph stays in SQLite."""
    messages = path(conv)
    limit, offset = max(1, min(100, int(limit))), max(0, int(offset))
    end = max(0, len(messages) - offset)
    start = max(0, end - limit)
    result = {k: v for k, v in conv.items() if k not in ("messages", "context_state") and not k.startswith("_")}
    display = []
    for message in messages[start:end]:
        item = {**message}
        if message.get("attachments"):
            item["attachments"] = [{**{k: v for k, v in a.items() if k not in ("text", "injected_text")}, "text": "", "text_chars": len(a.get("text", "")), "text_available": True} for a in message["attachments"]]
        display.append(item)
    result.update(messages=display, message_page={"total": len(messages), "offset": offset,
                  "limit": limit, "next_offset": offset + limit if start else None},
                  context_state=conv.get("context_state", {}))
    return result
