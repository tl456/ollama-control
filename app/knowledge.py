"""Local, versioned knowledge libraries. SQLite metadata/vectors, bounded batches."""
from __future__ import annotations
import asyncio
import hashlib
import heapq
import json
import math
import re
import sqlite3
import threading
import time
import bisect
import contextlib
from array import array
from .config import ROOT, settings
from .store import ClosingConnection, uid
from . import kb_documents, rag_chunks

LOCK = threading.RLock()
RUNNING = {}
DIRTY = set()

class LibraryChanged(ValueError):
    pass


def model_digest(model):
    if model.startswith("qwen3-embedding:"):
        tag = model.split(":", 1)[1]
        if re.fullmatch(r"[A-Za-z0-9_.-]+", tag) and tag not in (".", ".."):
            path = ROOT / "models/manifests/registry.ollama.ai/library/qwen3-embedding" / tag
            if path.is_file():
                return hashlib.sha256(path.read_bytes()).hexdigest()
    return model


def connect():
    folder = ROOT / "data/knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "knowledge.db", timeout=20, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS libraries(id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL DEFAULT '',
      enabled INTEGER NOT NULL DEFAULT 1,config TEXT NOT NULL,active_config TEXT NOT NULL DEFAULT '{}',
      generation TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'empty',
      error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,kb TEXT REFERENCES libraries(id) ON DELETE CASCADE,
      attachment TEXT NOT NULL,name TEXT NOT NULL,sha TEXT NOT NULL,chars INTEGER NOT NULL,created REAL NOT NULL,
      UNIQUE(kb,sha));
    CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,kb TEXT REFERENCES libraries(id) ON DELETE CASCADE,
      doc TEXT REFERENCES documents(id) ON DELETE CASCADE,generation TEXT NOT NULL,ordinal INTEGER NOT NULL,
      text TEXT NOT NULL,location TEXT NOT NULL,vector BLOB NOT NULL,dim INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS chunks_version ON chunks(kb,generation);
    CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY,kb TEXT, status TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0,
      total INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
    """)
    # Existing libraries remain readable; new columns describe managed MD only.
    with LOCK:
        columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if "source_kind" not in columns:
            db.execute("ALTER TABLE documents ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'attachment'")
        if "source_revision" not in columns:
            db.execute("ALTER TABLE documents ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 0")
        if "runtime_info" not in {row[1] for row in db.execute("PRAGMA table_info(builds)")}:
            db.execute("ALTER TABLE builds ADD COLUMN runtime_info TEXT NOT NULL DEFAULT '{}'")
        db.commit()
    return db


def defaults():
    cfg = settings()
    return {"model": cfg["embedding_model"], "chunk_tokens": cfg["rag_chunk_tokens"],
            "overlap_tokens": cfg["rag_overlap_tokens"], "top_k": cfg["rag_top_k"],
            "markdown_headers": cfg.get("rag_markdown_headers", True)}


def validate_config(value):
    from .providers import parse_model
    result = {**defaults(), **value}
    result["model"] = parse_model(str(result["model"]))
    for key, low, high in (("chunk_tokens", 128, 2048), ("overlap_tokens", 0, 512), ("top_k", 1, 10)):
        result[key] = max(low, min(high, int(result[key])))
    if result["overlap_tokens"] >= result["chunk_tokens"]:
        raise ValueError("分段重叠必须小于分段长度")
    if not isinstance(result["markdown_headers"], bool):
        raise ValueError("Markdown标题分节需要布尔值")
    return {k: result[k] for k in defaults()}


def public(row):
    value = dict(row)
    value["config"] = json.loads(value["config"])
    value["active_config"] = json.loads(value["active_config"])
    value["enabled"] = bool(value["enabled"])
    return value


def get(ident):
    with connect() as db:
        row = db.execute("SELECT *, (SELECT count(*) FROM documents WHERE kb=libraries.id) AS document_count,"
                         "(SELECT count(*) FROM chunks WHERE kb=libraries.id AND generation=libraries.generation) AS chunk_count "
                         "FROM libraries WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise ValueError("知识库不存在")
    return public(row)


def listing(page=1, search="", category=""):
    page = max(1, int(page))
    clause = "instr(lower(name),lower(?))>0 AND (?='' OR category=?)"
    args = (search[:100], category[:80], category[:80])
    with connect() as db:
        total = db.execute("SELECT count(*) FROM libraries WHERE " + clause, args).fetchone()[0]
        page = min(page, max(1, (total + 19) // 20))
        rows = db.execute("SELECT id FROM libraries WHERE " + clause + " ORDER BY updated DESC LIMIT 20 OFFSET ?", (*args, (page-1)*20)).fetchall()
        categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM libraries WHERE category<>'' ORDER BY category LIMIT 100")]
    return {"items": [get(r[0]) for r in rows], "total": total, "page": page, "page_size": 20, "categories": categories}


def create(name, category="", config=None):
    name = str(name).strip()[:100]
    if not name:
        raise ValueError("请填写知识库名称")
    ident = uid()
    now = time.time()
    with LOCK, connect() as db:
        db.execute("INSERT INTO libraries(id,name,category,config,created,updated) VALUES(?,?,?,?,?,?)",
                   (ident, name, str(category).strip()[:80], json.dumps(validate_config(config or {})), now, now))
    return get(ident)


def update(ident, patch):
    with LOCK, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM libraries WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("知识库不存在")
        value = public(row)
        name = str(patch.get("name", value["name"])).strip()[:100]
        if not name:
            raise ValueError("知识库名称不能为空")
        cfg = validate_config(patch.get("config", value["config"]))
        changed = any(cfg[k] != value["config"].get(k, True if k == "markdown_headers" else None)
                      for k in ("model", "chunk_tokens", "overlap_tokens", "markdown_headers"))
        enabled = patch.get("enabled", value["enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("启用需要布尔值")
        db.execute("UPDATE libraries SET name=?,category=?,enabled=?,config=?,revision=revision+?,status=?,updated=? WHERE id=?",
                   (name, str(patch.get("category", value["category"]))[:80], int(enabled), json.dumps(cfg), int(changed),
                    "needs_rebuild" if changed else value["status"], time.time(), ident))
    if changed:
        cancel(ident)
    return get(ident)


def documents(ident, page=1):
    get(ident)
    page = max(1, int(page))
    with connect() as db:
        total = db.execute("SELECT count(*) FROM documents WHERE kb=?", (ident,)).fetchone()[0]
        rows = db.execute("SELECT * FROM documents WHERE kb=? ORDER BY created DESC LIMIT 20 OFFSET ?", (ident, (page-1)*20)).fetchall()
    return {"items": [_public_document(r) for r in rows], "total": total, "page": page, "page_size": 20}


def _public_document(row):
    value = dict(row)
    value.update(kind=value["source_kind"], revision=value["source_revision"], editable=True)
    return value


def _document(ident, doc):
    get(ident)
    with connect() as db:
        row = db.execute("SELECT * FROM documents WHERE kb=? AND id=?", (ident, doc)).fetchone()
    if row is None:
        raise ValueError("知识文档不存在或已删除")
    return _public_document(row)


def _document_text(row):
    if row["source_kind"] == "markdown":
        return kb_documents.read(ROOT, row["kb"], row["id"])
    from . import attachments
    return attachments.load(row["attachment"])["text"]


def document_source(ident, doc):
    with LOCK:
        row = _document(ident, doc)
        text = _document_text(row)
        return {**row, "text": text, "status": get(ident)["status"],
                "chars": len(text), "sha": hashlib.sha256(text.encode()).hexdigest()}


def create_markdown(ident, name, text):
    name, blob = kb_documents.name(name), kb_documents.validate_text(text)
    with LOCK:
        get(ident)
        doc = uid()
        try:
            with connect() as db:
                if db.execute("SELECT count(*) FROM documents WHERE kb=?", (ident,)).fetchone()[0] >= 500:
                    raise ValueError("单库最多500份文档，请分类建立其他库")
                kb_documents.write(ROOT, ident, doc, text)
                db.execute("INSERT INTO documents(id,kb,attachment,name,sha,chars,created,source_kind,source_revision) VALUES(?,?,?,?,?,?,?,'markdown',1)",
                           (doc, ident, "", name, hashlib.sha256(blob).hexdigest(), len(text), time.time()))
                db.execute("UPDATE libraries SET revision=revision+1,status='needs_rebuild',updated=? WHERE id=?", (time.time(), ident))
        except BaseException:
            with contextlib.suppress(OSError, ValueError):
                kb_documents.remove(ROOT, ident, doc)
            raise
        DIRTY.add(ident)
        return {"id": doc, "document": document_source(ident, doc), "duplicate": False}


def update_document_source(ident, doc, patch):
    if not isinstance(patch, dict) or set(patch) - {"name", "text", "revision"}:
        raise ValueError("文档编辑只支持name/text；不能指定文件路径")
    with LOCK:
        old = document_source(ident, doc)
        text = patch.get("text", old["text"])
        blob = kb_documents.validate_text(text)
        name = kb_documents.name(patch.get("name", old["name"]))
        try:
            kb_documents.write(ROOT, ident, doc, text)
            with connect() as db:
                changed = db.execute("UPDATE documents SET name=?,sha=?,chars=?,source_kind='markdown',source_revision=source_revision+1 WHERE kb=? AND id=?",
                           (name, hashlib.sha256(blob).hexdigest(), len(text), ident, doc)).rowcount
                if changed != 1:
                    raise ValueError("知识文档已删除，未恢复旧文档")
                db.execute("UPDATE libraries SET revision=revision+1,status='needs_rebuild',updated=? WHERE id=?", (time.time(), ident))
        except BaseException:
            if old["source_kind"] == "markdown":
                kb_documents.write(ROOT, ident, doc, old["text"])
            else:
                kb_documents.remove(ROOT, ident, doc)
            raise
        DIRTY.add(ident)
        return {"id": doc, "document": document_source(ident, doc)}


def add_document(ident, attachment_id):
    from . import attachments
    get(ident)
    meta = attachments.load(attachment_id)
    if meta.get("status") != "ready" or not meta.get("text", "").strip():
        raise ValueError("请等待附件文字解析完成后加入知识库")
    if len(meta["text"]) > 2_000_000:
        raise ValueError("单份知识资料超过 200 万字符，请拆分后加入，未静默截断")
    with LOCK, connect() as db:
        source_fingerprint = hashlib.sha256((meta["sha256"] + meta["text"]).encode()).hexdigest()
        existing = db.execute("SELECT id FROM documents WHERE kb=? AND sha=?", (ident, source_fingerprint)).fetchone()
        if existing:
            return {"id": existing[0], "duplicate": True}
        if db.execute("SELECT count(*) FROM documents WHERE kb=?", (ident,)).fetchone()[0] >= 500:
            raise ValueError("单库最多 500 份文档，请分类建立其他库")
        doc = uid()
        db.execute("INSERT INTO documents(id,kb,attachment,name,sha,chars,created) VALUES(?,?,?,?,?,?,?)", (doc, ident, attachment_id, meta["name"], source_fingerprint, len(meta["text"]), time.time()))
        db.execute("UPDATE libraries SET revision=revision+1,status='needs_rebuild',updated=? WHERE id=?", (time.time(), ident))
    DIRTY.add(ident)
    return {"id": doc, "duplicate": False}


def delete_document(ident, doc):
    with LOCK:
        row = _document(ident, doc)
        with connect() as db:
            db.execute("DELETE FROM documents WHERE kb=? AND id=?", (ident, doc))
            db.execute("UPDATE libraries SET revision=revision+1,status='needs_rebuild',updated=? WHERE id=?", (time.time(), ident))
        warning = ""
        if row["source_kind"] == "markdown":
            try:
                kb_documents.remove(ROOT, ident, doc)
            except (OSError, ValueError):
                warning = "文档已不可检索；自管源文件清理失败，请检查权限"
        DIRTY.add(ident)
        return {"ok": True, "warning": warning}


def delete(ident):
    cancel(ident)
    with LOCK:
        with connect() as db:
            managed = [r[0] for r in db.execute("SELECT id FROM documents WHERE kb=? AND source_kind='markdown'", (ident,))]
            db.execute("DELETE FROM libraries WHERE id=?", (ident,))
            db.execute("DELETE FROM builds WHERE kb=?", (ident,))
        for doc in managed:
            with contextlib.suppress(OSError, ValueError):
                kb_documents.remove(ROOT, ident, doc)
    DIRTY.discard(ident)
    return {"ok": True}


def referenced_attachments():
    with connect() as db:
        return {r[0] for r in db.execute("SELECT attachment FROM documents WHERE attachment<>''")}


def split_text(text, size=512, overlap=64):
    # Public compatibility helper; section metadata lives in split_chunks.
    yield from rag_chunks.token_windows(text, size, overlap)


def normalize(vector):
    if not vector or len(vector) > 4096 or any(not math.isfinite(float(v)) for v in vector):
        raise ValueError("嵌入向量维度或数值异常")
    norm = math.sqrt(sum(float(v)**2 for v in vector))
    if norm <= 0:
        raise ValueError("嵌入返回零向量")
    return [float(v) / norm for v in vector]


def source_sections(text):
    return rag_chunks.source_sections(text)


def source_context(text, left, sections=None):
    sections = source_sections(text) if sections is None else sections
    index = bisect.bisect_right(sections, (left, chr(0x10ffff))) - 1
    if index < 0:
        return ""
    return sections[index][1]


def build_status(ident, status, completed=None, total=None, error="", runtime_info=None):
    with connect() as db:
        db.execute("UPDATE builds SET status=?,completed=COALESCE(?,completed),total=COALESCE(?,total),error=?,runtime_info=COALESCE(?,runtime_info),updated=? WHERE id=?",
                   (status, completed, total, error[:400], json.dumps(runtime_info, ensure_ascii=False) if runtime_info is not None else None, time.time(), ident))


def cancel(ident):
    record = RUNNING.get(ident)
    if record:
        record["rerun"] = False
        record["stop"].set()
        record["task"].cancel()
    DIRTY.discard(ident)
    return {"ok": True}


async def build(ident, job_id, stop):
    from . import attachments, providers
    snapshot = get(ident)
    cfg = snapshot["config"]
    cfg = {**cfg, "model_digest": model_digest(cfg["model"]), "parser_version": 3}
    with connect() as db:
        docs = [dict(r) for r in db.execute("SELECT * FROM documents WHERE kb=? ORDER BY created", (ident,))]
    build_status(job_id, "processing")
    generation = job_id
    completed = 0
    try:
        for doc in docs:
            if stop.is_set():
                raise asyncio.CancelledError()
            def source_snapshot():
                with LOCK:
                    current = get(ident)
                    live = _document(ident, doc["id"])
                    if current["revision"] != snapshot["revision"] or live["source_revision"] != doc["source_revision"]:
                        raise LibraryChanged("文档已变化，旧任务未覆盖新版本")
                    return _document_text(live)
            text = await asyncio.to_thread(source_snapshot)
            # Material is split without truncation. Keep source offsets for inspection.
            pieces = await asyncio.to_thread(lambda: list(rag_chunks.split_chunks(
                text, cfg["chunk_tokens"], cfg["overlap_tokens"], cfg.get("markdown_headers", True))))
            if completed + len(pieces) > 20000:
                raise ValueError("单库超过 2 万片段，请拆分知识库；没有将未索引部分当作完整")
            build_status(job_id, "embedding", total=completed + len(pieces))
            for offset in range(0, len(pieces), 2):
                if stop.is_set():
                    raise asyncio.CancelledError()
                batch = pieces[offset:offset+2]
                contexts = [p["source_context"] for p in batch]
                vectors = await providers.embed(cfg["model"], [(prefix + "\n" if prefix else "") + p["text"] for p, prefix in zip(batch, contexts)])
                runtime_info = providers.embedding_status()
                cfg["embedding_runtime"] = runtime_info
                if stop.is_set():
                    raise asyncio.CancelledError()
                with LOCK, connect() as db:
                    current = db.execute("SELECT revision FROM libraries WHERE id=?", (ident,)).fetchone()
                    if current is None or current[0] != snapshot["revision"]:
                        raise LibraryChanged("知识库配置或文档发生变化，已停止旧任务，请按最新资料重建")
                    for number, (piece, vector) in enumerate(zip(batch, vectors), offset):
                        normalized = normalize(vector)
                        location = json.dumps({"start": piece["start"], "end": piece["end"], "ordinal": number + 1,
                                               "source_context": contexts[number-offset], "heading_path": piece["heading_path"],
                                               "source_revision": doc["source_revision"], "source_sha": doc["sha"]}, ensure_ascii=False)
                        db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", (uid(), ident, doc["id"], generation, number,
                                    piece["text"], location, array("f", normalized).tobytes(), len(normalized)))
                completed += len(batch)
                build_status(job_id, "embedding", completed=completed, runtime_info=runtime_info)
                await asyncio.sleep(.02)
        with LOCK, connect() as db:
            current = db.execute("SELECT revision FROM libraries WHERE id=?", (ident,)).fetchone()
            if stop.is_set() or current is None or current[0] != snapshot["revision"]:
                raise LibraryChanged("资料变化或任务取消，未替换现有索引")
            db.execute("UPDATE libraries SET generation=?,active_config=?,status=?,error='',updated=? WHERE id=?",
                       (generation, json.dumps(cfg), "ready" if completed else "empty", time.time(), ident))
            db.execute("DELETE FROM chunks WHERE kb=? AND generation<>?", (ident, generation))
            DIRTY.discard(ident)
        build_status(job_id, "done", completed=completed, total=completed)
    except BaseException as error:
        cancelled = isinstance(error, asyncio.CancelledError) or stop.is_set()
        build_status(job_id, "cancelled" if cancelled else "failed", error="已取消" if cancelled else str(error))
        with LOCK, connect() as db:
            db.execute("DELETE FROM chunks WHERE kb=? AND generation=?", (ident, generation))
            # An obsolete/cancelled build may clean only its own generation. It
            # must never downgrade a newer successful revision back to pending.
            db.execute("UPDATE libraries SET status='needs_rebuild',error=?,updated=? WHERE id=? AND revision=?",
                       ("已取消" if cancelled else str(error)[:400], time.time(), ident, snapshot["revision"]))
        if isinstance(error, asyncio.CancelledError):
            raise
        return isinstance(error, LibraryChanged) and not stop.is_set()
    return False


def schedule(ident, background):
    get(ident)
    if ident in RUNNING and not RUNNING[ident]["task"].done():
        DIRTY.add(ident)
        RUNNING[ident]["rerun"] = True
        return {"id": RUNNING[ident]["id"], "status": "queued_changes"}
    job_id = uid()
    with connect() as db:
        db.execute("INSERT INTO builds(id,kb,status,created,updated) VALUES(?,?,'queued',?,?)", (job_id, ident, time.time(), time.time()))
    stop = asyncio.Event()
    async def worker():
        changed = rerun = False
        try:
            await asyncio.sleep(.5)
            if stop.is_set():
                raise asyncio.CancelledError()
            changed = await build(ident, job_id, stop)
        except asyncio.CancelledError:
            build_status(job_id, "cancelled", error="已取消")
        except ValueError as error:
            build_status(job_id, "failed", error=str(error))
        finally:
            record = RUNNING.pop(ident, None)
            rerun = bool(record and record.get("rerun"))
        if rerun or (changed and ident in DIRTY):
            try:
                latest = get(ident)
            except ValueError:
                return  # A deleted library can never be recreated by a worker.
            if latest["status"] == "needs_rebuild":
                schedule(ident, background)
    task = background(worker())
    RUNNING[ident] = {"id": job_id, "task": task, "stop": stop, "rerun": False}
    return {"id": job_id, "status": "queued"}


def tasks(page=1, kb=""):
    page = max(1, int(page))
    with connect() as db:
        total = db.execute("SELECT count(*) FROM builds WHERE (?='' OR kb=?)", (kb, kb)).fetchone()[0]
        rows = db.execute("SELECT * FROM builds WHERE (?='' OR kb=?) ORDER BY created DESC LIMIT 20 OFFSET ?", (kb, kb, (page-1)*20)).fetchall()
    return {"items": [{**dict(r), "runtime_info": json.loads(r["runtime_info"])} for r in rows], "page": page, "page_size": 20, "total": total}


def maintain():
    with connect() as db:
        db.execute("UPDATE builds SET status='interrupted',error='服务已重启，请手动重建' WHERE status IN ('queued','processing','embedding')")
        db.execute("DELETE FROM chunks WHERE generation NOT IN (SELECT generation FROM libraries WHERE generation<>'')")
        db.execute("DELETE FROM builds WHERE id NOT IN (SELECT id FROM builds ORDER BY created DESC LIMIT 100)")


async def search(query, selected, top_k=None, query_info=None):
    from . import providers
    started = time.perf_counter()
    embedding_seconds = scan_seconds = 0.0
    if not settings()["rag_enabled"]:
        raise ValueError("知识库功能已关闭")
    if not selected or len(selected) > 8:
        raise ValueError("请选择 1–8 个知识库")
    supplied = query_info if isinstance(query_info, dict) else {}
    try:
        preparation_seconds = max(0.0, float(supplied.get("seconds", 0)))
        if not math.isfinite(preparation_seconds):
            preparation_seconds = 0.0
    except (TypeError, ValueError):
        preparation_seconds = 0.0
    original = str(supplied.get("original", query))[:100000]
    raw_query = str(supplied.get("query", query)).strip()
    query = raw_query[:2000]
    declared = supplied.get("keywords", [])
    declared = [v.strip()[:100] for v in declared if isinstance(v, str) and v.strip()][:16] if isinstance(declared, list) else []
    terms = list(dict.fromkeys(v.casefold() for v in declared)) or re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}|[\u3400-\u9fff]{2,8}", query.casefold())[:16]
    info = {"original": original, "query": query, "keywords": declared or terms,
            "status": str(supplied.get("status", "direct"))[:80], "model": str(supplied.get("model", ""))[:160],
            "fallback": bool(supplied.get("fallback", False)),
            "provider": str(supplied.get("provider", ""))[:160], "source": str(supplied.get("source", "original_question"))[:100],
            "seconds": preparation_seconds,
            "error": re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[已隐藏]", str(supplied.get("error", "")))[:300],
            "keyword_source": "query_extractor" if declared else "regex", "query_truncated": len(raw_query) > 2000}
    if not query:
        elapsed = time.perf_counter() - started
        return {"items": [], "warnings": [], "note": "查询为空", "query_info": info,
                "elapsed_seconds": elapsed + preparation_seconds, "retrieval_seconds": elapsed,
                "timings": {"total_seconds": elapsed + preparation_seconds, "query_preparation_seconds": preparation_seconds,
                            "query_embedding_seconds": 0.0, "scan_seconds": 0.0}, "source": "local_hybrid_vector_keywords"}
    limit = max(1, min(10, int(top_k or settings()["rag_top_k"])))
    embeddings, embedding_digests, embedding_runs, candidates, warnings = {}, {}, {}, [], []
    for ident in dict.fromkeys(selected):
        kb = get(ident)
        if not kb["enabled"] or not kb["generation"]:
            warnings.append(kb["name"] + "：未启用或尚未完成索引")
            continue
        cfg = kb["active_config"]
        model = cfg["model"]
        digest = model_digest(model)
        index_digest = cfg.get("model_digest", digest)
        if index_digest != digest:
            warnings.append(kb["name"] + "：嵌入模型文件已变化，请重建知识库")
            continue
        if model not in embeddings:
            embedding_start = time.perf_counter()
            embedding_digests[model] = index_digest
            embeddings[model] = normalize((await providers.embed(model, ["Instruct: Given a user question, retrieve relevant technical passages.\nQuery: " + query]))[0])
            embedding_runs[model] = providers.embedding_status()
            if embedding_runs[model].get("fallback"):
                warnings.append("查询嵌入已使用CPU：" + str(embedding_runs[model].get("reason", "资源预检或GPU执行失败")))
            embedding_seconds += time.perf_counter() - embedding_start
        if embedding_digests[model] != index_digest:
            warnings.append(kb["name"] + "：查询向量对应的嵌入版本已变化，请重新发起检索")
            continue
        q = embeddings[model]
        def scan():
            import numpy as np
            found, notices = [], []
            with connect() as db:
                # The active generation and its chunks must share one WAL read
                # snapshot. A build may publish/delete old chunks concurrently.
                db.execute("BEGIN")
                live = db.execute("SELECT * FROM libraries WHERE id=?", (ident,)).fetchone()
                if live is None or not live["enabled"] or not live["generation"]:
                    return [], [kb["name"] + "：知识库已删除、停用或索引状态变化，请重新选择后检索"]
                active = json.loads(live["active_config"])
                current_digest = model_digest(model)
                if (active.get("model") != model or
                        active.get("model_digest", current_digest) != embedding_digests[model] or
                        current_digest != embedding_digests[model]):
                    return [], [live["name"] + "：嵌入模型或索引版本已变化，请重新发起检索；本次未复用不匹配向量"]
                if live["status"] != "ready":
                    notices.append(live["name"] + "：仍在使用上次完整索引与原嵌入模型")
                rows = db.execute("SELECT c.*,d.name AS source FROM chunks c JOIN documents d ON d.id=c.doc WHERE c.kb=? AND c.generation=?", (ident, live["generation"]))
                while batch := rows.fetchmany(128):
                    valid = [r for r in batch if r["dim"] == len(q)]
                    if not valid:
                        continue
                    matrix = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in valid])
                    scores = matrix @ np.asarray(q, dtype=np.float32)
                    for row, score in zip(valid, scores):
                        location = json.loads(row["location"])
                        lexical_text = (row["text"] + "\n" + location.get("source_context", "") + "\n" + row["source"]).casefold()
                        lexical = sum(term in lexical_text for term in terms) / max(1, len(terms))
                        combined = float(score) * .85 + lexical * .15
                        item = {"id": row["id"], "kb_id": ident, "kb": live["name"], "document_id": row["doc"],
                                "source": row["source"], "text": row["text"], "location": json.loads(row["location"]),
                                 "title_path": location.get("heading_path", []), "context": location.get("source_context", ""),
                                 "score": round(combined, 4), "vector_score": round(float(score), 4),
                                 "keyword_score": round(lexical, 4), "model": model,
                                 "source_kind": "knowledge_chunk", "index_generation": live["generation"]}
                        heapq.heappush(found, (combined, row["id"], item))
                        if len(found) > limit:
                            heapq.heappop(found)
            return [entry[2] for entry in found], notices
        scan_start = time.perf_counter()
        found, notices = await asyncio.to_thread(scan)
        candidates.extend(found)
        warnings.extend(notices)
        scan_seconds += time.perf_counter() - scan_start
    elapsed = time.perf_counter() - started
    return {"items": sorted(candidates, key=lambda x: x["score"], reverse=True)[:limit], "warnings": warnings,
            "query_info": info, "elapsed_seconds": elapsed + preparation_seconds, "retrieval_seconds": elapsed,
            "timings": {"total_seconds": elapsed + preparation_seconds, "query_preparation_seconds": preparation_seconds,
                        "query_embedding_seconds": embedding_seconds, "scan_seconds": scan_seconds},
            "source": "local_hybrid_vector_keywords", "weights": {"vector": .85, "keywords": .15},
            "embedding_runs": embedding_runs,
            "note": "相关片段，不等于全文分析或计算结果"}


def read_chunk(ident, selected):
    with connect() as db:
        row = db.execute("SELECT c.*,d.name AS source,l.name AS library FROM chunks c JOIN documents d ON d.id=c.doc "
                         "JOIN libraries l ON l.id=c.kb WHERE c.id=? AND l.enabled=1 AND c.generation=l.generation", (ident,)).fetchone()
    if row is None or row["kb"] not in selected:
        raise ValueError("片段不存在、已更新或不属于本会话所选知识库")
    location = json.loads(row["location"])
    return {"id": ident, "source": row["source"], "library": row["library"], "text": row["text"], "location": location,
            "title_path": location.get("heading_path", []), "context": location.get("source_context", ""), "source_kind": "knowledge_chunk"}


def definitions(selected):
    note = " 仅能检索本会话选择的知识库：" + ",".join(selected)
    return [{"type": "function", "function": {"name": "knowledge_search", "description": "按当前问题搜索知识库，返回相关片段与来源，不代表全文。" + note,
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 2000}}, "required": ["query"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "knowledge_read", "description": "读取已检索到的完整片段；只限当前所选库。",
        "parameters": {"type": "object", "properties": {"chunk_id": {"type": "string", "maxLength": 32}}, "required": ["chunk_id"], "additionalProperties": False}}}]


def format_results(result):
    return "\n\n".join(f'[{i}. {r["kb"]} / {r["source"]} / 片段 {r["location"]["ordinal"]} / 字符 {r["location"]["start"]}–{r["location"]["end"]}]\n{r["location"].get("source_context", "")}\n{r["text"]}' for i, r in enumerate(result["items"], 1))
