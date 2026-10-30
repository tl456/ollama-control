"""Roles of managed services; external providers keep their own addresses."""
WEB_PORT = 8765
GATEWAY_PORT = 11436
CHAT_PORT = 11437
EMBED_PORT = 11438
LOOPBACK = "127.0.0.1"
LOCAL_URL = f"http://{LOOPBACK}:{CHAT_PORT}"
EMBED_URL = f"http://{LOOPBACK}:{EMBED_PORT}"
GPU_QUEUE = "local"
RESERVED_PORTS = frozenset((WEB_PORT, CHAT_PORT, EMBED_PORT))


def roles():
    return {"web": {"port": WEB_PORT}, "gateway": {"port": GATEWAY_PORT},
            "chat": {"port": CHAT_PORT, "url": LOCAL_URL}, "embedding": {"port": EMBED_PORT, "url": EMBED_URL}}
