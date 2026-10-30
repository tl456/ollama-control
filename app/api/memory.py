"""The single shared Markdown memory's small HTTP adapter."""
from __future__ import annotations
import asyncio
from fastapi import APIRouter


def build_router(context):
    router = APIRouter(tags=["长期记忆"])

    @router.get("/api/memory")
    async def read_memory():
        return await asyncio.to_thread(context.memory.read)

    @router.put("/api/memory")
    async def edit_memory(body: dict):
        if not isinstance(body.get("text"), str):
            raise ValueError("请提供 text 字符串；明确提交空字符串才会清空记忆")
        # Revision remains accepted for older clients, never a write gate. AI and
        # manual UI share the same locked atomic service implementation.
        return await asyncio.to_thread(context.memory.update, body["text"],
                                       body.get("revision"), mode=body.get("mode", "replace"))

    return router
