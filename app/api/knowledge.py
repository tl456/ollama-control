"""Knowledge lifecycle routes; scheduling and indexing stay in the service."""
from __future__ import annotations
import asyncio
from fastapi import APIRouter


def build_router(context):
    router = APIRouter(tags=["知识库"])

    @router.get("/api/knowledge")
    async def libraries(page: int = 1, search: str = "", category: str = ""):
        return await asyncio.to_thread(context.knowledge.listing, page, search, category)

    @router.post("/api/knowledge")
    async def create_library(body: dict):
        return await asyncio.to_thread(context.knowledge.create, body.get("name", ""), body.get("category", ""), body.get("config"))

    @router.get("/api/knowledge/tasks")
    async def knowledge_tasks(page: int = 1, kb: str = ""):
        return await asyncio.to_thread(context.knowledge.tasks, page, kb)

    @router.get("/api/knowledge/{ident}")
    async def library_detail(ident: str):
        return await asyncio.to_thread(context.knowledge.get, ident)

    @router.patch("/api/knowledge/{ident}")
    async def edit_library(ident: str, body: dict):
        return await asyncio.to_thread(context.knowledge.update, ident, body)

    @router.delete("/api/knowledge/{ident}")
    async def remove_library(ident: str, body: dict):
        if body.get("confirm") != context.knowledge.get(ident)["name"]:
            raise ValueError("请输入知识库名称确认删除；外部原文件不会删除")
        return await asyncio.to_thread(context.knowledge.delete, ident)

    @router.get("/api/knowledge/{ident}/documents")
    async def knowledge_documents(ident: str, page: int = 1):
        return await asyncio.to_thread(context.knowledge.documents, ident, page)

    @router.post("/api/knowledge/{ident}/documents")
    async def add_knowledge_document(ident: str, body: dict):
        result = await asyncio.to_thread(context.knowledge.add_document, ident, body["attachment_id"])
        if not result["duplicate"]:
            result["job"] = context.knowledge.schedule(ident, context.background)
        return result

    @router.post("/api/knowledge/{ident}/documents/md")
    async def create_markdown_document(ident: str, body: dict):
        result = await asyncio.to_thread(context.knowledge.create_markdown, ident, body.get("name", ""), body.get("text", ""))
        result["job"] = context.knowledge.schedule(ident, context.background)
        return result

    @router.get("/api/knowledge/{ident}/documents/{doc}/source")
    async def get_document_source(ident: str, doc: str):
        return await asyncio.to_thread(context.knowledge.document_source, ident, doc)

    @router.put("/api/knowledge/{ident}/documents/{doc}/source")
    async def edit_document_source(ident: str, doc: str, body: dict):
        result = await asyncio.to_thread(context.knowledge.update_document_source, ident, doc, body)
        result["job"] = context.knowledge.schedule(ident, context.background)
        return result

    @router.delete("/api/knowledge/{ident}/documents/{doc}")
    async def remove_knowledge_document(ident: str, doc: str):
        result = await asyncio.to_thread(context.knowledge.delete_document, ident, doc)
        result["job"] = context.knowledge.schedule(ident, context.background)
        return result

    @router.post("/api/knowledge/{ident}/rebuild")
    async def rebuild_knowledge(ident: str):
        return context.knowledge.schedule(ident, context.background)

    @router.post("/api/knowledge/{ident}/cancel")
    async def cancel_knowledge(ident: str):
        return context.knowledge.cancel(ident)

    @router.post("/api/knowledge-search")
    async def search_knowledge(body: dict):
        return await context.knowledge.search(body.get("query", ""), body.get("ids", []))

    return router
