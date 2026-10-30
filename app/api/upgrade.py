"""Observable retrieval preparation and redacted runtime diagnostics."""
from __future__ import annotations
import asyncio
import re
from datetime import datetime
from fastapi import APIRouter, Request
from .. import rag_query


def build_router(context):
    router = APIRouter(tags=["检索与诊断"])

    @router.get("/api/rag-query/defaults")
    async def query_defaults():
        return rag_query.defaults()

    @router.post("/api/rag-query/test")
    async def test_query(body: dict, request: Request):
        if not context.network.can_manage(request):
            raise ValueError("检索提示词测试需要管理权限")
        text = body.get("query", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError("请输入非空检索问题，最多12000字符")
        cfg = context.settings()
        opts = {**cfg, "provider": body.get("provider", cfg["provider"]), "model": body.get("model", cfg["default_model"])}
        async with context.jobs.slot(context.providers.queue_key(opts["provider"]), "检索关键词测试") as job:
            info = await rag_query.prepare(text, opts, prompt=body.get("prompt"), force=True,
                                           stop=job["stop"] if job is not None else None)
        return {"query_info": info}

    @router.get("/api/diagnostics")
    async def diagnostics(request: Request):
        if not context.network.can_manage(request):
            raise ValueError("诊断信息需要管理权限")
        from ..service_config import WEB_PORT, GATEWAY_PORT, CHAT_PORT, EMBED_PORT
        cfg = context.settings()
        result = {"version": context.VERSION, "sampled_at": datetime.now().astimezone().isoformat(),
                  "ports": {"web": WEB_PORT, "gateway": cfg.get("gateway_port", GATEWAY_PORT), "chat": CHAT_PORT, "embedding": EMBED_PORT},
                  "network": {k: cfg.get(k) for k in ("web_bind", "gateway_enabled", "gateway_bind", "gateway_require_key", "remote_full_access")},
                  "device_preferences": {k: cfg.get(k) for k in ("inference_device", "embedding_device")},
                  "hardware": await asyncio.to_thread(context.resources.hardware),
                  "queue": context.jobs.listing(), "index_tasks": context.knowledge.tasks(), "redacted": True}
        try:
            result["ollama"] = await context.providers.service_status()
            version, models, loaded = await asyncio.gather(context.providers.local("GET", "/api/version"), context.providers.models("local"), context.providers.local("GET", "/api/ps"))
            result.update(ollama_version=version.get("version"), models=models.get("models", []), loaded=loaded.get("models", []))
        except Exception as error:
            result["ollama_error"] = "后端未连接（" + type(error).__name__ + "）"
        if hasattr(context.providers, "embedding_status"):
            result["embedding"] = context.providers.embedding_status()
        # Copy/export omits prompt-like job labels, owner IDs and any secrets.
        for group in (result.get("queue", {}), result.get("index_tasks", {})):
            for item in group.get("items", []):
                for key in ("label", "owner", "prompt", "text"):
                    item.pop(key, None)
        def scrub(value):
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items() if not any(s in k.casefold() for s in ("secret", "password", "token", "api_key", "authorization", "cookie"))}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, str):
                return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[密钥已隐藏]", value)
            return value
        return scrub(result)

    return router
