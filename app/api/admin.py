"""Settings, runtime and local administration HTTP adapters.

Authentication/host-only enforcement remains in the one server middleware.
Adapters deliberately reference the injected facade at request time so lifecycle
and service state have one authority, including in existing integrations/tests.
"""
from __future__ import annotations
import asyncio
from fastapi import APIRouter, Request


def build_router(context):
    router = APIRouter(tags=["工作台管理"])

    @router.get("/api/auth/status")
    async def auth_status(request: Request):
        return context.network.status(request)

    @router.post("/api/auth/login")
    async def login(request: Request, body: dict):
        return await asyncio.to_thread(context.network.login, request, body.get("password", ""), bool(body.get("remember")))

    @router.post("/api/auth/logout")
    async def logout(request: Request):
        return context.network.logout(request)

    @router.post("/api/auth/password")
    async def password(body: dict):
        return await asyncio.to_thread(context.network.set_password, str(body.get("password", "")))

    @router.post("/api/auth/gateway-key")
    async def gateway_key():
        return context.network.rotate_gateway_key()

    @router.get("/api/access")
    async def access_logs(page: int = 1, ip: str = "", path: str = ""):
        return await asyncio.to_thread(context.network.access_logs, page, ip, path)

    @router.delete("/api/access")
    async def clear_access():
        return context.network.clear_logs()

    @router.get("/api/settings")
    async def get_settings():
        return context.public_settings()

    @router.get("/api/runtime")
    async def runtime_status(provider: str = "local"):
        try:
            if provider != "local":
                if context.providers.is_ollama(provider):
                    async with context.providers.ollama_client(provider) as client:
                        response = await client.get("api/ps")
                        response.raise_for_status()
                        return {"online": True, "loaded": response.json().get("models", []), "hardware": {}, "remote": True}
                return {"online": True, "loaded": [], "hardware": {}, "cloud": True}
            loaded, hardware = await asyncio.gather(context.providers.local("GET", "/api/ps"), asyncio.to_thread(context.providers.hardware))
            return {"online": True, "loaded": loaded.get("models", []), "hardware": hardware}
        except Exception:
            return {"online": False, "loaded": [], "hardware": {}}

    @router.post("/api/runtime/monitor-resume")
    async def resume_gpu_monitor():
        return await asyncio.to_thread(context.resources.reset_monitor)

    @router.put("/api/settings")
    async def update_settings(body: dict, request: Request):
        if not context.network.is_local(request) and "remote_full_access" in body:
            raise ValueError("远程完整控制授权只可在宿主机更改")
        if (context.chat.ACTIVE or context.jobs.active() or context.knowledge.RUNNING or context.FILE_TASKS or context.python_lab.active_ids()) and not set(body).issubset({"memory_enabled", "memory_ai_write"}):
            raise ValueError("请先等待或取消聊天、共享接口和知识库任务，再更改全局设置")
        before = context.settings()
        if body.get("gateway_enabled", before["gateway_enabled"]) and body.get("gateway_require_key", before["gateway_require_key"]) and not context.network.auth_config().get("gateway_key"):
            raise ValueError("请先生成共享 API 密钥，再启用要求鉴权的网关")
        value = await asyncio.to_thread(context.save_settings, body)
        if any(before.get(k) != value.get(k) for k in ("inference_device", "embedding_device")):
            try:
                await asyncio.to_thread(context.providers.apply_device_preferences, before, value)
            except Exception as error:
                await asyncio.to_thread(context.save_settings, before)
                raise ValueError("设备设置未应用，已保留原设置：" + type(error).__name__) from error
        if any(before[k] != value[k] for k in ("gateway_enabled", "gateway_bind", "gateway_port", "gateway_require_key")):
            await context.network.apply_gateway(context.background)
        return value

    @router.post("/api/maintenance/cleanup")
    async def manual_cleanup():
        if context.chat.ACTIVE or context.FILE_TASKS or context.python_lab.active_ids():
            raise ValueError("请等待当前任务完成再清理")
        return await asyncio.to_thread(context.lifecycle.cleanup)

    @router.post("/api/factory-reset")
    async def factory_reset(body: dict):
        if context.chat.ACTIVE or context.turn_queue.busy(include_paused=False) or context.FILE_TASKS or context.knowledge.RUNNING or context.python_lab.active_ids() or any(j["status"] in ("queued", "running") for j in context.jobs.JOBS.values()) or any(not d.get("done") for d in context.providers.DOWNLOADS.values()):
            raise ValueError("请先停止聊天和下载任务")
        if body.get("confirm") != "初始化":
            raise ValueError("请输入 初始化 确认")
        with context.tools.maintenance():
            result = await asyncio.to_thread(context.lifecycle.reset_managed)
        context.turn_queue.recover()
        context.jobs.JOBS.clear()
        context.providers.DOWNLOADS.clear()
        context.providers.CAPS.clear()
        context.resources.FAULTS.clear()
        context.resources.reset_monitor()
        context.knowledge.DIRTY.clear()
        await context.network.apply_gateway(context.background)
        context.background(context.providers.first_start())
        return {**result, "settings": context.public_settings()}

    return router
