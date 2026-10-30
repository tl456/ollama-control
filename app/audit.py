"""Content-free local tool audit. Fixed retention; intentionally outside reset data."""
from __future__ import annotations
import hashlib
import gzip
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import zlib
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from .config import ROOT

RETENTION_DAYS = 365
MAX_BYTES = 1_000_000_000
ARCHIVE_BATCH = 256
HOT_KEEP = 128
MAX_SEGMENT_ROWS = 512
MAX_SEGMENT_RAW_BYTES = 2 * 1024 * 1024
FIELDS = ("id", "timestamp", "created_at", "model", "provider", "tool", "action", "status", "duration", "target", "snapshot_id", "error_type")
LOCK = threading.RLock()

class AuditCorruptionError(ValueError):
    """Visible corruption failure: never silently replace a damaged block with []."""

def _open_db():
    folder = ROOT / "audit"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "tools.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA auto_vacuum=FULL")
    # Leave space for the bounded rollback journal and SQLite indexes.
    db.execute("PRAGMA max_page_count=" + str(max(16, int(MAX_BYTES * .45) // 4096)))
    db.execute("""CREATE TABLE IF NOT EXISTS events(
      id TEXT PRIMARY KEY, timestamp REAL NOT NULL, created_at TEXT NOT NULL,
      model TEXT, provider TEXT, tool TEXT, action TEXT, status TEXT,
      duration REAL, target TEXT, snapshot_id TEXT, error_type TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(timestamp)")
    db.execute("""CREATE TABLE IF NOT EXISTS archives(
      id INTEGER PRIMARY KEY, min_time REAL NOT NULL, max_time REAL NOT NULL,
      row_count INTEGER NOT NULL, raw_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
      payload BLOB NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS audit_index(
      id TEXT PRIMARY KEY, timestamp REAL NOT NULL, model TEXT, tool TEXT,
      status TEXT, archive_id INTEGER)""")
    db.execute("CREATE INDEX IF NOT EXISTS audit_index_time ON audit_index(timestamp DESC,id DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS audit_index_archive ON audit_index(archive_id)")
    # The narrow index supports filters/count/offset before decompressing any data.
    # Keep the existing 12-column events table usable by integration fixtures.
    db.execute("""INSERT OR IGNORE INTO audit_index
      SELECT id,timestamp,model,tool,status,NULL FROM events""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS audit_event_insert AFTER INSERT ON events
      BEGIN INSERT INTO audit_index VALUES(NEW.id,NEW.timestamp,NEW.model,NEW.tool,NEW.status,NULL); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS audit_event_update AFTER UPDATE ON events
      BEGIN UPDATE audit_index SET timestamp=NEW.timestamp,model=NEW.model,tool=NEW.tool,status=NEW.status
      WHERE id=NEW.id AND archive_id IS NULL; END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS audit_event_delete AFTER DELETE ON events
      BEGIN DELETE FROM audit_index WHERE id=OLD.id AND archive_id IS NULL; END""")
    return db

@contextmanager
def _db():
    with closing(_open_db()) as db, db:
        yield db

def _label(value, limit=128):
    # Scalar metadata only; strip controls and never interpolate arguments/errors.
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or ""))[:limit]

def target(args):
    if not isinstance(args, dict):
        return ""
    value = args.get("url")
    if value:
        p = urlsplit(str(value))
        return (p.scheme + "://" + (p.hostname or ""))[:200]
    value = args.get("path") or args.get("directory")
    if value:
        p = Path(str(value))
        suffix = p.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", p.suffix) else ""
        return "file#" + hashlib.sha256(str(p).casefold().encode()).hexdigest()[:16] + suffix
    return ""

def _disk_bytes(db):
    return db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]

def _encode(rows):
    if not rows or len(rows)>MAX_SEGMENT_ROWS:
        raise ValueError("审计分段记录数量无效")
    raw=json.dumps([[r[k] for k in FIELDS] for r in rows],ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")
    if len(raw)>MAX_SEGMENT_RAW_BYTES:
        raise ValueError("审计分段超出大小限制")
    return (min(r["timestamp"] for r in rows),max(r["timestamp"] for r in rows),len(rows),len(raw),
            hashlib.sha256(raw).hexdigest(),gzip.compress(raw,compresslevel=6,mtime=0))

def _decode(segment):
    try:
        raw_limit=int(segment["raw_bytes"])
        if not 0<raw_limit<=MAX_SEGMENT_RAW_BYTES or not 0<int(segment["row_count"])<=MAX_SEGMENT_ROWS:
            raise ValueError("Invalid segment size")
        if len(segment["payload"])>MAX_SEGMENT_RAW_BYTES+65536:
            raise ValueError("Invalid compressed segment size")
        with gzip.GzipFile(fileobj=io.BytesIO(segment["payload"])) as stream:
            raw=stream.read(MAX_SEGMENT_RAW_BYTES+1)
        if len(raw)!=raw_limit or hashlib.sha256(raw).hexdigest()!=segment["sha256"]:
            raise ValueError("Checksum mismatch")
        items=json.loads(raw.decode("utf-8"))
        if not isinstance(items,list) or len(items)!=segment["row_count"]:
            raise ValueError("Record count mismatch")
        rows=[]
        for item in items:
            if not isinstance(item,list) or len(item)!=len(FIELDS):
                raise ValueError("Invalid record shape")
            row=dict(zip(FIELDS,item))
            if not isinstance(row["id"],str) or not isinstance(row["timestamp"],(int,float)):
                raise ValueError("Invalid record metadata")
            rows.append(row)
        if len({r["id"] for r in rows})!=len(rows):
            raise ValueError("Duplicate records in segment")
        return rows
    except (OSError,EOFError,ValueError,TypeError,KeyError,OverflowError,zlib.error) as error:
        raise AuditCorruptionError("审计压缩分段损坏，未清空或跳过记录；请保留本地审计文件检查") from error

def _read_segment(db,ident):
    segment=db.execute("SELECT * FROM archives WHERE id=?",(ident,)).fetchone()
    if segment is None:
        raise AuditCorruptionError("审计索引对应的压缩分段缺失，未清空记录")
    rows=_decode(segment)
    indexed={r["id"]:(r["timestamp"],r["model"],r["tool"],r["status"]) for r in
             db.execute("SELECT id,timestamp,model,tool,status FROM audit_index WHERE archive_id=?",(ident,))}
    actual={r["id"]:(r["timestamp"],r["model"],r["tool"],r["status"]) for r in rows}
    if indexed!=actual:
        raise AuditCorruptionError("审计分段与索引不一致，未清空记录")
    return rows

def _replace_segment(db,ident,rows):
    if rows:
        db.execute("UPDATE archives SET min_time=?,max_time=?,row_count=?,raw_bytes=?,sha256=?,payload=? WHERE id=?",
                   (*_encode(rows),ident))
    else:
        db.execute("DELETE FROM archives WHERE id=?",(ident,))

def _archive(db,force=False):
    # Running calls stay mutable in the hot table. Only completed calls are packed.
    finished=db.execute("SELECT COUNT(*) FROM events WHERE status!='running'").fetchone()[0]
    eligible=max(0,finished-HOT_KEEP)
    batch=min(MAX_SEGMENT_ROWS,max(2,int(ARCHIVE_BATCH)))
    if eligible<batch and (not force or eligible<2):
        return 0
    amount=min(batch,eligible)
    rows=[dict(r) for r in db.execute("SELECT * FROM events WHERE status!='running' ORDER BY timestamp,id LIMIT ?",(amount,))]
    if not rows:return 0
    ident=db.execute("INSERT INTO archives(min_time,max_time,row_count,raw_bytes,sha256,payload) VALUES(?,?,?,?,?,?)",_encode(rows)).lastrowid
    db.executemany("UPDATE audit_index SET archive_id=? WHERE id=?",[(ident,r["id"]) for r in rows])
    # Trigger only removes NULL-archive index rows, so call-id uniqueness survives.
    db.executemany("DELETE FROM events WHERE id=?",[(r["id"],) for r in rows])
    db.commit()
    return len(rows)

def _delete_indexed(db,records):
    hot=[]; grouped={}
    for record in records:
        if record["archive_id"] is None:hot.append((record["id"],))
        else:grouped.setdefault(record["archive_id"],set()).add(record["id"])
    for ident,removed in grouped.items():
        # Validate even full blocks before quota eviction: corruption must surface.
        rows=_read_segment(db,ident)
        keep=[r for r in rows if r["id"] not in removed]
        db.executemany("DELETE FROM audit_index WHERE id=?",[(x,) for x in removed])
        _replace_segment(db,ident,keep)
    db.executemany("DELETE FROM events WHERE id=?",hot)
    db.commit()

def _cleanup(db):
    cutoff=time.time()-RETENTION_DAYS*86400
    # Fully expired segments need no decompression. Expired source data is no
    # longer part of retention; partial boundary segments are validated/repacked.
    while True:
        full=db.execute("SELECT id FROM archives WHERE max_time<? LIMIT 32",(cutoff,)).fetchall()
        if not full:break
        for row in full:
            db.execute("DELETE FROM audit_index WHERE archive_id=?",(row["id"],))
            db.execute("DELETE FROM archives WHERE id=?",(row["id"],))
        db.commit()
    while True:
        expired=db.execute("SELECT id,archive_id FROM audit_index WHERE timestamp<? ORDER BY timestamp,id LIMIT 256",(cutoff,)).fetchall()
        if not expired:break
        _delete_indexed(db,expired)
    # SQLite pages + worst-case rollback journal remain below the fixed cap.
    while _disk_bytes(db)>MAX_BYTES*.40:
        old=db.execute("SELECT id,archive_id FROM audit_index ORDER BY timestamp,id LIMIT 256").fetchall()
        if not old:break
        _delete_indexed(db,old)


def action_metadata(name, args):
    if name == "snapshot_restore":
        return name, "restore"
    fixed = {"memory_read", "memory_update", "knowledge_search", "knowledge_read"}
    if name in fixed:
        return name, "execute"
    from . import tools
    item = next((t for t in tools.list_tools() if t["id"] == name), None)
    if item is None:
        return "unknown_tool", "invalid_parameters"
    if not isinstance(args, dict):
        return name, "invalid_parameters"
    properties = item.get("parameters", {}).get("properties", {})
    for key in ("action", "method"):
        if key in args:
            allowed = properties.get(key, {}).get("enum", [])
            return name, args[key] if isinstance(args[key], str) and args[key] in allowed else "invalid_parameters"
    return name, "execute"


def start(name, args, scope=None, call_id=None):
    scope = scope or {}
    ident = str(call_id or scope.get("_call_id") or uuid.uuid4().hex)
    if len(ident) > 160:
        raise ValueError("调用编号过长")
    now = time.time()
    tool_name, action = action_metadata(name, args)
    with LOCK, _db() as db:
        _cleanup(db)
        try:
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                ident, now, datetime.fromtimestamp(now, timezone.utc).isoformat(),
                _label(scope.get("model")), _label(scope.get("provider", "local")), _label(tool_name),
                action, "running", 0,
                target(args), "", ""))
        except sqlite3.IntegrityError as error:
            rejected = ValueError("该调用编号已有执行记录，未重复执行；请查看工具审计，必要时发起新的调用")
            rejected.executed = False
            raise rejected from error
    return ident

def finish(call_id, name, args, scope=None, result=None, status="success", duration=0, error_type=""):
    snapshot = result.get("snapshot_id", "") if isinstance(result, dict) else ""
    if status == "success" and isinstance(result,dict):
        if result.get("ok") is False: status="error"
        if name=="http_request" and int(result.get("status",200))>=400: status="http_error"
    with LOCK, _db() as db:
        changes = {"status": _label(status,32), "duration": round(max(0,float(duration)),3),
                   "snapshot_id": _label(snapshot), "error_type": _label(error_type,64)}
        updated = db.execute("UPDATE events SET status=?,duration=?,snapshot_id=?,error_type=? WHERE id=?",
                             (*changes.values(), call_id)).rowcount
        if not updated:
            archived = db.execute("SELECT archive_id FROM audit_index WHERE id=?", (call_id,)).fetchone()
            if archived and archived["archive_id"] is not None:
                # Long-running calls may finish behind many newer entries and
                # be compressed before a final unknown-outcome classification.
                rows = _read_segment(db, archived["archive_id"])
                next(row for row in rows if row["id"] == call_id).update(changes)
                db.execute("UPDATE audit_index SET status=? WHERE id=?", (changes["status"], call_id))
                _replace_segment(db, archived["archive_id"], rows)
        _cleanup(db)
        _archive(db)
        _cleanup(db)

def record(name, args, scope=None, result=None, status="success", duration=0, error_type="", call_id=None):
    ident = start(name,args,scope,call_id)
    finish(ident,name,args,scope,result,status,duration,error_type)
    return ident

def listing(page=1, page_size=30, tool="", model="", status="", since="", until=""):
    page, page_size = max(1,int(page)), min(100,max(1,int(page_size)))
    where, values = [], []
    for column,value in (("tool",tool),("model",model),("status",status)):
        if value:
            where.append(column+"=?"); values.append(value)
    for op,value in ((">=",since),("<=",until)):
        if value:
            try: stamp=datetime.fromisoformat(value).timestamp()
            except ValueError as error: raise ValueError("日期格式无效") from error
            where.append("timestamp"+op+"?"); values.append(stamp)
    clause = " WHERE "+" AND ".join(where) if where else ""
    with LOCK, _db() as db:
        _cleanup(db)
        total=db.execute("SELECT COUNT(*) FROM audit_index"+clause,values).fetchone()[0]
        selected=db.execute("SELECT id,archive_id FROM audit_index"+clause+" ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?",[*values,page_size,(page-1)*page_size]).fetchall()
        blocks={}
        rows=[]
        for item in selected:
            if item["archive_id"] is None:
                row=db.execute("SELECT * FROM events WHERE id=?",(item["id"],)).fetchone()
                if row is None: raise AuditCorruptionError("审计热记录缺失，未清空记录")
                rows.append(dict(row))
            else:
                if item["archive_id"] not in blocks:
                    blocks[item["archive_id"]]={r["id"]:r for r in _read_segment(db,item["archive_id"])}
                row=blocks[item["archive_id"]].get(item["id"])
                if row is None: raise AuditCorruptionError("审计分段缺少索引记录，未清空记录")
                rows.append(row)
        compressed=db.execute("SELECT COUNT(*),COALESCE(SUM(row_count),0),COALESCE(SUM(raw_bytes),0),COALESCE(SUM(length(payload)),0) FROM archives").fetchone()
        hot=db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {"items":[{k:r[k] for k in r.keys() if k!="timestamp"} for r in rows],
            "total":total,"page":page,"page_size":page_size,"retention_days":RETENTION_DAYS,"max_bytes":MAX_BYTES,
            "bytes":sum(p.stat().st_size for p in (ROOT/"audit").glob("*") if p.is_file()),
            "compression":{"format":"gzip-batches","segments":compressed[0],"archived_records":compressed[1],
                           "source_bytes":compressed[2],"compressed_bytes":compressed[3],"hot_records":hot}}

def cleanup():
    with LOCK, _db() as db:
        _cleanup(db)
        while _archive(db,force=True):
            _cleanup(db)
