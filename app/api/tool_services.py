"""Paged local audit and snapshot routes; no audit deletion endpoint."""
import asyncio
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from .. import audit, snapshots

def build_router(context):
    router=APIRouter(tags=["工具记录"])
    def permitted(request):
        if not context.network.can_manage(request):
            raise HTTPException(403,"此设备没有工作台管理权限")
    @router.get("/api/tools/example-interface")
    async def example_interface(request:Request):
        permitted(request)
        import io, zipfile
        from pathlib import Path
        output=io.BytesIO()
        source=Path(__file__).resolve().parents[2]/"plugins/examples/tool_interface_demo"
        with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(source.glob("*")):
                if file.is_file(): archive.write(file,file.name)
        return Response(output.getvalue(),media_type="application/zip",headers={"Content-Disposition":'attachment; filename="tool_interface_demo.zip"'})
    @router.get("/api/tools/{ident}/export")
    async def export_tool(request:Request,ident:str):
        permitted(request)
        from .. import tools
        blob=await asyncio.to_thread(tools.export_plugin,ident)
        return Response(blob,media_type="application/zip",headers={"Content-Disposition":'attachment; filename="tool.zip"'})
    @router.get("/api/audit")
    async def audit_list(request:Request,page:int=1,page_size:int=30,tool:str="",model:str="",status:str="",since:str="",until:str=""):
        permitted(request)
        return await asyncio.to_thread(audit.listing,page,page_size,tool,model,status,since,until)
    @router.get("/api/snapshots")
    async def snapshot_list(request:Request,page:int=1,page_size:int=30):
        permitted(request)
        return await asyncio.to_thread(snapshots.listing,page,page_size)
    @router.post("/api/snapshots/{ident}/restore")
    async def restore(request:Request,ident:str):
        permitted(request)
        call=audit.start("snapshot_restore",{"action":"restore"})
        try:
            result=await asyncio.to_thread(snapshots.restore,ident)
        except Exception as error:
            audit.finish(call,"snapshot_restore",{},status="error",error_type=type(error).__name__)
            raise
        audit.finish(call,"snapshot_restore",{},result=result)
        return result
    return router
