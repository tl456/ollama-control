"""Explicit current-turn interaction endpoints; session auth follows existing UI policy."""
from fastapi import APIRouter, Request, HTTPException
from .. import user_interaction

def build_router(context):
    router=APIRouter(tags=["本轮交互"])
    def permitted(request,conversation,turn):
        value=context.turn_queue.get(turn,conversation)
        if not request.state.can_manage and not value.get("restricted",value.get("remote",False)):
            raise HTTPException(403,"当前设备只能调整受限聊天任务，不能借用宿主机任务的管理权限")
    @router.post("/api/chat/{conversation}/turns/{turn}/answer")
    async def answer(conversation:str,turn:str,body:dict,request:Request):
        permitted(request,conversation,turn)
        return user_interaction.answer(conversation,turn,body.get("question_id"),body.get("answer",""),body.get("skip",False))
    @router.post("/api/chat/{conversation}/turns/{turn}/steer")
    async def steer(conversation:str,turn:str,body:dict,request:Request):
        permitted(request,conversation,turn)
        return user_interaction.steer(conversation,turn,body.get("content"),body.get("client_request_id"))
    return router
