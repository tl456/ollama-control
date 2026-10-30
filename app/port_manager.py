"""Bounded allocation for this workspace's services; never adopts unrelated listeners."""
from __future__ import annotations
import os
import socket
import sys
import threading
from pathlib import Path
from . import service_config
from .config import ROOT, read_json, write_json

LOCK = threading.RLock()
DEFAULTS = {"web": 8765, "gateway": 11436, "chat": 11437, "embedding": 11438}
NAMES = {"web": "WEB_PORT", "gateway": "GATEWAY_PORT", "chat": "CHAT_PORT", "embedding": "EMBED_PORT"}
CHANGES = []

def current(role):
    return getattr(service_config, NAMES[role])

def set_port(role, port, requested=None):
    port = int(port)
    if not 1024 <= port <= 65535:
        raise ValueError("服务端口须在1024至65535之间")
    setattr(service_config, NAMES[role], port)
    os.environ["LOCAL_AI_" + NAMES[role]] = str(port)
    service_config.LOCAL_URL = f"http://127.0.0.1:{current('chat')}"
    service_config.EMBED_URL = f"http://127.0.0.1:{current('embedding')}"
    service_config.RESERVED_PORTS = frozenset(current(r) for r in ("web", "chat", "embedding"))
    providers = sys.modules.get("app.providers")
    if providers:
        for key in ("CHAT_PORT", "EMBED_PORT", "LOCAL_URL", "EMBED_URL"):
            setattr(providers, key, getattr(service_config, key))
    if requested is not None and requested != port:
        change = {"role": role, "requested": requested, "actual": port}
        if change not in CHANGES:
            CHANGES.append(change)
    write_json(ROOT / "cache/ports.json", {"ports": {k: current(k) for k in NAMES}, "changes": CHANGES})
    return port

def bind_available(role, host, preferred, excluded=()):
    """Keep the returned listening socket open through Uvicorn startup (no probe/bind gap)."""
    preferred = int(preferred)
    if not 1024 <= preferred <= 65535:
        raise ValueError("服务端口须在1024至65535之间")
    reserved = {current(k) for k in NAMES if k != role} | set(excluded)
    for port in range(preferred, min(65535, preferred + 100) + 1):
        if port in reserved:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((host, port))
            sock.listen(128)
            sock.setblocking(False)
            set_port(role, port, preferred)
            return sock, port
        except OSError:
            sock.close()
    raise ValueError(f"{role} 无可用端口：已检查 {preferred} 起最多101个端口")

def owned_port(role):
    import psutil
    name = "embedding-process.json" if role == "embedding" else "ollama-process.json"
    record = read_json(ROOT / "cache" / name, {})
    try:
        process = psutil.Process(record["pid"])
        port = int(record["port"])
        env = process.environ()
        if (abs(process.create_time() - record["created"]) < 1
                and Path(process.exe()).resolve() == Path(record["exe"]).resolve()
                and Path(record["models"]).resolve() == ROOT / "models"
                and Path(env.get("OLLAMA_MODELS", "")).resolve() == ROOT / "models"
                and env.get("OLLAMA_HOST") == f"127.0.0.1:{port}"
                and "serve" in process.cmdline()):
            # Identity is sufficient while the owned process is still starting.
            # Requiring a listener here could spawn a second process on refresh.
            return port
    except (KeyError, ValueError, TypeError, OSError, psutil.Error):
        pass
    return None

def ensure_role(role):
    with LOCK:
        owned = owned_port(role)
        if owned:
            return set_port(role, owned)
        sock, port = bind_available(role, "127.0.0.1", current(role))
        sock.close()  # Ollama has no inherited-listener interface; launch rechecks ownership.
        return port

def prepare_managed():
    for role in ("chat", "embedding"):
        ensure_role(role)
    return status()

def status():
    return {"ports": {k: current(k) for k in NAMES}, "changes": list(CHANGES)}
