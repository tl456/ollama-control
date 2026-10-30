"""Persistent, bounded per-conversation outbox; HTTP is only a subscriber.

Future user messages stay outside the conversation graph until their own turn
gets the backend slot. No queued request is silently replayed after a restart.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from collections import deque

from . import attachments, chat, context as context_manager, jobs, knowledge, providers, python_lab, store, tools
from .config import settings

PENDING = ("queued", "running", "paused")
TERMINAL = ("done", "failed", "cancelled", "interrupted")
WORKERS = {}
CHANNELS = {}
STOPS = {}
SHUTTING_DOWN = False
MAX_PER_CONVERSATION = 8
MAX_GLOBAL = 64
MAX_EVENTS = 128
MAX_EVENT_CHARS = 262144
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
GENERATION_KEYS = ("output_tokens", "system_prompt", "compression_trigger", "recent_ratio", "attachment_ratio", "rag_query_enabled", "rag_query_prompt", "max_tool_calls")

OPTION_KEYS = {"model", "provider", "context", "tool_mode", "tools", "think", "analysis_mode", "context_policy",
               "sampling_mode", "temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty", "seed",
               "rag_enabled", "rag_ids", "rag_mode", "skill_ids", "inference_device", "reasoning_effort"}


def options(base, patch=None):
    if patch is not None and not isinstance(patch, dict):
        raise ValueError("对话参数必须是对象")
    value = copy.deepcopy(base)
    cfg = settings()
    for key in ("sampling_mode", "temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty", "seed", "context_policy"):
        value.setdefault(key, cfg[key])
    # Global device changes affect future submissions in existing conversations;
    # already queued requests retain their own immutable options below.
    value["inference_device"] = cfg.get("inference_device", "auto")
    value.setdefault("reasoning_effort", "inherit")
    value.update({k: v for k, v in (patch or {}).items() if k in OPTION_KEYS})
    if value["inference_device"] not in ("auto", "cpu"):
        raise ValueError("聊天设备需要选择 auto 或 cpu")
    if value["reasoning_effort"] not in ("inherit", "off", "low", "medium", "high", "max"):
        raise ValueError("思考深度需要选择 inherit/off/low/medium/high/max")
    value["_resolved_reasoning_effort"] = (providers.provider(value.get("provider", "local")).get("reasoning_effort", "high")
        if value["reasoning_effort"] == "inherit" else value["reasoning_effort"])
    value["_requested_device"] = value["inference_device"]
    value["rag_query_enabled"] = cfg.get("rag_query_enabled", True)
    value["rag_query_prompt"] = cfg.get("rag_query_prompt", "")
    value.pop("attachment_ids", None)
    value.pop("python_run_ids", None)
    value.pop("skill_snapshot", None)
    if value.get("tool_mode") not in ("auto", "native", "legacy", "off"):
        raise ValueError("工具模式无效")
    try:
        value["context"] = int(value["context"])
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("上下文需要有效的整数容量") from error
    if not 2048 <= value["context"] <= 2147483647:
        raise ValueError("上下文须为不小于 2K 的有效整数 Token 数")
    if value.get("rag_mode", "original") not in ("original", "call"):
        raise ValueError("知识库检索方式无效")
    if value.get("context_policy") not in ("summary", "truncate", "manual"):
        raise ValueError("上下文处理方式无效")
    if value.get("sampling_mode") not in ("recommended", "model", "custom"):
        raise ValueError("生成参数模式无效")
    for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1), ("min_p", 0, 1),
                           ("repeat_penalty", 0, 2), ("presence_penalty", -2, 2), ("top_k", 0, 200), ("seed", -1, 2147483647)):
        try:
            number = float(value[key])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(key + " 需要有效数值") from error
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{key} 需要 {low} 至 {high} 之间的有限数值")
        value[key] = int(number) if key in ("top_k", "seed") else number
    for key, limit in (("tools", 30), ("rag_ids", 8), ("skill_ids", 2)):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > limit or any(not isinstance(x, str) for x in items):
            raise ValueError(f"{key} 参数格式错误或超过上限 {limit}")
        value[key] = list(dict.fromkeys(items))
    for ident in value["rag_ids"]:
        knowledge.get(ident)
    from . import skills
    value["skill_snapshot"] = skills.snapshot(value["skill_ids"])
    value["_rag_top_k"] = cfg["rag_top_k"]
    value["_generation_settings"] = {key: copy.deepcopy(cfg[key]) for key in GENERATION_KEYS}
    if not isinstance(value.get("provider"), str) or not value["provider"] or not isinstance(value.get("model"), str) or not value["model"]:
        raise ValueError("请选择连接和模型")
    return value


def _decode(row):
    if not row:
        raise KeyError("待发消息不存在")
    value = json.loads(row[8])
    value.update(seq=row[0], id=row[1], conversation_id=row[2], client_request_id=row[3], status=row[4],
                 created=row[5], updated=row[6], revision=row[7])
    return value


def get(ident, conversation=None):
    with store.connect() as db:
        value = _decode(db.execute("SELECT * FROM chat_turns WHERE id=?", (ident,)).fetchone())
    if conversation is not None and value["conversation_id"] != conversation:
        raise KeyError("待发消息不属于此对话")
    return value


def _write(db, value):
    value["updated"] = time.time()
    value["revision"] += 1
    payload = {k: v for k, v in value.items() if k not in {"seq", "id", "conversation_id", "client_request_id", "status", "created", "updated", "revision"}}
    db.execute("UPDATE chat_turns SET status=?,updated=?,revision=?,payload=? WHERE id=?",
               (value["status"], value["updated"], value["revision"], json.dumps(payload, ensure_ascii=False), value["id"]))
    return value


def busy(conversation=None, include_paused=True):
    statuses = PENDING if include_paused else ("queued", "running")
    with store.connect() as db:
        sql = "SELECT 1 FROM chat_turns WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
        args = list(statuses)
        if conversation is not None:
            sql += " AND conversation=?"
            args.append(conversation)
        return bool(db.execute(sql + " LIMIT 1", args).fetchone())


def public(value):
    result = {k: copy.deepcopy(v) for k, v in value.items() if k not in ("remote", "cloud_confirmed", "attachment_snapshots", "options", "seq")}
    result["options"] = {k: copy.deepcopy(v) for k, v in value["options"].items() if k not in ("skill_snapshot", "attachment_ids", "python_run_ids") and not k.startswith("_")}
    result["rag_top_k"] = value["options"].get("_rag_top_k", 2)
    result["attachments"] = [{k: copy.deepcopy(v) for k, v in item.items() if k not in ("text", "injected_text")} |
                             {"text": "", "text_chars": len(item.get("text", ""))} for item in value.get("attachment_snapshots", [])]
    channel = CHANNELS.get(value["id"])
    result["last_event_id"] = channel["cursor"] if channel else 0
    result["live_message"] = None
    if channel and channel.get("conv"):
        live = next((m for m in reversed(channel["conv"]["messages"]) if m["role"] == "assistant" and m.get("turn_id") == value["id"]), None)
        result["live_message"] = copy.deepcopy(live) if live else None
    return result


def listing(conversation, page=1, page_size=20):
    store.get(conversation)
    page, page_size = max(1, int(page)), max(5, min(50, int(page_size)))
    with store.connect() as db:
        total = db.execute("SELECT count(*) FROM chat_turns WHERE conversation=?", (conversation,)).fetchone()[0]
        page = min(page, max(1, (total + page_size - 1) // page_size))
        rows = db.execute("SELECT * FROM chat_turns WHERE conversation=? ORDER BY CASE WHEN status IN ('queued','running','paused') THEN 0 ELSE 1 END,CASE WHEN status IN ('queued','running','paused') THEN seq ELSE -seq END LIMIT ? OFFSET ?",
                          (conversation, page_size, (page - 1) * page_size)).fetchall()
        active = db.execute("SELECT id,status,json_extract(payload,'$.paused_reason') FROM chat_turns WHERE conversation=? AND status IN ('queued','running','paused') ORDER BY seq", (conversation,)).fetchall()
    order = {row[0]: index + 1 for index, row in enumerate(active)}
    items = [public(_decode(row)) for row in rows]
    for item in items:
        item["position"] = order.get(item["id"], 0)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "running_id": next((row[0] for row in active if row[1] == "running"), None),
            "paused": any(row[1] == "paused" for row in active),
            "paused_reason": next((row[2] or "" for row in active if row[1] == "paused"), ""),
            "pending_count": len(active), "limit": MAX_PER_CONVERSATION}


def _channel(ident):
    if ident not in CHANNELS:
        CHANNELS[ident] = {"cursor": 0, "events": deque(), "chars": 0, "conv": None, "status": "queued", "updated": time.time()}
    return CHANNELS[ident]


def emit(ident, packet):
    channel = _channel(ident)
    channel["cursor"] += 1
    event = {**packet, "turn_id": ident, "event_id": channel["cursor"]}
    if packet.get("type") == "turn_state" and packet.get("turn"):
        channel["status"] = packet["turn"]["status"]
    # Full history stays in SQLite, not in every retained stream event.
    cached = {k: v for k, v in event.items() if k != "conversation"}
    size = len(json.dumps(cached, ensure_ascii=False))
    if size <= MAX_EVENT_CHARS:
        channel["events"].append((channel["cursor"], cached, size))
        channel["chars"] += size
    else:
        channel["events"].clear()
        channel["chars"] = 0
    while len(channel["events"]) > MAX_EVENTS or channel["chars"] > MAX_EVENT_CHARS:
        channel["chars"] -= channel["events"].popleft()[2]
    channel["updated"] = time.time()


def _trim():
    with store.LOCK, store.connect() as db:
        db.execute("DELETE FROM chat_turns WHERE status IN ('done','failed','cancelled','interrupted') AND (updated<? OR id NOT IN (SELECT id FROM chat_turns WHERE status IN ('done','failed','cancelled','interrupted') ORDER BY seq DESC LIMIT 100))", (time.time() - 7 * 86400,))
    inactive = [(key, value) for key, value in CHANNELS.items() if key not in STOPS]
    for key, value in sorted(inactive, key=lambda pair: pair[1]["updated"], reverse=True)[20:]:
        CHANNELS.pop(key, None)


async def enqueue(conversation, body, remote=False, can_manage=None):
    if SHUTTING_DOWN:
        raise ValueError("软件正在退出，请稍后重新启动")
    conv = store.get(conversation)
    if conv.get("deleted"):
        raise ValueError("请先恢复对话")
    request_id = body.get("client_request_id") or store.uid()
    if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
        raise ValueError("消息提交标识无效")
    with store.connect() as db:
        duplicate = db.execute("SELECT * FROM chat_turns WHERE conversation=? AND request_id=?", (conversation, request_id)).fetchone()
    if duplicate:
        return public(_decode(duplicate))
    if body.get("regenerate") and busy(conversation):
        raise ValueError("对话仍有待发任务，请先完成或取消，再重新生成")
    value = await _snapshot(conv, body, remote, can_manage)
    with store.LOCK, store.connect() as db:
        duplicate = db.execute("SELECT * FROM chat_turns WHERE conversation=? AND request_id=?", (conversation, request_id)).fetchone()
        if duplicate:
            return public(_decode(duplicate))
        count = db.execute("SELECT count(*) FROM chat_turns WHERE conversation=? AND status IN ('queued','running','paused')", (conversation,)).fetchone()[0]
        all_count = db.execute("SELECT count(*) FROM chat_turns WHERE status IN ('queued','running','paused')").fetchone()[0]
        if count >= MAX_PER_CONVERSATION or all_count >= MAX_GLOBAL:
            raise ValueError("待发队列已满，请等待或取消部分消息（每个对话最多 8 条）")
        if not value["content"].strip() and not value["attachment_snapshots"]:
            if count or not store.path(conv) or store.path(conv)[-1]["role"] != "user":
                raise ValueError("请输入问题或上传附件")
            value["reuse_user"] = True
        paused = db.execute("SELECT payload FROM chat_turns WHERE conversation=? AND status='paused' LIMIT 1", (conversation,)).fetchone()
        status = "paused" if paused else "queued"
        value["paused_reason"] = json.loads(paused[0]).get("paused_reason", "等待明确继续") if paused else ""
        ident, stamp = store.uid(), time.time()
        db.execute("INSERT INTO chat_turns(id,conversation,request_id,status,created,updated,revision,payload) VALUES(?,?,?,?,?,?,1,?)",
                   (ident, conversation, request_id, status, stamp, stamp, json.dumps(value, ensure_ascii=False)))
    _channel(ident)["status"] = status
    _trim()
    kick(conversation)
    return public(get(ident))


async def _snapshot(conv, body, remote, can_manage=None):
    content = body.get("content", "")
    if not isinstance(content, str) or len(content) > 100000:
        raise ValueError("单条输入最多 10 万字符，超长内容请上传附件；未静默截断")
    refs = body.get("attachments", [])
    if not isinstance(refs, list) or len(refs) > settings()["max_attachments"]:
        raise ValueError("附件数量超过设置上限")
    snapshots = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
            raise ValueError("附件引用格式无效")
        if ref.get("text") is not None and not isinstance(ref["text"], str):
            raise ValueError("附件选取文字格式无效")
        snapshots.append(await asyncio.to_thread(attachments.snapshot, ref["id"], ref.get("text"), ref.get("mode", "both")))
    option_patch = body.get("options")
    requested_provider = (option_patch or {}).get("provider", conv["options"].get("provider", "local")) if option_patch is None or isinstance(option_patch, dict) else "local"
    if requested_provider != "local" and body.get("cloud_confirmed") is not True:
        raise ValueError("此连接会发送消息、附件、记忆和知识片段到其他主机，请先确认数据去向")
    opts = options(conv["options"], option_patch)
    restricted = bool(remote and not can_manage)
    if restricted:
        opts.update(tools=[name for name in opts["tools"] if name in ("system_time", "ask_user")], skill_ids=[], skill_snapshot=[])
    if opts["provider"] != "local" and body.get("cloud_confirmed") is not True:
        raise ValueError("此连接会发送消息、附件、记忆和知识片段到其他主机，请先确认数据去向")
    value = {"content": content, "attachment_snapshots": snapshots, "options": opts,
             "cloud_confirmed": body.get("cloud_confirmed") is True, "remote": bool(remote),
             "restricted": restricted, "paused_reason": ""}
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("本条消息附件快照超过 4 MiB，请缩小选取文字或分条发送")
    return value


async def edit(conversation, ident, body, remote=False, can_manage=None):
    original = get(ident, conversation)
    if original["status"] not in ("queued", "paused"):
        raise ValueError("只有尚未执行的待发消息可以修改")
    if body.get("revision") != original["revision"]:
        raise ValueError("待发消息已变化，请刷新后重试")
    if original.get("reuse_user"):
        raise ValueError("重新生成任务不能编辑，请取消后重新发送")
    # Fields omitted by PATCH retain the exact prior snapshot, not current UI defaults.
    merged = copy.deepcopy(original)
    if any(key in body for key in ("content", "attachments", "options", "cloud_confirmed")):
        value = {"content": body.get("content", original["content"]),
                 "options": body.get("options", {}), "cloud_confirmed": body.get("cloud_confirmed", original["cloud_confirmed"])}
        value["attachments"] = body.get("attachments", [])
        # Keep original server-issued skill revisions unless skill selection changes.
        updated = await _snapshot({"options": original["options"]}, value, remote or original["remote"], can_manage)
        if "attachments" not in body:
            updated["attachment_snapshots"] = original["attachment_snapshots"]
        if "skill_ids" not in (body.get("options") or {}):
            updated["options"]["skill_snapshot"] = original["options"].get("skill_snapshot", [])
        updated["options"]["_rag_top_k"] = original["options"].get("_rag_top_k", 2)
        updated["options"]["_generation_settings"] = original["options"].get("_generation_settings", updated["options"]["_generation_settings"])
        for key in ("rag_query_enabled", "rag_query_prompt"):
            updated["options"][key] = original["options"].get(key, updated["options"][key])
        if "reasoning_effort" not in (body.get("options") or {}):
            updated["options"]["_resolved_reasoning_effort"] = original["options"].get("_resolved_reasoning_effort", updated["options"]["_resolved_reasoning_effort"])
        if "inference_device" not in (body.get("options") or {}):
            updated["options"]["inference_device"] = original["options"].get("inference_device", updated["options"]["inference_device"])
            updated["options"]["_requested_device"] = updated["options"]["inference_device"]
        merged.update(updated, paused_reason=original.get("paused_reason", ""))
    if not merged["content"].strip() and not merged["attachment_snapshots"]:
        raise ValueError("请输入问题或上传附件")
    with store.LOCK, store.connect() as db:
        current = _decode(db.execute("SELECT * FROM chat_turns WHERE id=?", (ident,)).fetchone())
        if current["status"] not in ("queued", "paused") or current["revision"] != original["revision"]:
            raise ValueError("待发消息已经开始或被修改，请刷新")
        merged = _write(db, merged)
        if len(json.dumps(merged, ensure_ascii=False).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("修改后的消息快照超过 4 MiB，请缩小附件选取范围")
    return public(merged)


def _pause(db, conversation, reason):
    rows = db.execute("SELECT * FROM chat_turns WHERE conversation=? AND status='queued'", (conversation,)).fetchall()
    for row in rows:
        value = _decode(row)
        value.update(status="paused", paused_reason=reason[:240])
        _write(db, value)
        emit(value["id"], {"type": "turn_state", "turn": public(value)})


def cancel(conversation, ident):
    with store.LOCK, store.connect() as db:
        value = _decode(db.execute("SELECT * FROM chat_turns WHERE id=?", (ident,)).fetchone())
        if value["conversation_id"] != conversation:
            raise KeyError("待发消息不属于此对话")
        if value["status"] == "running":
            if ident in STOPS:
                STOPS[ident].set()
            if value.get("job_id"):
                python_lab.stop(owner=value["job_id"])
            value["cancel_requested"] = True
            _pause(db, conversation, "上一轮已请求停止，后续消息暂停；请确认是否继续")
        elif value["status"] in ("queued", "paused"):
            value.update(status="cancelled", ended=time.time())
        value = _write(db, value)
    emit(ident, {"type": "turn_state", "turn": public(value)})
    return public(value)


def stop_current(conversation):
    with store.connect() as db:
        row = db.execute("SELECT * FROM chat_turns WHERE conversation=? AND status='running' ORDER BY seq LIMIT 1", (conversation,)).fetchone()
    return cancel(conversation, _decode(row)["id"]) if row else None


def stop_all():
    with store.LOCK, store.connect() as db:
        rows = db.execute("SELECT * FROM chat_turns WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            value = _decode(row)
            if value["status"] == "running":
                if value["id"] in STOPS:
                    STOPS[value["id"]].set()
            else:
                value.update(status="paused", paused_reason="全部任务已暂停，请明确继续")
                _write(db, value)
                emit(value["id"], {"type": "turn_state", "turn": public(value)})


def resume(conversation):
    store.get(conversation)
    if conversation in chat.ACTIVE:
        raise ValueError("请等待当前停止完成再继续")
    with store.LOCK, store.connect() as db:
        rows = db.execute("SELECT * FROM chat_turns WHERE conversation=? AND status='paused' ORDER BY seq", (conversation,)).fetchall()
        for row in rows:
            value = _decode(row)
            value.update(status="queued", paused_reason="")
            _write(db, value)
    kick(conversation)
    return listing(conversation)


def recover():
    global SHUTTING_DOWN
    SHUTTING_DOWN = False
    CHANNELS.clear()
    STOPS.clear()
    with store.LOCK, store.connect() as db:
        rows = db.execute("SELECT * FROM chat_turns WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            value = _decode(row)
            if value["status"] == "running":
                value.update(status="interrupted", error="上次程序退出中断此轮，未自动重发；请检查历史后重新发送", ended=time.time())
                if value.get("user_question", {}).get("status") == "pending":
                    value["user_question"].update(status="skipped", answer="")
                for item in value.get("steering", []):
                    if item.get("status") == "pending":
                        item["status"] = "not_applied"
            else:
                value.update(status="paused", paused_reason="软件重新启动，待发任务未自动重放；请检查后明确继续")
            _write(db, value)
        # Legacy abandoned marker is only display metadata, never a resumable job.
        for ident, payload in db.execute("SELECT id,payload FROM conversations WHERE json_extract(payload,'$.pending_job') IS NOT NULL").fetchall():
            conv = json.loads(payload)
            conv.pop("pending_job", None)
            db.execute("UPDATE conversations SET payload=? WHERE id=?", (json.dumps(conv, ensure_ascii=False), ident))
    _trim()


def kick(conversation):
    if SHUTTING_DOWN:
        return
    worker = WORKERS.get(conversation)
    if worker and not worker.done():
        return
    task = asyncio.create_task(_worker(conversation))
    WORKERS[conversation] = task
    def finished(completed):
        if WORKERS.get(conversation) is completed:
            WORKERS.pop(conversation, None)
        if not completed.cancelled():
            completed.exception()  # Retrieve exceptions; failed turns are recorded by _worker.
    task.add_done_callback(finished)


async def _worker(conversation):
    while not SHUTTING_DOWN:
        with store.LOCK, store.connect() as db:
            row = db.execute("SELECT * FROM chat_turns WHERE conversation=? AND status IN ('queued','paused') ORDER BY seq LIMIT 1", (conversation,)).fetchone()
            if not row:
                return
            value = _decode(row)
            if value["status"] == "paused":
                return
            value.update(status="running", started=time.time())
            _write(db, value)
        await _execute(value)


async def _execute(value):
    ident, conversation = value["id"], value["conversation_id"]
    stop = asyncio.Event()
    STOPS[ident] = stop
    chat.ACTIVE[conversation] = stop
    channel = _channel(ident)
    lock = acquire_task = job = owner_token = job_token = conv = None
    outcome, failure = "done", ""
    try:
        opts = value["options"]
        if jobs.MAINTENANCE.is_set():
            emit(ident, {"type": "phase", "text": "正在应用设备偏好，本条继续排队"})
        while jobs.MAINTENANCE.is_set():
            if stop.is_set():
                raise asyncio.CancelledError()
            await asyncio.sleep(.1)
        job = jobs.enqueue(providers.queue_key(opts["provider"]), opts["model"], owner=conversation)
        job["stop"] = stop
        value["job_id"] = job["id"]
        with store.LOCK, store.connect() as db:
            current = _decode(db.execute("SELECT * FROM chat_turns WHERE id=?", (ident,)).fetchone())
            current["job_id"] = job["id"]
            value = _write(db, current)
        emit(ident, {"type": "turn_state", "turn": public(value)})
        acquire_task = asyncio.create_task(jobs.acquire(job))
        while not acquire_task.done():
            emit(ident, {"type": "queued", "job_id": job["id"], "position": jobs.position(job), "text": "等待前面的任务完成；不会打断其他对话"})
            await asyncio.wait({acquire_task}, timeout=.5)
        lock = await acquire_task
        owner_token = jobs.OWNER.set(providers.queue_key(opts["provider"]))
        job_token = jobs.CURRENT_JOB.set(job["id"])
        if stop.is_set():
            raise asyncio.CancelledError()
        caps_task = asyncio.create_task(providers.capability(opts["provider"], opts["model"]))
        try:
            while not caps_task.done():
                if stop.is_set():
                    raise asyncio.CancelledError()
                await asyncio.wait({caps_task}, timeout=.1)
            caps = await caps_task
        finally:
            if not caps_task.done():
                caps_task.cancel()
                await asyncio.gather(caps_task, return_exceptions=True)
        if caps.get("embedding"):
            raise ValueError("这是嵌入模型，请在知识库中使用，不用于聊天")
        if opts.get("rag_enabled") and not opts.get("rag_ids"):
            raise ValueError("已启用知识库，请至少选择一个库")
        conv = store.get(conversation)
        if conv.get("deleted"):
            raise ValueError("对话已经移入回收站")
        if any(conv["options"].get(k) != opts.get(k) for k in ("provider", "model", "context", "context_policy")):
            conv.pop("context_state", None)
        restricted = value.get("restricted", value["remote"]) or (value["remote"] and
            not settings().get("remote_full_access", False))
        conv.update(options=copy.deepcopy(opts), _remote_request=value["remote"], _restricted_request=bool(restricted), _turn_snapshot=True, _job_id=job["id"], _turn_id=ident,
                    pending_job={"id": job["id"], "status": "running", "turn_id": ident})
        if not value.get("reuse_user"):
            user = store.append(conv, "user", value["content"], attachments=copy.deepcopy(value["attachment_snapshots"]), turn_id=ident)
            value["user_message_id"] = user["id"]
            if conv["title"] == "新对话":
                conv["title"] = (value["content"].strip() or "附件对话")[:28]
        elif not store.path(conv) or store.path(conv)[-1]["role"] != "user":
            raise ValueError("待重新生成的用户消息已变化，请重新发送")
        if opts.get("context_policy", settings()["context_policy"]) == "manual":
            definitions = tools.definitions(opts.get("tools", [])) if opts["tool_mode"] != "off" else None
            report = context_manager.preview(conv, caps["vision"], definitions)
            if report["total"] > opts["context"]:
                raise ValueError("手动模式下上下文超限，请选择摘要、截断或扩大上下文")
        store.put(conv)
        channel["conv"] = conv
        emit(ident, {"type": "accepted", "conversation": store.window(store.get(conversation)), "user_message_id": value.get("user_message_id")})
        async for packet in chat.run(conv, caps, stop):
            if packet["type"] == "start":
                value["message_id"] = packet["message_id"]
                message = next(m for m in conv["messages"] if m["id"] == packet["message_id"])
                message["turn_id"] = ident
                store.put(conv)
                with store.LOCK, store.connect() as db:
                    current = _decode(db.execute("SELECT * FROM chat_turns WHERE id=?", (ident,)).fetchone())
                    current.update({key: value[key] for key in ("message_id", "user_message_id", "job_id") if key in value})
                    value = _write(db, current)
            if packet["type"] == "error":
                outcome, failure = "failed", packet.get("message", "本轮执行失败")
            if packet["type"] == "done":
                continue  # Only expose done after durable terminal state is committed.
            emit(ident, packet)
        if stop.is_set():
            outcome, failure = "cancelled", "上一轮已停止，后续消息暂停；请检查后明确继续"
    except asyncio.CancelledError:
        outcome = "interrupted" if SHUTTING_DOWN else "cancelled"
        failure = "程序退出中断此轮，后续未自动重放" if SHUTTING_DOWN else "上一轮已停止，后续消息暂停；请检查后明确继续"
        stop.set()
        if job:
            python_lab.stop(owner=job["id"])
    except Exception as error:
        outcome, failure = "failed", str(error)[:500]
        emit(ident, {"type": "error", "message": failure})
    finally:
        if acquire_task and not acquire_task.done():
            acquire_task.cancel()
            await asyncio.gather(acquire_task, return_exceptions=True)
        if acquire_task and acquire_task.done() and not acquire_task.cancelled() and acquire_task.exception() is None and lock is None:
            lock = acquire_task.result()
        if job:
            jobs.finish(job, lock, "failed" if outcome == "failed" else "done")
        if owner_token is not None:
            jobs.OWNER.reset(owner_token)
        if job_token is not None:
            jobs.CURRENT_JOB.reset(job_token)
        if conv:
            conv.pop("pending_job", None)
            store.put(conv)
        chat.ACTIVE.pop(conversation, None)
        STOPS.pop(ident, None)
        from . import user_interaction
        user_interaction.finish_turn(ident)
        with store.LOCK, store.connect() as db:
            current = _decode(db.execute("SELECT * FROM chat_turns WHERE id=?", (ident,)).fetchone())
            current.update({key: value[key] for key in ("message_id", "user_message_id", "job_id") if key in value})
            current.update(status=outcome, ended=time.time(), error=failure)
            if current.get("user_question", {}).get("status") == "pending":
                current["user_question"].update(status="skipped", answer="")
            for item in current.get("steering", []):
                if item.get("status") == "pending":
                    item["status"] = "not_applied"
            # Raw text snapshots are only necessary while awaiting execution.
            current["attachment_snapshots"] = [{**a, "text": ""} for a in current.get("attachment_snapshots", [])]
            _write(db, current)
            if outcome != "done":
                _pause(db, conversation, failure or "上一轮未完成，请确认是否继续")
        emit(ident, {"type": "done", "cancelled": outcome in ("cancelled", "interrupted"), "conversation": store.window(store.get(conversation))})
        emit(ident, {"type": "turn_state", "turn": public(current)})
        channel["conv"] = None
        _trim()


async def events(conversation, ident, after=0):
    get(ident, conversation)
    after = max(0, int(after))
    channel = CHANNELS.get(ident)
    if after == 0 or not channel or (channel["events"] and after < channel["events"][0][0] - 1) or (channel and after > channel["cursor"]):
        cursor = channel["cursor"] if channel else 0
        value = public(get(ident, conversation))
        yield {"type": "resync", "turn_id": ident, "event_id": cursor, "conversation": store.window(store.get(conversation)),
               "live_message": value["live_message"], "turn": value}
        after = cursor
    last_heartbeat = time.monotonic()
    while True:
        channel = CHANNELS.get(ident)
        pending = [event for seq, event, _ in channel["events"] if seq > after] if channel else []
        if channel and channel["cursor"] > after and (not pending or pending[0]["event_id"] > after + 1):
            value = public(get(ident, conversation))
            after = channel["cursor"]
            yield {"type": "resync", "turn_id": ident, "event_id": after, "conversation": store.window(store.get(conversation)),
                   "live_message": value["live_message"], "turn": value}
        else:
            for packet in pending:
                after = packet["event_id"]
                if packet["type"] in ("done", "accepted"):
                    packet = {**packet, "conversation": store.window(store.get(conversation))}
                yield packet
        # Each yield may block on the HTTP consumer while the independent worker
        # appends stats/done and becomes terminal. Drain those newly appended
        # events before honoring terminal state; otherwise the final batch is lost.
        if channel and channel["cursor"] > after:
            continue
        # Do not reparse a multi-MiB attachment snapshot on every 80 ms poll.
        # All live state changes publish turn_state; evicted channels use SQLite.
        status = channel["status"] if channel else get(ident, conversation)["status"]
        if status in TERMINAL:
            if not pending:
                value = get(ident, conversation)
                yield {"type": "done", "turn_id": ident, "event_id": after, "cancelled": value["status"] in ("cancelled", "interrupted"),
                       "conversation": store.window(store.get(conversation)), "turn": public(value)}
            return
        if status == "paused":
            # A paused outbox is restored by list/resume, not a forever-open
            # stream that keeps reloading its attachment snapshot.
            if not pending:
                yield {"type": "turn_state", "turn_id": ident, "event_id": after, "turn": public(get(ident, conversation))}
            return
        if time.monotonic() - last_heartbeat >= 10:
            yield {"type": "heartbeat", "turn_id": ident, "event_id": after}
            last_heartbeat = time.monotonic()
        await asyncio.sleep(.08)


def referenced_attachments():
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM chat_turns WHERE status IN ('queued','running','paused')")
        return {a["id"] for (payload,) in rows for a in json.loads(payload).get("attachment_snapshots", [])}


async def shutdown():
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    stop_all()
    tasks = list(WORKERS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    WORKERS.clear()
