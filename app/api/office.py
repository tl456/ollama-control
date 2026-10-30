"""Office preview adapters; normal server authentication applies."""
import asyncio
from fastapi import APIRouter
from fastapi.responses import FileResponse
from .. import office_operations as office

def build_router(context=None):
    router=APIRouter(tags=["Office预览"])
    @router.get("/api/office/fonts")
    async def fonts():
        return await asyncio.to_thread(office.fonts)
    @router.get("/api/office/previews/{ident}")
    async def preview(ident:str):
        return await asyncio.to_thread(office.preview_info,ident)
    @router.get("/api/office/previews/{ident}/{name}")
    async def resource(ident:str,name:str):
        return FileResponse(await asyncio.to_thread(office.preview_file,ident,name),headers={"Cache-Control":"private, max-age=3600"})
    return router
