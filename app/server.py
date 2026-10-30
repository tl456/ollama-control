from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import secrets
import shutil
import time
import zipfile
import copy
import sys
from datetime import datetime
from pathlib import Path

import jsonschema

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import attachments, chat, dialogs, providers, store, tools, context as context_manager, lifecycle, python_lab, memory, knowledge, jobs, network, resources, turn_queue, skills
from .config import DEFAULTS, ROOT, VERSION, public_settings, read_json, save_settings, settings, write_json

CONTROL_TOKEN = secrets.token_urlsafe(32)
OWNER_CONTROL_PATHS = frozenset(("/api/ui/show", "/api/ui/quit", "/api/ui/stop-all", "/api/ui/unload-all"))
WEB = Path(__file__).resolve().parents[1] / "web"
TASKS = set()
FILE_TASKS = set()


@contextlib.asynccontextmanager
async def lifespan(app):
    lifecycle.initialize_version()
    write_json(ROOT / "cache/control.json", {"token": CONTROL_TOKEN, "pid": os.getpid()})
    store.connect().close()
    turn_queue.recover()
    knowledge.maintain()
    await network.apply_gateway(background)
    await asyncio.to_thread(providers.start_managed_if_needed)
    background(providers.first_start())
    async def maintain():
        while True:
            await asyncio.sleep(300)
            if not chat.ACTIVE and not turn_queue.busy(include_paused=False) and not FILE_TASKS and not knowledge.RUNNING and not jobs.active() and not python_lab.active_ids():
                await asyncio.to_thread(lifecycle.cleanup)
    background(maintain())
    yield
    await turn_queue.shutdown()
    python_lab.stop()
    if network.GATEWAY.get("server"):
        network.GATEWAY["server"].should_exit = True
    for task in TASKS:
        task.cancel()
    await asyncio.gather(*TASKS, return_exceptions=True)
    await providers.close_clients()
    await asyncio.to_thread(providers.stop_embedding_service)
    await asyncio.to_thread(providers.shutdown_owned_services)


app = FastAPI(title="本地智能工作台", lifespan=lifespan, docs_url=None, redoc_url=None)


def background(coro):
    task = asyncio.create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return task


@app.middleware("http")
async def security(request: Request, call_next):
    if not network.host_allowed(request) or (not network.is_local(request) and settings()["web_bind"] != "0.0.0.0"):
        return JSONResponse({"detail": "当前仅允许本机访问"}, 403)
    request.state.local = network.is_local(request)
    origin = request.headers.get("origin")
    allowed_origins = {f"http://127.0.0.1:{request.url.port or 80}", f"http://localhost:{request.url.port or 80}", str(request.base_url).rstrip("/")}
    if origin and origin not in allowed_origins:
        return JSONResponse({"detail": "来源校验失败"}, 403)
    public_auth = {"/api/auth/status", "/api/auth/login", "/api/auth/logout", "/api/bootstrap"}
    local_control = (request.state.local and request.method == "POST" and request.url.path in OWNER_CONTROL_PATHS
                     and secrets.compare_digest(request.headers.get("x-local-control", ""), CONTROL_TOKEN))
    request.state.local_control = local_control
    request.state.can_manage = local_control or network.can_manage(request)
    if not local_control and not network.authenticated(request):
        if request.url.path.startswith("/api/") and request.url.path not in public_auth:
            return JSONResponse({"detail": "请先登录", "code": "login_required"}, 401)
    host_only = ("/api/settings", "/api/tools", "/api/plugins", "/api/python", "/api/service", "/api/ollama", "/api/maintenance", "/api/ui", "/api/auth/password", "/api/auth/gateway-key", "/api/access", "/api/memory", "/api/factory-reset", "/api/downloads", "/api/runtime/monitor-resume", "/api/audit", "/api/snapshots", "/api/office")
    owner_only = ("/api/auth/password", "/api/auth/gateway-key")
    if not request.state.local and request.url.path.startswith(owner_only):
        return JSONResponse({"detail": "授权和认证只可在宿主机设置"}, 403)
    if not request.state.can_manage and request.url.path.startswith(host_only):
        return JSONResponse({"detail": "宿主机设置、文件和代码管理仅允许本机操作"}, 403)
    if not request.state.can_manage and request.url.path.startswith("/api/models") and request.method != "GET" and request.url.path != "/api/models/show":
        return JSONResponse({"detail": "宿主机模型管理仅允许本机操作"}, 403)
    if not request.state.can_manage and request.url.path.startswith("/api/knowledge") and request.method != "GET":
        return JSONResponse({"detail": "知识库管理仅允许本机操作"}, 403)
    if not request.state.can_manage and request.url.path.startswith("/api/skills") and request.method != "GET":
        return JSONResponse({"detail": "技能管理需要宿主机管理权限"}, 403)
    # All UI clients use the same optional-password Cookie authentication.
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; object-src 'none'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/assets/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse({"detail": str(error), "code": getattr(error, "code", "invalid_request"), "details": getattr(error, "details", {})}, 400)


@app.exception_handler(jsonschema.ValidationError)
async def invalid_schema(request, error):
    return JSONResponse({"detail": "参数格式不正确：" + error.message[:300]}, 400)


@app.exception_handler(KeyError)
async def missing(request, error):
    return JSONResponse({"detail": str(error)}, 404)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/health")
async def health(request: Request):
    return {"app": "Local AI Studio", "display_name": "本地智能工作台", "version": VERSION, "status": "ok", "root": str(ROOT) if request.state.local else ""}


@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    if not network.authenticated(request):
        return {"version": VERSION, "authenticated": False, "can_manage": False, "login_required": True,
                "auth": {"password_set": True, "authenticated": False, "local": request.state.local, "can_manage": False}}
    cfg = public_settings()
    available = tools.list_tools()
    if not request.state.can_manage:
        cfg.update(roots=[], plugins=[], proxy_url="", root="共享工作空间", file_read_mode="native")
        available = [t for t in available if t["id"] in ("system_time", "ask_user")]
    from .port_manager import status as port_status
    return {"settings": cfg, "tools": available, "version": VERSION, "service_ports": port_status(),
            "local_access": request.state.can_manage, "can_manage": request.state.can_manage,
            "actual_local": request.state.local, "is_local": request.state.local, "auth": network.status(request)}


@app.get("/api/jobs")
async def job_list(page: int = 1):
    return jobs.listing(page)


@app.post("/api/jobs/{ident}/cancel")
async def cancel_job(ident: str):
    if ident in jobs.JOBS:
        jobs.JOBS[ident]["stop"].set()
        python_lab.stop(owner=ident)
    return {"ok": True}


@app.get("/api/guide")
async def software_guide(format: str = "json"):
    text = (Path(__file__).resolve().parents[1] / "docs/软件教程.md").read_text(encoding="utf-8")
    if format == "md":
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=LocalAIStudio-guide.md"})
    return {"markdown": text}


@app.get("/api/status")
async def status():
    result = {"online": False, "version": None, "loaded": []}
    try:
        version, loaded, model_list = await asyncio.gather(providers.local("GET", "/api/version"), providers.local("GET", "/api/ps"), providers.models("local"))
        result.update(online=True, version=version.get("version"), loaded=loaded.get("models", []), models=model_list["models"])
    except Exception as error:
        result["error"] = "Ollama 未连接：请启动服务或检查 11437 端口"
    result["hardware"] = await asyncio.to_thread(providers.hardware)
    result["service"] = await asyncio.to_thread(providers.service_ownership)
    return result


@app.get("/api/conversations")
async def conversations(page: int = 1, page_size: int = 20, search: str = "", trash: bool = False):
    return store.listing_page(page, page_size, search, trash)


@app.post("/api/conversations")
async def new_conversation():
    return store.create(settings())


@app.get("/api/conversations/{ident}")
async def get_conversation(ident: str, offset: int = 0, limit: int = 50, around: str = ""):
    conv = store.get(ident)
    if around:
        messages = store.path(conv)
        index = next((i for i, m in enumerate(messages) if m["id"] == around), None)
        if index is None:
            raise ValueError("该消息不在当前对话分支")
        limit = max(1, min(100, limit))
        offset = max(0, len(messages) - min(len(messages), index + max(1, limit // 2)))
    result = store.window(conv, offset, limit)
    result["message_page"]["around"] = around
    return result


@app.get("/api/conversations/{ident}/outline")
async def conversation_outline(ident: str):
    messages = store.path(store.get(ident))
    items = [{"id": m["id"], "summary": m["content"][:120], "index": i}
             for i, m in enumerate(messages) if m["role"] == "user"]
    return {"items": items, "total": len(items)}


@app.get("/api/conversations/{ident}/branches")
async def conversation_branches(ident: str, page: int = 1):
    conv = store.get(ident)
    messages = conv["messages"]
    page = max(1, page)
    return {"items": [{"id": m["id"], "role": m["role"], "content": m["content"][:80]} for m in messages[(page - 1) * 50:page * 50]],
            "page": page, "total": len(messages), "leaf": conv["leaf"]}


def editable(ident):
    if ident in chat.ACTIVE or turn_queue.busy(ident):
        raise ValueError("请先完成、取消或清空此对话的待发任务，再编辑历史或分支")
    return store.get(ident)


@app.get("/api/conversations/{ident}/messages/{message_id}/attachments/{attachment_id}")
async def historical_attachment(ident: str, message_id: str, attachment_id: str):
    conv = await asyncio.to_thread(store.get, ident)
    message = next((m for m in conv["messages"] if m["id"] == message_id), None)
    if not message:
        raise ValueError("消息不存在")
    snapshot = next((a for a in message.get("attachments", []) if a["id"] == attachment_id), None)
    if not snapshot:
        raise ValueError("此消息没有该附件")
    return {**attachments.load(attachment_id), **snapshot, "status": "ready"}


@app.patch("/api/conversations/{ident}")
async def patch_conversation(ident: str, body: dict):
    conv = store.get(ident) if set(body).issubset({"options"}) else editable(ident)
    for key in ("title", "pinned", "deleted"):
        if key in body:
            conv[key] = body[key]
    if "options" in body:
        previous = conv["options"]
        opts = turn_queue.options(previous, body["options"])
        conv["options"] = opts
        if any(opts.get(k) != previous.get(k) for k in ("model", "provider", "context_policy")):
            conv.pop("context_state", None)
    if "leaf" in body:
        if body["leaf"] and not any(m["id"] == body["leaf"] for m in conv["messages"]):
            raise ValueError("分支不存在")
        conv["leaf"] = body["leaf"]
    return store.window(store.put(conv))


@app.delete("/api/conversations/{ident}")
async def delete_conversation(ident: str):
    conv = editable(ident)
    if not conv.get("deleted"):
        raise ValueError("先移入回收站，再选择永久删除")
    store.erase(ident)
    return {"ok": True, "note": "附件按引用保留，可在初始化时集中归档"}


@app.post("/api/conversations/{ident}/message-action")
async def message_action(ident: str, body: dict):
    conv = editable(ident)
    message = next((m for m in conv["messages"] if m["id"] == body["message_id"]), None)
    if not message:
        raise ValueError("消息不存在")
    action = body["action"]
    if action == "edit":
        if message["role"] != "user":
            raise ValueError("只编辑用户消息；回答可重新生成")
        conv["leaf"] = message.get("parent")
        store.append(conv, "user", str(body["content"]), attachments=message.get("attachments", []))
    elif action == "regenerate":
        conv["leaf"] = message.get("parent") if message["role"] == "assistant" else message["id"]
    elif action == "exclude":
        message["excluded"] = not message.get("excluded", False)
    elif action == "truncate":
        conv["leaf"] = message.get("parent")
    else:
        raise ValueError("未知消息操作")
    conv.pop("context_state", None)
    return store.window(store.put(conv))


@app.get("/api/conversations/{ident}/export")
async def export_conversation(ident: str, format: str = "md"):
    conv = store.get(ident)
    if format == "json":
        return Response(json.dumps(conv, ensure_ascii=False, indent=2), media_type="application/json")
    lines = ["# " + str(conv["title"])]
    for message in store.path(conv):
        lines.extend(["\n## " + ("用户" if message["role"] == "user" else "助手"), message["content"]])
        for attach in message.get("attachments", []):
            lines.append(f'\n> 附件：{attach["name"]}（{attach["method"]}）\n\n{attach["text"]}')
        if message.get("interactions"):
            lines.append("\n```json\n" + json.dumps(message["interactions"], ensure_ascii=False, indent=2) + "\n```")
    return Response("\n\n".join(lines), media_type="text/markdown")


@app.post("/api/chat/{ident}")
async def chat_message(ident: str, body: dict, request: Request):
    """Legacy NDJSON client: subscribe to the same independent durable outbox."""
    turn = await turn_queue.enqueue(ident, body, remote=not request.state.local, can_manage=request.state.can_manage)
    async def output():
        async for packet in turn_queue.events(ident, turn["id"]):
            yield json.dumps(packet, ensure_ascii=False) + "\n"
    return StreamingResponse(output(), media_type="application/x-ndjson")


@app.post("/api/chat/{ident}/turns", status_code=202)
async def enqueue_turn(ident: str, body: dict, request: Request):
    turn = await turn_queue.enqueue(ident, body, remote=not request.state.local, can_manage=request.state.can_manage)
    return {"turn": turn, "queue": turn_queue.listing(ident)}


@app.get("/api/chat/{ident}/turns")
async def conversation_turns(ident: str, page: int = 1, page_size: int = 20):
    return turn_queue.listing(ident, page, page_size)


@app.patch("/api/chat/{ident}/turns/{turn_id}")
async def edit_turn(ident: str, turn_id: str, body: dict, request: Request):
    return {"turn": await turn_queue.edit(ident, turn_id, body, remote=not request.state.local, can_manage=request.state.can_manage), "queue": turn_queue.listing(ident)}


@app.post("/api/chat/{ident}/turns/resume")
async def resume_turns(ident: str):
    return turn_queue.resume(ident)


@app.post("/api/chat/{ident}/turns/{turn_id}/cancel")
async def cancel_turn(ident: str, turn_id: str):
    return {"turn": turn_queue.cancel(ident, turn_id), "queue": turn_queue.listing(ident)}


@app.get("/api/chat/{ident}/turns/{turn_id}/events")
async def turn_events(ident: str, turn_id: str, after: int = 0):
    turn_queue.get(turn_id, ident)
    async def output():
        async for packet in turn_queue.events(ident, turn_id, after):
            yield json.dumps(packet, ensure_ascii=False) + "\n"
    return StreamingResponse(output(), media_type="application/x-ndjson")


@app.post("/api/chat/{ident}/stop")
async def stop_chat(ident: str):
    turn_queue.stop_current(ident)
    if ident in chat.ACTIVE:
        chat.ACTIVE[ident].set()
        for job in jobs.JOBS.values():
            if job.get("owner") == ident and job["status"] in ("queued", "running"):
                python_lab.stop(owner=job["id"])
    return {"ok": True}


@app.post("/api/attachments")
async def add_attachment(request: Request, read_mode: str = "native", file: UploadFile = File(...)):
    limit = settings()["upload_mb"] * 1024 * 1024
    blob = await file.read(limit + 1)
    if len(blob) > limit:
        raise ValueError("文件超过大小限制")
    if read_mode not in ("native", "office"):
        raise ValueError("无效读取模式")
    if read_mode == "office" and not request.state.can_manage:
        raise ValueError("共享界面不允许调用宿主机 Office COM，请使用原始读取")
    meta = attachments.upload(file.filename or "attachment", blob, read_mode)
    task = background(asyncio.to_thread(attachments.extract, meta["id"]))
    FILE_TASKS.add(task)
    task.add_done_callback(FILE_TASKS.discard)
    return meta


@app.get("/api/attachments/{ident}")
async def get_attachment(ident: str):
    return attachments.load(ident)


@app.get("/api/attachments/{ident}/image")
async def attachment_image(ident: str):
    folder = attachments.directory(ident)
    if not (folder / "vision.jpg").exists():
        raise HTTPException(404, "图像未准备好")
    return FileResponse(folder / "vision.jpg", media_type="image/jpeg")


@app.post("/api/attachments/{ident}/summary")
async def summarize_attachment(ident: str, body: dict):
    meta = attachments.load(ident)
    text = meta.get("text", "")
    if not text:
        raise ValueError("附件没有可总结的文字")
    provider_id = body.get("provider", "local")
    model = body.get("model", settings()["default_model"])
    if chat.ACTIVE:
        raise ValueError("请等待聊天完成再运行附件摘要，避免同时占用模型")
    opts = {"provider": provider_id, "model": model, "context": int(body.get("context", settings()["context"]))}
    caps = await providers.capability(provider_id, model)
    opts["context"] = min(opts["context"], caps["max_context"])
    async with jobs.slot(providers.queue_key(provider_id), "附件分段摘要", kind="summary") as job:
        result = await context_manager.summarize(text, opts, stop=job["stop"] if job else None, target=min(2048, opts["context"] // 8))
    return {"text": result, "source_sha256": meta["sha256"], "coverage": "已提取文字的全文分段滚动摘要（非一次读完，细节可能丢失）", "model": model, "provider": provider_id}


@app.post("/api/context/preview")
async def context_preview(body: dict):
    conv = await asyncio.to_thread(store.get, body["conversation_id"]) if body.get("conversation_id") else {"messages": [], "leaf": None, "options": {
        "provider": settings()["provider"], "model": settings()["default_model"], "context": settings()["context"], "tool_mode": settings()["tool_mode"], "tools": settings()["tools"]}}
    conv["options"] = turn_queue.options(conv["options"], body.get("options", {}))
    refs = [attachments.snapshot(a["id"], a.get("text"), a.get("mode", "both")) for a in body.get("attachments", [])]
    if body.get("content") or refs:
        store.append(conv, "user", str(body.get("content", "")), attachments=refs)
    defs = tools.definitions(conv["options"].get("tools", [])) if conv["options"]["tool_mode"] != "off" else []
    defs += memory.definitions()
    if conv["options"].get("rag_enabled") and conv["options"].get("rag_mode") == "call":
        defs += knowledge.definitions(conv["options"].get("rag_ids", []))
    return await asyncio.to_thread(context_manager.preview, conv, True, defs)


@app.get("/api/python/runs")
async def python_runs(page: int = 1):
    return await asyncio.to_thread(python_lab.listing, page)


@app.post("/api/python/stop")
async def python_stop(body: dict):
    python_lab.stop(body.get("id"))
    return {"ok": True}


@app.get("/api/python/runs/{ident}/file")
async def python_file(ident: str, name: str):
    base = python_lab.folder(ident)
    target = base / name
    if name != Path(name).name or any(c in name for c in (":", "/", "\\")) or target.is_symlink() or not target.resolve().is_relative_to(base.resolve()) or not target.is_file():
        raise ValueError("只能下载本次实验生成的普通文件")
    if target.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("输出文件超过 20 MiB，请在实验中缩小后导出")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@app.get("/api/models")
async def model_list(provider: str = "local"):
    return await providers.models(provider)


@app.post("/api/models/show")
async def show_model(body: dict):
    return await providers.capability(body.get("provider", "local"), body["model"])


@app.post("/api/models/unload")
async def unload_model(body: dict):
    if chat.ACTIVE or jobs.active("local"):
        raise ValueError("请先等待或取消聊天和共享接口任务")
    return await providers.local("POST", "/api/generate", json={"model": providers.parse_model(body["model"]), "keep_alive": 0})


@app.delete("/api/models")
async def delete_model(body: dict):
    if chat.ACTIVE or jobs.active() or knowledge.RUNNING:
        raise ValueError("请先等待或取消聊天、共享接口和知识库任务")
    if body.get("confirm") != body["model"]:
        raise ValueError("请确认模型名称")
    return await providers.local("DELETE", "/api/delete", json={"model": providers.parse_model(body["model"])})


@app.post("/api/downloads")
async def start_download(body: dict):
    model = providers.parse_model(body["model"])
    if any(not j.get("done") and j["model"] == model for j in providers.DOWNLOADS.values()):
        raise ValueError("该模型已在下载")
    ident = store.uid()
    providers.DOWNLOADS[ident] = {"id": ident, "model": model, "status": "准备下载", "done": False, "total": 0, "completed": 0, "created": time.time()}
    async def pull():
        try:
            await providers.pull_task(ident, model)
        finally:
            from . import downloads as records
            providers.DOWNLOADS[ident]["ended"] = time.time()
            records.remember(providers.DOWNLOADS[ident])
    background(pull())
    return providers.DOWNLOADS[ident]


@app.get("/api/downloads")
async def downloads():
    from . import downloads as records
    return records.listing(providers.DOWNLOADS)


@app.post("/api/downloads/dismiss-finished")
async def dismiss_finished_downloads():
    from . import downloads as records
    return records.dismiss_finished(providers.DOWNLOADS)


@app.post("/api/downloads/{ident}/dismiss")
async def dismiss_download(ident: str):
    from . import downloads as records
    return records.dismiss(ident, providers.DOWNLOADS)


@app.post("/api/downloads/{ident}/cancel")
async def cancel_download(ident: str):
    providers.DOWNLOADS[ident]["cancel"] = True
    return {"ok": True}


@app.post("/api/service/start")
async def start_service():
    if chat.ACTIVE or turn_queue.busy(include_paused=False) or jobs.active() or knowledge.RUNNING:
        raise ValueError("请先等待或停止当前任务，再更改服务状态")
    try:
        return await providers.local("GET", "/api/version")
    except Exception:
        return await asyncio.to_thread(providers.launch_ollama)


@app.get("/api/ollama/service")
async def ollama_service_status():
    return await providers.service_status()


@app.post("/api/ollama/stop")
async def ollama_service_stop():
    if chat.ACTIVE or turn_queue.busy(include_paused=False) or jobs.active() or knowledge.RUNNING or any(not j.get("done") for j in providers.DOWNLOADS.values()):
        raise ValueError("请先等待或停止当前任务，再停止自管服务")
    return await asyncio.to_thread(providers.stop_local_service)


@app.get("/api/tools")
async def tool_list():
    return tools.list_tools()


@app.post("/api/service/restart")
async def restart_service():
    if chat.ACTIVE or turn_queue.busy(include_paused=False) or jobs.active() or knowledge.RUNNING or any(not j.get("done") for j in providers.DOWNLOADS.values()):
        raise ValueError("先等待或停止聊天、共享接口、知识库和下载任务再重启")
    return await asyncio.to_thread(providers.restart_ollama)


@app.post("/api/tools/test")
async def test_tool(body: dict):
    return await asyncio.to_thread(tools.execute, body["name"], body.get("arguments", {}), [body["name"]])


@app.get("/api/tools/{ident}/settings")
async def get_tool_settings(ident: str):
    return tools.tool_configuration(ident)


@app.post("/api/ui/pick-directory")
async def pick_directory():
    return {"path": await asyncio.to_thread(dialogs.choose_directory)}


@app.post("/api/ui/show")
async def show_ui():
    write_json(ROOT / "cache/ui-show.json", {"time": time.time()})
    return {"ok": True}


@app.post("/api/ui/stop-all")
async def stop_all():
    turn_queue.stop_all()
    for event in chat.ACTIVE.values():
        event.set()
    for job in jobs.JOBS.values():
        if job["status"] in ("queued", "running"):
            job["stop"].set()
    for ident in list(knowledge.RUNNING):
        knowledge.cancel(ident)
    python_lab.stop()
    return {"ok": True}


@app.post("/api/ui/quit")
async def quit_ui(request: Request):
    permitted_remote = request.state.can_manage and not request.state.local
    if not permitted_remote and not request.state.local_control:
        raise HTTPException(403, "此操作仅供宿主机托盘正常退出")
    callback = getattr(request.app.state, "shutdown_callback", None)
    if not callable(callback):
        raise HTTPException(409, "当前启动方式未提供正常退出控制，请关闭启动终端")
    asyncio.get_running_loop().call_later(.1, callback)
    return {"ok": True, "note": "正在正常退出，待发任务将暂停，不会静默重放"}


@app.post("/api/ui/unload-all")
async def unload_all():
    if chat.ACTIVE or jobs.active("local"):
        raise ValueError("请先等待或取消聊天和共享接口任务")
    for model in (await providers.local("GET", "/api/ps")).get("models", []):
        await providers.local("POST", "/api/generate", json={"model": model["name"], "keep_alive": 0})
    return {"ok": True}


@app.put("/api/tools/{ident}/settings")
async def put_tool_settings(ident: str, body: dict):
    if chat.ACTIVE:
        raise ValueError("请先停止生成，再修改工具配置")
    return tools.save_tool_configuration(ident, body)


@app.post("/api/tools/import")
async def add_plugin(file: UploadFile = File(...)):
    blob = await file.read(20 * 1024 * 1024 + 1)
    if chat.ACTIVE:
        raise ValueError("请先停止生成")
    return tools.import_plugin(blob)


@app.get("/api/tools/{ident}/source")
async def plugin_source(ident: str):
    if not any(t["id"] == ident and not t["builtin"] for t in tools.list_tools()):
        raise ValueError("插件不存在")
    folder = ROOT / "plugins/imported" / ident
    return {"manifest": read_json(folder / "manifest.json"), "source": (folder / "tool.py").read_text(encoding="utf-8")[:50000], "hashes": read_json(folder / ".integrity.json")}


@app.delete("/api/tools/{ident}")
async def remove_plugin(ident: str):
    if chat.ACTIVE:
        raise ValueError("请先停止生成")
    cfg = settings()
    if ident in cfg["plugins"]:
        raise ValueError("请先停用插件")
    if not any(t["id"] == ident and not t["builtin"] for t in tools.list_tools()):
        raise ValueError("插件不存在")
    folder = ROOT / "plugins/imported" / ident
    lifecycle.safe_remove(folder, ROOT / "plugins/imported")
    return {"ok": True, "backup": None}


@app.get("/api/example-plugin")
async def example_plugin():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in (Path(__file__).resolve().parents[1] / "plugins/examples/time_demo").glob("*"):
            if file.is_file():
                archive.write(file, file.name)
    return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": "attachment; filename=time_demo.zip"})


# Preserve the old function-level facade for integrations while each HTTP group
# has one route implementation. Shared middleware above remains authoritative.
from .api import install_routes
globals().update(install_routes(app, sys.modules[__name__]))

app.mount("/assets", StaticFiles(directory=WEB), name="assets")
