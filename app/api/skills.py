"""Text skill catalogue and editing adapters."""
import asyncio
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response
from .. import skills

def build_router(context=None):
    router=APIRouter(tags=["技能中心"])
    @router.get("/api/skills")
    async def listing(page:int|None=None,search:str=""):
        return await asyncio.to_thread(skills.listing,page,search)
    @router.post("/api/skills")
    async def create(body:dict):
        return await asyncio.to_thread(skills.save,body)
    @router.post("/api/skills/import")
    async def import_skill(file:UploadFile=File(...)):
        blob=await file.read(1024*1024+1)
        return await asyncio.to_thread(skills.import_file,file.filename or "skill.md",blob)
    @router.get("/api/skills/{ident}/export")
    async def export(ident:str,format:str="zip"):
        blob,name,mime=await asyncio.to_thread(skills.export,ident,format)
        return Response(blob,media_type=mime,headers={"Content-Disposition":'attachment; filename="'+name+'"'})
    @router.post("/api/skills/{ident}/copy")
    async def duplicate(ident:str):
        return await asyncio.to_thread(skills.duplicate,ident)
    @router.get("/api/skills/{ident}")
    async def detail(ident:str):
        return await asyncio.to_thread(skills.detail,ident)
    @router.put("/api/skills/{ident}")
    async def save(ident:str,body:dict):
        return await asyncio.to_thread(skills.save,body,ident)
    @router.delete("/api/skills/{ident}")
    async def delete(ident:str):
        return await asyncio.to_thread(skills.remove,ident)
    return router
