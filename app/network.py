"""Single-workspace login and an opt-in, inference-only Ollama gateway."""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import secrets
import socket
import sqlite3
import threading
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from .config import ROOT, settings, read_json, write_json
from .store import ClosingConnection

LOCK = threading.RLock()
ATTEMPTS = {}
GATEWAY = {"running": False, "error": "", "server": None, "task": None}
COOKIE = "localai_session"


def connect():
    db = sqlite3.connect(ROOT / "data/access.db", timeout=10, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,expires REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS access(id INTEGER PRIMARY KEY,time REAL NOT NULL,ip TEXT NOT NULL,path TEXT NOT NULL,
        model TEXT NOT NULL,status INTEGER NOT NULL,seconds REAL NOT NULL,input_tokens INTEGER,output_tokens INTEGER);
      CREATE INDEX IF NOT EXISTS access_time ON access(time);
    """)
    return db


def is_local(request):
    return bool(request.client and request.client.host in ("127.0.0.1", "::1", "testclient"))


def host_allowed(request):
    host = request.url.hostname
    if host in ("127.0.0.1", "localhost", "testserver", "::1"):
        return True
    if settings()["web_bind"] != "0.0.0.0":
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False  # No arbitrary DNS Host headers / DNS rebinding.


def auth_config():
    return read_json(ROOT / "config/auth.json", {})


def authenticated(request):
    if not auth_config().get("password"):
        return True
    cookie = request.cookies.get(COOKIE, "")
    if not cookie or len(cookie) > 256:
        return False
    with connect() as db:
        row = db.execute("SELECT expires FROM sessions WHERE hash=?", (hashlib.sha256(cookie.encode()).hexdigest(),)).fetchone()
    return bool(row and row[0] > time.time())


def can_manage(request):
    """Authorization is separate from the true network source.

    No-password access is allowed; remote administration remains explicit opt-in.
    """
    if is_local(request):
        return authenticated(request)
    return bool(settings().get("remote_full_access", False) and authenticated(request))


def status(request=None):
    cfg = auth_config()
    return {"password_set": bool(cfg.get("password")), "gateway_key_set": bool(cfg.get("gateway_key")),
            "authenticated": authenticated(request) if request else False,
            "local": is_local(request) if request else False,
            "can_manage": can_manage(request) if request else False,
            "remote_full_access": settings().get("remote_full_access", False),
            "gateway": {k: GATEWAY[k] for k in ("running", "error")}}


def password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def set_password(password):
    if password and not 8 <= len(password) <= 256:
        raise ValueError("访问密码需要 8–256 个字符")
    with LOCK:
        cfg = auth_config()
        if password:
            salt = secrets.token_hex(16)
            cfg["password"] = {"salt": salt, "hash": password_hash(password, salt)}
        else:
            cfg.pop("password", None)
        write_json(ROOT / "config/auth.json", cfg)
        with connect() as db:
            db.execute("DELETE FROM sessions")
    return {"ok": True, "password_set": bool(password)}


def login(request, password, remember=False):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with LOCK:
        previous = [t for t in ATTEMPTS.get(ip, []) if now-t < 300]
        if len(previous) >= 10:
            raise ValueError("尝试过于频繁，请 5 分钟后重试")
        record = auth_config().get("password")
        if record and not secrets.compare_digest(password_hash(str(password)[:256], record["salt"]), record["hash"]):
            ATTEMPTS[ip] = [*previous, now]
            if len(ATTEMPTS) > 1024:
                ATTEMPTS.pop(next(iter(ATTEMPTS)))
            raise ValueError("密码不正确")
        ATTEMPTS.pop(ip, None)
        token = secrets.token_urlsafe(32)
        lifetime = 30 * 86400 if remember else 12 * 3600
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), now+lifetime))
            db.execute("DELETE FROM sessions WHERE hash NOT IN (SELECT hash FROM sessions ORDER BY rowid DESC LIMIT 100)")
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=lifetime if remember else None, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https")
        return response


def logout(request):
    token = request.cookies.get(COOKIE, "")
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    return response


def rotate_gateway_key():
    key = secrets.token_urlsafe(32)
    with LOCK:
        cfg = auth_config()
        cfg["gateway_key"] = hashlib.sha256(key.encode()).hexdigest()
        write_json(ROOT / "config/auth.json", cfg)
    return {"key": key, "note": "仅本次显示，请复制保存；旧密钥立即失效"}


def gateway_authorized(request):
    if not settings()["gateway_require_key"]:
        return True
    header = request.headers.get("authorization", "")
    key = header[7:] if header.lower().startswith("bearer ") else ""
    expected = auth_config().get("gateway_key", "")
    return bool(key and expected and secrets.compare_digest(hashlib.sha256(key.encode()).hexdigest(), expected))


def record(ip, path, model, code, seconds, usage=None):
    usage = usage or {}
    with LOCK, connect() as db:
        db.execute("INSERT INTO access(time,ip,path,model,status,seconds,input_tokens,output_tokens) VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), str(ip)[:80], path[:100], str(model)[:150], int(code), round(seconds, 3),
                    usage.get("prompt_tokens", usage.get("prompt_eval_count")), usage.get("completion_tokens", usage.get("eval_count"))))
        cfg = settings()
        db.execute("DELETE FROM access WHERE time<?", (time.time() - cfg["access_retention_days"]*86400,))
        db.execute("DELETE FROM access WHERE id NOT IN (SELECT id FROM access ORDER BY id DESC LIMIT ?)", (cfg["access_retention_count"],))


def access_logs(page=1, ip="", path=""):
    page = max(1, int(page))
    where = "instr(ip,?)>0 AND instr(path,?)>0"
    args = (ip[:80], path[:100])
    with connect() as db:
        total = db.execute("SELECT count(*) FROM access WHERE " + where, args).fetchone()[0]
        page = min(page, max(1, (total+49)//50))
        rows = db.execute("SELECT * FROM access WHERE " + where + " ORDER BY id DESC LIMIT 50 OFFSET ?", (*args, (page-1)*50)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": 50}


def clear_logs():
    with LOCK, connect() as db:
        db.execute("DELETE FROM access")
    return {"ok": True}


gateway = FastAPI(docs_url=None, redoc_url=None)
READ_PATHS = {"api/version", "api/tags", "api/ps", "v1/models"}
POST_PATHS = {"api/chat", "api/generate", "api/embed", "api/embeddings", "api/show", "v1/chat/completions", "v1/completions", "v1/embeddings"}


@gateway.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def proxy(request: Request, path: str):
    from . import providers, resources, jobs
    ip = request.client.host if request.client else "unknown"  # Never trust arbitrary X-Forwarded-For.
    started, model, job, lock = time.monotonic(), "", None, None
    if not gateway_authorized(request):
        record(ip, "/"+path, "", 401, 0)
        return JSONResponse({"error": "需要有效 Bearer API Key"}, 401)
    if not ((request.method == "GET" and path in READ_PATHS) or (request.method == "POST" and path in POST_PATHS)):
        record(ip, "/"+path, "", 403, 0)
        return JSONResponse({"error": "此入口仅开放列明的推理接口，不提供模型管理或宿主机工具"}, 403)
    try:
        body = None
        if request.method == "POST":
            blob = bytearray()
            async for block in request.stream():
                blob.extend(block)
                if len(blob) > 32 * 1024 * 1024:
                    raise ValueError("请求超过 32 MiB")
            body = json.loads(blob)
            if not isinstance(body, dict):
                raise ValueError("请求需要 JSON 对象")
            model = providers.parse_model(body.get("model", ""))
            await providers.capability("local", model)  # Reject Ollama cloud aliases.
            if path != "api/show":
                resources.check_fault("local")
                resources.admit(int(body.get("options", {}).get("num_ctx", settings()["context"])), allow_large=settings()["allow_large_context"])
                job = jobs.enqueue("local", model, kind="shared_api")
                waiting = asyncio.create_task(jobs.acquire(job))
                try:
                    while not waiting.done():
                        if await request.is_disconnected():
                            job["stop"].set()
                        await asyncio.wait({waiting}, timeout=.25)
                    lock = await waiting
                finally:
                    if not waiting.done():
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
            if path in ("api/chat", "api/generate"):
                body["options"] = {**body.get("options", {})}
                body["options"].setdefault("num_ctx", settings()["context"])
                if settings().get("inference_device", "auto") == "cpu":
                    body["options"]["num_gpu"] = 0
        async with providers.local_client(600) as client:
            upstream = await client.send(client.build_request(request.method, "/"+path, json=body), stream=True)
        code = upstream.status_code
        async def relay():
            usage, pending = {}, b""
            response_failed = False
            try:
                async for block in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        if job:
                            job["stop"].set()
                        break
                    yield block
                    # Read only usage metadata from bounded NDJSON/SSE lines.
                    pending = (pending + block)[-131072:]
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            value = json.loads(line.removeprefix(b"data: "))
                            if isinstance(value, dict) and value.get("error"):
                                resources.fault("local", value["error"])
                                response_failed = True
                            usage.update(value.get("usage") or {k: value[k] for k in ("prompt_eval_count", "eval_count") if k in value})
                        except (ValueError, AttributeError, TypeError):
                            pass
                if pending:
                    with contextlib.suppress(ValueError, AttributeError, TypeError):
                        value = json.loads(pending)
                        if isinstance(value, dict) and value.get("error"):
                            resources.fault("local", value["error"])
                            response_failed = True
                        usage.update(value.get("usage") or {k: value[k] for k in ("prompt_eval_count", "eval_count") if k in value})
            except (asyncio.CancelledError, GeneratorExit):
                if job:
                    job["stop"].set()
                raise
            finally:
                await upstream.aclose()
                if job:
                    jobs.finish(job, lock, "failed" if code >= 400 or response_failed else "done")
                record(ip, "/"+path, model, 499 if job and job["stop"].is_set() else 502 if response_failed else code, time.monotonic()-started, usage)
        return StreamingResponse(relay(), status_code=code, media_type=upstream.headers.get("content-type", "application/json"), headers={"Cache-Control": "no-store"})
    except asyncio.CancelledError:
        if job:
            job["stop"].set()
            jobs.finish(job, lock)
        record(ip, "/"+path, model, 499, time.monotonic()-started)
        raise
    except Exception as error:
        resources.fault("local", error)
        if job:
            jobs.finish(job, lock, "failed")
        record(ip, "/"+path, model, 400, time.monotonic()-started)
        return JSONResponse({"error": str(error)[:400]}, 400)


async def apply_gateway(background):
    previous = GATEWAY.get("server")
    if previous:
        previous.should_exit = True
        task = GATEWAY.get("task")
        if task:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 5)
    GATEWAY.update(running=False, server=None, task=None, error="")
    cfg = settings()
    if not cfg["gateway_enabled"]:
        return
    if cfg["gateway_require_key"] and not auth_config().get("gateway_key"):
        GATEWAY["error"] = "请先生成 API Key，再启用共享入口"
        return
    from .port_manager import bind_available
    try:
        sock, actual_port = bind_available("gateway", cfg["gateway_bind"], cfg["gateway_port"])
    except (OSError, ValueError) as error:
        GATEWAY["error"] = str(error)
        return
    import uvicorn
    GATEWAY["port"] = actual_port
    server = uvicorn.Server(uvicorn.Config(gateway, host=cfg["gateway_bind"], port=actual_port, log_config=None, access_log=False))
    async def serve():
        try:
            GATEWAY["running"] = True
            await server.serve(sockets=[sock])
        finally:
            GATEWAY["running"] = False
            sock.close()
    GATEWAY.update(server=server, task=background(serve()))
