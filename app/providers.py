from __future__ import annotations
from contextvars import ContextVar
import copy

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import contextlib
import hashlib
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import psutil

from .config import (ROOT, proxy, secret, settings, read_json, write_json,
                     REASONING_EFFORT_LEVELS, DEEPSEEK_EFFORT_LEVELS,
                     normalize_reasoning_effort, normalize_reasoning_levels)
from .service_config import CHAT_PORT, EMBED_PORT, LOCAL_URL, EMBED_URL, GPU_QUEUE

USAGE_SINK = ContextVar("model_usage_sink", default=None)


def record_usage(usage, record=None):
    sink = USAGE_SINK.get()
    if sink is None or not isinstance(usage, dict):
        return record
    if record is None:
        record = {}
        sink.append(record)
    record.update(usage)
    return record


def usage_totals(records):
    def number(record, key):
        value = record.get(key, 0)
        return max(0, int(value)) if isinstance(value, (int, float)) else 0
    cached = sum(number(r, "prompt_cache_hit_tokens") if "prompt_cache_hit_tokens" in r
                 else number(r.get("prompt_tokens_details") or {}, "cached_tokens") for r in records)
    inputs = sum(number(r, "prompt_tokens") for r in records)
    return {"input_tokens": inputs, "output_tokens": sum(number(r, "completion_tokens") for r in records),
            "cached_input_tokens": cached, "cache_miss_tokens": max(0, inputs - cached),
            "model_requests": len(records), "source": "云服务返回用量合计（包括已返回用量的检索、摘要和工具决策请求）"}

POOLS = {}
POOL_USERS = {}
CAPS = {}
EMBED_LOCK = threading.Lock()
SERVICE_LOCK = threading.RLock()


@contextlib.asynccontextmanager
async def pooled_client(base_url, timeout=20, key="", outbound_proxy=None):
    loop = asyncio.get_running_loop()
    identity = (loop, base_url, timeout, hashlib.sha256(key.encode()).hexdigest(), outbound_proxy)
    client = POOLS.get(identity)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", timeout=httpx.Timeout(timeout, connect=5),
                                  headers={"Authorization": "Bearer " + key} if key else {},
                                  proxy=outbound_proxy, trust_env=False, follow_redirects=False,
                                  limits=httpx.Limits(max_connections=12, max_keepalive_connections=6, keepalive_expiry=60))
        POOLS[identity] = client
    POOL_USERS[identity] = POOL_USERS.get(identity, 0) + 1
    # Bounded pool generations when users edit endpoints or credentials.
    for old in list(POOLS):
        if old != identity and not POOL_USERS.get(old) and (old[0].is_closed() or (len(POOLS) > 16 and old[0] is loop)):
            removed = POOLS.pop(old)
            POOL_USERS.pop(old, None)
            if old[0] is loop:
                await removed.aclose()
    try:
        yield client
    finally:
        POOL_USERS[identity] = max(0, POOL_USERS.get(identity, 1) - 1)


async def close_clients():
    loop = asyncio.get_running_loop()
    for key in list(POOLS):
        if key[0] is loop:
            with contextlib.suppress(Exception):
                await POOLS.pop(key).aclose()
            POOL_USERS.pop(key, None)


def local_client(timeout=20):
    return pooled_client(LOCAL_URL, timeout)


def is_ollama(ident):
    return ident == "local" or provider(ident).get("kind") == "ollama"


def queue_key(ident):
    cfg = provider(ident)
    if ident == "local":
        return "local"
    parsed = urlsplit(cfg["base_url"])
    if cfg.get("kind") == "ollama" and parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port == CHAT_PORT:
        return "local"
    return cfg.get("kind", "cloud") + ":" + cfg["base_url"].rstrip("/").lower()


def ollama_client(ident="local", timeout=20):
    if ident == "local":
        return local_client(timeout)
    cfg = provider(ident)
    if cfg.get("kind") != "ollama":
        raise ValueError("该连接不是原生 Ollama")
    return pooled_client(cfg["base_url"], timeout, secret(ident))


async def local(method, endpoint, **kwargs):
    async with local_client() as client:
        response = await client.request(method, endpoint, **kwargs)
        if response.is_error:
            raise ValueError(f"Ollama {response.status_code}: " + response.text[:300])
        return response.json() if response.content else {}


def provider(ident):
    if ident == "local":
        return {"id": "local", "kind": "local", "name": "本地 Ollama", "base_url": LOCAL_URL, "vision": True, "tools": True}
    value = next((p for p in settings()["providers"] if p["id"] == ident), None)
    if not value:
        raise ValueError("服务商不存在")
    return value


def cloud_client(ident, timeout=180):
    cfg = provider(ident)
    api_key = secret(ident)
    return pooled_client(cfg["base_url"], timeout, api_key, proxy() or None)


async def models(ident):
    if ident == "local":
        return await local("GET", "/api/tags")
    if is_ollama(ident):
        async with ollama_client(ident, 30) as client:
            response = await client.get("api/tags")
            response.raise_for_status()
            return response.json()
    async with cloud_client(ident, 30) as client:
        response = await client.get("models")
        if response.is_error:
            raise ValueError(f"云服务连接失败：HTTP {response.status_code}，检查地址、API Key 和服务商权限")
        return {"models": [{"name": item["id"], "size": None} for item in response.json().get("data", [])]}


async def capability(ident, model):
    if not is_ollama(ident):
        cfg = provider(ident)
        return {"vision": cfg.get("vision", False), "tools": cfg.get("tools", True),
                "max_context": int(cfg.get("context", 32768)), **reasoning_profile(cfg)}
    identity = (ident, model, provider(ident)["base_url"])
    cached = CAPS.get(identity)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    if ident == "local":
        info = await local("POST", "/api/show", json={"model": model})
    else:
        async with ollama_client(ident) as client:
            response = await client.post("api/show", json={"model": model})
            response.raise_for_status()
            info = response.json()
    if info.get("remote_model") or info.get("remote_host"):
        raise ValueError("这是 Ollama 转发的云模型，不能标为本地运行。请使用已明确提示外发的 OpenAI 兼容云连接。")
    caps = info.get("capabilities", [])
    lengths = [v for k, v in info.get("model_info", {}).items() if k.endswith(".context_length") and isinstance(v, int)]
    result = {"vision": "vision" in caps, "tools": "tools" in caps, "thinking": "thinking" in caps,
              "embedding": "embedding" in caps, "max_context": max(lengths, default=4096), "context_verified": bool(lengths), "details": info}
    CAPS[identity] = (time.monotonic(), result)
    if len(CAPS) > 64:
        CAPS.pop(next(iter(CAPS)))
    return result


async def resilient_stream(ident, body, stop):
    """Never change CPU/GPU allocation; optional connection-only retry before output."""
    attempts = 2 if ident == "local" and settings()["auto_recovery"] else 1
    for attempt in range(attempts):
        if stop.is_set():
            raise asyncio.CancelledError()
        if queue_key(ident) == "local":
            from . import resources
            resources.check_fault("local")
            resources.admit(body.get("options", {}).get("num_ctx", 8192), allow_large=settings()["allow_large_context"])
        if attempt:
            yield {"recovery": "尚未输出且建连失败：仅重连一次，原配置不变，不切 CPU/GPU，不重放工具", "reset_attempt": False}
        emitted = False
        try:
            async for packet in stream_model(ident, body):
                if stop.is_set():
                    raise asyncio.CancelledError()
                message = packet.get("message", {})
                emitted = emitted or bool(message.get("content") or message.get("thinking") or message.get("tool_calls"))
                yield packet
            return
        except (ValueError, httpx.HTTPError) as error:
            if queue_key(ident) == "local":
                resources.fault("local", error)
            if attempt == attempts - 1 or emitted or not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
                raise


async def stream_model(ident, body):
    if is_ollama(ident):
        body = local_device_body(ident, body)
        if ident == "local":
            body = {**body, "keep_alive": settings().get("ollama_keep_alive", 300)}
        if queue_key(ident) == "local":
            from . import resources
            resources.check_fault("local")
            resources.admit(body.get("options", {}).get("num_ctx", 8192), allow_large=settings()["allow_large_context"])
        # Includes summaries: never send attachment text to an Ollama cloud alias
        # while the UI labels the provider as local.
        await capability(ident, body["model"])
        async with ollama_client(ident, 600) as client:
            completed = False
            async with client.stream("POST", "api/chat", json={**body, "stream": True}) as response:
                if response.is_error:
                    raise ValueError("Ollama: " + (await response.aread()).decode()[:400])
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    packet = json.loads(line)
                    if packet.get("error"):
                        raise ValueError(packet["error"])
                    completed = completed or bool(packet.get("done"))
                    yield packet
            if not completed:
                raise ValueError("Ollama 流在完成标记前结束，保留已有回答，不自动重放")
        return
    cfg = provider(ident)
    request = {"model": body["model"], "messages": cloud_messages(body["messages"], reasoning=cfg.get("thinking_mode") == "deepseek"),
               "stream": True, "stream_options": {"include_usage": True},
               "max_tokens": body.get("options", {}).get("num_predict", 2048)}
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed"):
        if key in body.get("options", {}):
            request[key] = body["options"][key]
    if body.get("tools"):
        request["tools"] = body["tools"]
    cfg = provider(ident)
    if cfg.get("thinking_mode") == "deepseek":
        effort = reasoning_effort(cfg, body)
        enabled = bool(body.get("think")) and effort != "off"
        request["thinking"] = {"type": "enabled" if enabled else "disabled"}
        if enabled:
            request["reasoning_effort"] = effort
            for key in ("temperature", "presence_penalty", "frequency_penalty"):
                request.pop(key, None)
    elif cfg.get("reasoning_effort_supported") and body.get("think"):
        effort = reasoning_effort(cfg, body)
        if effort != "off":
            request["reasoning_effort"] = effort
    async with cloud_client(ident, 600) as client:
        usage_record = None
        async with client.stream("POST", "chat/completions", json=request) as response:
            if response.is_error:
                # Do not include upstream request dumps / credentials in UI errors.
                raise ValueError(f"云模型请求失败 HTTP {response.status_code}，请检查模型名、余额、参数和服务商兼容范围")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                item = json.loads(data)
                if item.get("error"):
                    raise ValueError("云模型返回错误，请检查服务商控制台")
                delta = (item.get("choices") or [{}])[0].get("delta", {})
                usage_record = record_usage(item.get("usage"), usage_record)
                yield {"message": {"content": delta.get("content") or "", "thinking": delta.get("reasoning_content") or "",
                                   "tool_calls": delta.get("tool_calls", [])}, "usage": item.get("usage"), "cloud": True}


def cloud_messages(messages, reasoning=False):
    result = []
    for message in messages:
        m = copy.deepcopy({k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "tool_call_id", "reasoning_content")})
        for call in m.get("tool_calls", []):
            arguments = call.get("function", {}).get("arguments")
            if isinstance(arguments, dict):
                call["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        if not reasoning:
            m.pop("reasoning_content", None)
        if reasoning and message.get("thinking"):
            m["reasoning_content"] = message["thinking"]
        if message.get("images"):
            m["content"] = [{"type": "text", "text": message["content"]}] + [
                {"type": "image_url", "image_url": {"url": ("data:image/png;base64," if im.startswith("iVBOR") else "data:image/jpeg;base64,") + im}} for im in message["images"]]
        result.append(m)
    return result


def reasoning_profile(cfg, metadata=None):
    """Explicit UI capability only; optional model metadata overrides the same keys.

    Old compatible connections without a level declaration remain usable on the
    backend, but cannot advertise guessed slider positions to the UI.
    """
    effective = dict(cfg)
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("模型思考能力需要对象")
        effective.update({key: metadata[key] for key in
            ("reasoning_effort_supported", "reasoning_effort_levels", "reasoning_effort") if key in metadata})
    deepseek = effective.get("thinking_mode") == "deepseek"
    declared = "reasoning_effort_levels" in effective
    levels = normalize_reasoning_levels(effective["reasoning_effort_levels"], deepseek=deepseek) if declared else list(DEEPSEEK_EFFORT_LEVELS) if deepseek else []
    supported = bool(deepseek or (effective.get("reasoning_effort_supported") is True and declared))
    if metadata is not None and metadata.get("reasoning_effort_supported") is False:
        supported = False
    default = normalize_reasoning_effort(effective.get("reasoning_effort", "high"))
    if supported and default not in levels:
        raise ValueError("模型默认思考深度不在已声明档位中")
    return {"reasoning_effort_supported": supported, "reasoning_effort_levels": levels if supported else [],
            "reasoning_effort": default}


def reasoning_effort(cfg, body):
    effort = body.get("reasoning_effort")
    if effort is None:
        effort = body.get("options", {}).get("reasoning_effort", cfg.get("reasoning_effort", "high"))
    if effort in (None, "inherit"):
        effort = cfg.get("reasoning_effort", "high")
    effort = normalize_reasoning_effort(effort)
    if cfg.get("thinking_mode") == "deepseek":
        if effort == "off":
            # Display-level subsets do not remove DeepSeek's protocol-level
            # disabled mode, used by keyword extraction and other auxiliaries.
            return effort
        allowed = normalize_reasoning_levels(cfg.get("reasoning_effort_levels", list(DEEPSEEK_EFFORT_LEVELS)), deepseek=True)
    elif "reasoning_effort_levels" in cfg:
        allowed = normalize_reasoning_levels(cfg["reasoning_effort_levels"])
    else:
        allowed = REASONING_EFFORT_LEVELS  # Preserve legacy supported connections, including medium.
    if effort not in allowed:
        raise ValueError("当前连接未声明支持所选思考深度：" + effort)
    return effort


def local_device_body(ident, body):
    options = dict(body.get("options", {}))
    device = body.get("inference_device", options.get("inference_device", settings().get("inference_device", "auto")))
    if device not in ("auto", "cpu"):
        raise ValueError("聊天设备需要选择 auto 或 cpu")
    # Application-only metadata must not leak into Ollama's options schema.
    private = ("inference_device", "reasoning_effort", "reasoning_effort_supported", "reasoning_effort_levels")
    for key in private:
        options.pop(key, None)
    if device == "cpu" and queue_key(ident) == GPU_QUEUE:
        options["num_gpu"] = 0
    return {k: v for k, v in {**body, "options": options}.items() if k not in private}


async def decision(ident, body):
    if is_ollama(ident):
        body = local_device_body(ident, body)
        if queue_key(ident) == "local":
            from . import resources
            resources.check_fault("local")
            resources.admit(body.get("options", {}).get("num_ctx", 8192), allow_large=settings()["allow_large_context"])
        async with ollama_client(ident, 300) as client:
            request = {k: v for k, v in body.items() if k != "decision_schema"}
            if ident == "local":
                request["keep_alive"] = settings().get("ollama_keep_alive", 300)
            response = await client.post("api/chat", json={**request, "stream": False, "format": body.get("decision_schema", "json"), "think": False})
            data = response.json()
            if response.is_error or data.get("error"):
                error = ValueError("Ollama 工具决策失败：" + str(data.get("error", response.status_code))[:300])
                if queue_key(ident) == "local":
                    resources.fault("local", error)
                raise error
            return data["message"]["content"]
    request = {"model": body["model"], "messages": cloud_messages(body["messages"], reasoning=provider(ident).get("thinking_mode") == "deepseek"), "stream": False,
               "temperature": 0, "max_tokens": 512}
    # Prompt protocol works without assuming every compatible provider implements JSON mode.
    async with cloud_client(ident, 300) as client:
        response = await client.post("chat/completions", json=request)
        if response.is_error:
            raise ValueError(f"工具决策失败 HTTP {response.status_code}")
        data = response.json()
        record_usage(data.get("usage"))
        return data["choices"][0]["message"]["content"]


def parse_model(text):
    text = text.strip()
    if text.startswith("ollama run ") or text.startswith("ollama pull "):
        text = text.split()[-1]
    if "://" in text:
        url = urlsplit(text)
        if url.scheme != "https" or url.hostname != "ollama.com" or url.query or url.fragment:
            raise ValueError("只接受 https://ollama.com/library/ 官方模型页面")
        if not url.path.startswith("/library/"):
            raise ValueError("请粘贴官方 /library/ 模型详情链接")
        text = unquote(url.path[len("/library/"):]).rstrip("/")
    if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", text):
        raise ValueError("模型名无效，例如 qwen3.5:4b")
    return text


DOWNLOADS = {}


async def pull_task(ident, name):
    job = DOWNLOADS[ident]
    layers = {}
    started = time.monotonic()
    try:
        async with local_client(1800) as client:
            async with client.stream("POST", "/api/pull", json={"model": name, "stream": True}) as response:
                if response.is_error:
                    raise ValueError((await response.aread()).decode()[:300])
                async for line in response.aiter_lines():
                    if job.get("cancel"):
                        job["status"] = "已取消，可重新下载续传"
                        break
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise ValueError(event["error"])
                    if event.get("digest"):
                        layers[event["digest"]] = {"completed": event.get("completed", 0), "total": event.get("total", 0)}
                    total = sum(v["total"] for v in layers.values())
                    completed = sum(v["completed"] for v in layers.values())
                    elapsed = max(1, time.monotonic() - started)
                    speed = completed / elapsed
                    job.update(status=event.get("status"), total=total, completed=completed, speed=speed,
                               eta=(total - completed) / speed if speed else None, layers=list(layers.values()))
    except Exception as error:
        job.update(status="失败", error=str(error)[:400])
    finally:
        job["done"] = True


def ollama_exe():
    # Installed releases prefer their verified runtime. Source checkouts may use
    # an already-installed ASCII-path official CLI without starting its GUI.
    bundled = str(ROOT / "runtime/ollama/ollama.exe")
    external = [shutil.which("ollama"), str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"), r"C:\Program Files\Ollama\ollama.exe"]
    import sys
    candidates = [bundled, *external] if getattr(sys, "frozen", False) else [*external, bundled]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def service_ownership():
    record = read_json(ROOT / "cache/ollama-process.json", {})
    try:
        process = psutil.Process(record["pid"])
        if Path(process.exe()).resolve() != Path(record["exe"]).resolve() or "serve" not in process.cmdline():
            return {"managed": False, "models_path": None}
        if record.get("created") and abs(process.create_time() - record["created"]) > 1:
            return {"managed": False, "models_path": None}
        if (Path(record.get("models", "")).resolve() != ROOT / "models" or
                Path(process.environ().get("OLLAMA_MODELS", "")).resolve() != ROOT / "models" or
                process.environ().get("OLLAMA_HOST") != f"127.0.0.1:{CHAT_PORT}"):
            return {"managed": False, "models_path": None}
        listening = any(c.status == psutil.CONN_LISTEN and c.laddr.port == CHAT_PORT for c in process.net_connections(kind="inet"))
        return {"managed": listening, "models_path": record["models"] if listening else None,
                "unicode_runtime_path": not str(record["exe"]).isascii() if listening else False}
    except (KeyError, OSError, psutil.Error):
        return {"managed": False, "models_path": None}


def launch_ollama(embedding=False, device=None):
    with SERVICE_LOCK:
        return _launch_ollama(embedding, device)


def _launch_ollama(embedding=False, device=None):
    if embedding or settings().get("ollama_management", "managed") == "managed":
        from .port_manager import ensure_role, owned_port
        role = "embedding" if embedding else "chat"
        ensure_role(role)
        if owned_port(role):
            return read_json(ROOT / "cache" / ("embedding-process.json" if embedding else "ollama-process.json"), {})
    if not embedding:
        try:
            response = httpx.get(LOCAL_URL + "/api/version", timeout=1, trust_env=False)
            response.raise_for_status()
            if service_ownership()["managed"]:
                return read_json(ROOT / "cache/ollama-process.json", {})
            raise ValueError("主端口已由外部服务使用，请先退出官方托盘，或选择连接已有服务；未强行接管")
        except httpx.HTTPError:
            pass
        if settings().get("ollama_management", "managed") != "managed":
            raise ValueError("当前设置为连接已有服务；切换为工作台管理后才能启动")
    exe = ollama_exe()
    if not exe:
        raise ValueError("未找到 Ollama，请先安装官方 Windows 版本")
    device = device or (settings().get("embedding_device", "cpu") if embedding else settings().get("inference_device", "auto"))
    if device not in (("cpu", "gpu") if embedding else ("auto", "cpu")):
        raise ValueError("无效的设备偏好，未启动进程")
    port = EMBED_PORT if embedding else CHAT_PORT
    env = {**os.environ, "OLLAMA_MODELS": str(ROOT / "models"), "OLLAMA_HOST": f"127.0.0.1:{port}", "OLLAMA_NO_CLOUD": "true",
           "OLLAMA_CONTEXT_LENGTH": "2048" if embedding else "8192", "OLLAMA_MAX_QUEUE": "4",
           "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1"}
    if embedding and device == "cpu":
        env.update(CUDA_VISIBLE_DEVICES="-1", ROCR_VISIBLE_DEVICES="-1", GGML_VK_VISIBLE_DEVICES="-1")
    env.pop("HTTP_PROXY", None)
    env.pop("http_proxy", None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    if proxy():
        env["HTTPS_PROXY"] = proxy()
    else:
        env.pop("HTTPS_PROXY", None)
        env.pop("https_proxy", None)
    from .lifecycle import rotate_closed_log, capture_process_log
    log_name = "ollama-embedding.log" if embedding else "ollama.log"
    rotate_closed_log(ROOT / "logs" / log_name)
    child = subprocess.Popen([exe, "serve"], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    capture_process_log(child, ROOT / "logs" / log_name)
    record = {"pid": child.pid, "created": psutil.Process(child.pid).create_time(), "models": env["OLLAMA_MODELS"], "exe": str(Path(exe).resolve()),
              "role": "embedding" if embedding else "chat", "port": port, "endpoint": EMBED_URL if embedding else LOCAL_URL, "device": device}
    write_json(ROOT / "cache" / ("embedding-process.json" if embedding else "ollama-process.json"), record)
    return record


def restart_ollama():
    if not service_ownership()["managed"]:
        raise ValueError("当前主服务不是本工作台自管；未重启外部服务")
    record = read_json(ROOT / "cache/ollama-process.json", {})
    if not record.get("pid"):
        raise ValueError("当前 Ollama 由外部应用管理。请从托盘退出 Ollama，然后在本控制台启动。")
    try:
        process = psutil.Process(record["pid"])
        if Path(process.exe()).resolve() != Path(record["exe"]).resolve() or "serve" not in process.cmdline():
            raise ValueError("进程身份已变化，未进行重启")
        if record.get("created") and abs(process.create_time() - record["created"]) > 1:
            raise ValueError("进程身份已变化，未进行重启")
        stop_owned_service(record)
    except psutil.NoSuchProcess:
        pass
    return launch_ollama()


def stop_local_service():
    from . import jobs
    if jobs.active("local"):
        raise ValueError("请先等待或停止本地任务，再停止后台服务")
    if not service_ownership()["managed"]:
        raise ValueError("主服务由外部程序管理或已停止，未停止外部进程")
    record = read_json(ROOT / "cache/ollama-process.json", {})
    stop_owned_service(record)
    return {"ok": True, "ownership": "offline", "note": "已停止工作台自管主服务，模型文件保留"}


def apply_device_preferences(before, after):
    """Idle-only explicit changes; external engines are never adopted or stopped."""
    from . import jobs
    changed_chat = before.get("inference_device", "auto") != after.get("inference_device", "auto")
    changed_embed = before.get("embedding_device", "cpu") != after.get("embedding_device", "cpu")
    result = {"chat_restarted": False, "embedding_stopped": False, "external_untouched": True}
    if not (changed_chat or changed_embed):
        return {**result, "embedding_status": embedding_status()}
    with SERVICE_LOCK:
        jobs.MAINTENANCE.set()
        try:
            if jobs.active():
                raise ValueError("已有推理或嵌入任务，请等待结束后再应用设备设置")
            if changed_chat and service_ownership()["managed"]:
                restart_ollama()
                result["chat_restarted"] = True
            if changed_embed:
                record = read_json(ROOT / "cache/embedding-process.json", {})
                owned = False
                try:
                    process = psutil.Process(record["pid"])
                    env = process.environ()
                    owned = (abs(process.create_time() - record["created"]) < 1 and
                        Path(process.exe()).resolve() == Path(record["exe"]).resolve() and "serve" in process.cmdline() and
                        Path(record.get("models", "")).resolve() == ROOT / "models" and
                        Path(env.get("OLLAMA_MODELS", "")).resolve() == ROOT / "models" and
                        env.get("OLLAMA_HOST") == f"127.0.0.1:{EMBED_PORT}" and
                        any(c.status == psutil.CONN_LISTEN and c.laddr.port == EMBED_PORT for c in process.net_connections(kind="inet")))
                except (KeyError, OSError, psutil.Error):
                    pass
                if owned:
                    stop_owned_service(record)
                    result["embedding_stopped"] = True
                reset_embedding_device()
        finally:
            jobs.MAINTENANCE.clear()
    return {**result, "embedding_status": embedding_status()}


def start_managed_if_needed():
    cfg = settings()
    if not cfg.get("ollama_autostart", True) or cfg.get("ollama_management", "managed") != "managed":
        return {"started": False, "reason": "按设置不自动启动"}
    from .port_manager import ensure_role
    ensure_role("chat")
    try:
        response = httpx.get(LOCAL_URL + "/api/version", timeout=1, trust_env=False)
        response.raise_for_status()
        return {"started": False, "reason": "已连接自管服务" if service_ownership()["managed"] else "已有外部服务，未接管"}
    except httpx.HTTPError:
        pass
    try:
        record = launch_ollama()
        with httpx.Client(trust_env=False, timeout=.5) as client:
            for _ in range(40):
                try:
                    client.get(LOCAL_URL + "/api/version").raise_for_status()
                    return {"started": True, "process": record}
                except httpx.HTTPError:
                    time.sleep(.15)
        return {"started": True, "ready": False, "process": record, "reason": "后台启动中，请查看服务状态；不会重复创建进程"}
    except Exception as error:
        import logging
        logging.warning("Ollama startup unavailable: %s", type(error).__name__)
        return {"started": False, "reason": str(error)[:250]}


def shutdown_owned_services():
    stop_embedding_service()
    if settings().get("ollama_stop_on_exit", True) and service_ownership()["managed"]:
        try:
            stop_owned_service(read_json(ROOT / "cache/ollama-process.json", {}))
        except (ValueError, OSError, psutil.Error):
            import logging
            logging.exception("Verified local service could not be stopped")


async def service_status():
    try:
        data = await local("GET", "/api/version")
        online, version = True, data.get("version", "")
    except Exception:
        online, version = False, ""
    own = await asyncio.to_thread(service_ownership)
    cfg = settings()
    record = read_json(ROOT / "cache/ollama-process.json", {})
    return {"online": online, "version": version, "endpoint": LOCAL_URL,
            "ownership": "managed" if online and own["managed"] else "external" if online else "offline",
            "pid": record.get("pid") if online and own["managed"] else None,
            "management_mode": cfg.get("ollama_management", "managed"),
            "autostart": cfg.get("ollama_autostart", True), "stop_on_exit": cfg.get("ollama_stop_on_exit", True),
            "keep_alive": cfg.get("ollama_keep_alive", 300), "models_path": own.get("models_path"),
            "requested_device": cfg.get("inference_device", "auto"), "embedding": embedding_status()}


def stop_owned_service(record):
    """Stop only a verified managed server and its verified runner descendants."""
    process = psutil.Process(record["pid"])
    exe = Path(record["exe"]).resolve()
    environment = process.environ()
    port = record.get("port")
    if port is None:
        # Legacy metadata is only accepted if the live process actually uses
        # one of this release's managed role addresses; never touch 11434/35.
        port = next((p for p in (CHAT_PORT, EMBED_PORT) if environment.get("OLLAMA_HOST") == f"127.0.0.1:{p}"), None)
    if (abs(process.create_time() - record["created"]) >= 1 or
            Path(process.exe()).resolve() != exe or "serve" not in process.cmdline() or
            Path(record["models"]).resolve() != ROOT / "models" or
            Path(environment.get("OLLAMA_MODELS", "")).resolve() != ROOT / "models" or
            port not in (CHAT_PORT, EMBED_PORT) or environment.get("OLLAMA_HOST") != f"127.0.0.1:{port}"):
        raise ValueError("无法确认 Ollama 进程归属，未停止外部服务")
    runners = []
    for child in process.children(recursive=True):
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            child_exe = Path(child.exe()).resolve()
            if child_exe.is_relative_to(exe.parent) and any("runner" in v.lower() for v in child.cmdline()):
                runners.append((child.pid, child.create_time(), child_exe))
    process.terminate()
    process.wait(timeout=8)
    for pid, created, child_exe in runners:
        with contextlib.suppress(psutil.NoSuchProcess):
            child = psutil.Process(pid)
            if abs(child.create_time() - created) < 1 and Path(child.exe()).resolve() == child_exe:
                child.terminate()
                child.wait(timeout=5)


def hardware():
    from .resources import hardware as cached_hardware
    return cached_hardware()


async def first_start():
    if read_json(ROOT / "config/first-start.json", {}).get("completed"):
        return
    try:
        listed = await models("local")
        names = {m["name"] for m in listed.get("models", [])}
    except Exception:
        return  # Retry when Ollama becomes available, not falsely mark detection complete.
    hw = await asyncio.to_thread(hardware)
    eligible = any(g["total_mb"] > 10 * 1024 for g in hw["gpus"])
    selected = "qwen3.5:9b" if eligible and "qwen3.5:9b" in names else "qwen3.5:4b"
    reason = "单张独显专用显存大于 10 GiB，优先 9B" if selected.endswith("9b") else "其他情况或无法可靠检测，保守选用 4B"
    if selected not in names:
        return
    reason += "；只读取安装信息，不进行启动试载，发送问题时按需加载"
    from .config import save_settings
    save_settings({"default_model": selected})
    write_json(ROOT / "config/first-start.json", {"completed": True, "selected": selected, "reason": reason,
                                                  "dedicated_threshold_gib": 10, "checked_at": time.time()})


def ensure_embedding_service(device=None):
    from .port_manager import ensure_role
    ensure_role("embedding")
    """One owned endpoint, switching a CPU-only server only for explicit GPU use."""
    from . import resources
    import urllib.request
    device = device or settings().get("embedding_device", "cpu")
    if device not in ("cpu", "gpu"):
        raise ValueError("嵌入设备需要选择 cpu 或 gpu")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with EMBED_LOCK:
        record = read_json(ROOT / "cache/embedding-process.json", {})
        owned = False
        try:
            process = psutil.Process(record["pid"])
            owned = (abs(process.create_time() - record["created"]) < 1 and
                     Path(process.exe()).resolve() == Path(record["exe"]).resolve() and "serve" in process.cmdline() and
                     record.get("models") == str(ROOT / "models") and
                     Path(process.environ().get("OLLAMA_MODELS", "")).resolve() == ROOT / "models" and
                     process.environ().get("OLLAMA_HOST") == f"127.0.0.1:{EMBED_PORT}")
        except (KeyError, OSError, psutil.Error):
            pass
        try:
            with opener.open(EMBED_URL + "/api/version", timeout=1):
                if not owned:
                    raise ValueError(f"{EMBED_PORT} 已被其他服务占用，未借用未知嵌入服务")
                if device == "gpu" and record.get("device", "cpu") != "gpu":
                    stop_owned_service(record)
                    owned = False
                else:
                    return
        except ValueError:
            raise
        except OSError:
            if owned:
                raise ValueError("已有 CPU 嵌入服务未响应，请手动重启控制台；不会循环创建进程")
        resources.check_fault("embedding")
        resources.admit(2048, 1024 ** 3, embedding=device == "cpu")
        launch_ollama(embedding=True, device=device)
        for _ in range(40):
            try:
                with opener.open(EMBED_URL + "/api/version", timeout=.5):
                    return
            except OSError:
                time.sleep(.15)
        raise ValueError("嵌入服务未就绪，已停止本次任务，请查看嵌入日志")


EMBED_STATE_LOCK = threading.RLock()
EMBED_STATE = {"requested_device": "cpu", "used_device": None, "fallback": False, "reason": "", "gpu_paused": False,
               "preference": None, "sampled_at": None}


def embedding_status():
    with EMBED_STATE_LOCK:
        return {key: value for key, value in EMBED_STATE.items() if key != "preference"}


def reset_embedding_device():
    """Called after an explicit idle preference change; no driver/service action."""
    with EMBED_STATE_LOCK:
        EMBED_STATE.update(preference=None, gpu_paused=False, fallback=False, reason="", used_device=None)
    return embedding_status()


def _embedding_state(**values):
    from datetime import datetime
    with EMBED_STATE_LOCK:
        EMBED_STATE.update(values, sampled_at=datetime.now().astimezone().isoformat(timespec="seconds"))


def embedding_gpu_reason(model_bytes):
    """Use only existing cached telemetry; never force inventory or a driver probe."""
    from . import resources
    with resources.LOCK:
        gpus = list(resources.GPU_CACHE.get("items", []))
    reserve_mb = (model_bytes * 1.4 + 1024 ** 3) / 1024 ** 2
    if not any(isinstance(g.get("free_mb"), (int, float)) and g["free_mb"] >= reserve_mb for g in gpus):
        return "无法从现有采样确认足够可用显存，本次使用 CPU；未额外探测显卡"
    try:
        resources.admit(4096, model_bytes, embedding=False)
    except ValueError:
        return "GPU 嵌入预检资源余量不足，本次使用 CPU，资源门禁未放宽"
    return ""


async def _embed_request(model, texts, device, model_bytes):
    from . import resources
    resources.admit(4096, model_bytes, embedding=device == "cpu")
    await asyncio.to_thread(ensure_embedding_service, device)
    options = {"num_ctx": 4096, "num_batch": 64, "num_thread": max(1, min(4, (os.cpu_count() or 2) // 2))}
    if device == "cpu":
        options["num_gpu"] = 0
    async with pooled_client(EMBED_URL, 180) as client:
        response = await client.post("api/embed", json={"model": model, "input": texts, "truncate": False,
            "keep_alive": 0 if device == "gpu" else "2m", "options": options})
        if response.is_error:
            raise ValueError("嵌入请求失败 HTTP " + str(response.status_code))
        data = response.json()
        if data.get("error"):
            raise ValueError("嵌入服务未能完成本次请求")
        vectors = data.get("embeddings", [])
        import math
        if (not isinstance(vectors, list) or len(vectors) != len(texts) or not vectors or
                any(not isinstance(v, list) or not v or len(v) != len(vectors[0]) or
                    any(type(x) not in (float, int) or not math.isfinite(x) for x in v) for v in vectors)):
            raise ValueError("嵌入数量、维度或数值不完整，未建立索引")
        return vectors


async def embed(model, texts):
    from . import resources, jobs
    resources.check_fault("embedding")
    model_bytes = 1024 ** 3
    if model.startswith("qwen3-embedding:"):
        tag = model.split(":", 1)[1]
        if re.fullmatch(r"[A-Za-z0-9_.-]+", tag) and tag not in (".", ".."):
            manifest = read_json(ROOT / "models/manifests/registry.ollama.ai/library/qwen3-embedding" / tag, {})
            model_bytes = sum(layer.get("size", 0) for layer in manifest.get("layers", [])) or model_bytes
    requested = settings().get("embedding_device", "cpu")
    if requested not in ("cpu", "gpu"):
        raise ValueError("嵌入设备需要选择 cpu 或 gpu")
    with EMBED_STATE_LOCK:
        if EMBED_STATE["preference"] != requested:
            EMBED_STATE.update(preference=requested, gpu_paused=False)
        paused = EMBED_STATE["gpu_paused"]
    reason = "GPU 嵌入本次运行已暂停，继续使用 CPU；切换设备偏好或重启后可重新尝试" if paused else ""
    _embedding_state(requested_device=requested, used_device=None, fallback=False, reason=reason)
    if requested == "gpu" and not paused:
        # Order matters: GPU first, endpoint second. Chat/RAG already owns GPU
        # re-entrantly, so indexing cannot hold the endpoint while waiting on it.
        async with jobs.slot(GPU_QUEUE, "GPU 嵌入排队", kind="embedding"):
            # Another queued index may have failed while this call waited. The
            # pre-queue snapshot cannot authorize a second GPU attempt after
            # that failure has paused GPU embedding for this process.
            with EMBED_STATE_LOCK:
                paused_now = EMBED_STATE["gpu_paused"]
            if paused_now:
                reason = "GPU 嵌入本次运行已暂停，继续使用 CPU；切换设备偏好或重启后可重新尝试"
            else:
                async with jobs.slot("embedding", "知识库嵌入", kind="embedding"):
                    reason = embedding_gpu_reason(model_bytes)
                    if not reason:
                        try:
                            vectors = await _embed_request(model, texts, "gpu", model_bytes)
                            _embedding_state(used_device="gpu", fallback=False, reason="")
                            return vectors
                        except (ValueError, OSError, TimeoutError, psutil.Error, httpx.HTTPError):
                            reason = "GPU 嵌入失败，本次仅尝试一次 CPU 兜底；已暂停后续 GPU 尝试，未循环重载"
                            _embedding_state(gpu_paused=True)
    async with jobs.slot("embedding", "知识库嵌入", kind="embedding"):
        _embedding_state(used_device="cpu", fallback=requested == "gpu", reason=reason)
        try:
            vectors = await _embed_request(model, texts, "cpu", model_bytes)
            _embedding_state(used_device="cpu", fallback=requested == "gpu", reason=reason)
            return vectors
        except Exception as error:
            resources.fault("embedding", error)
            raise


def stop_embedding_service():
    record = read_json(ROOT / "cache/embedding-process.json", {})
    try:
        stop_owned_service(record)
    except (KeyError, ValueError, OSError, psutil.Error):
        pass
