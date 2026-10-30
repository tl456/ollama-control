"""Bounded FIFO per backend, with re-entrant ownership for retrieval before chat."""
from __future__ import annotations
import asyncio
import contextvars
import time
import threading
from contextlib import asynccontextmanager
from .store import uid
from .config import settings

JOBS = {}
LOCKS = {}
OWNER = contextvars.ContextVar("inference_backend", default=None)
CURRENT_JOB = contextvars.ContextVar("inference_job", default=None)
MAINTENANCE = threading.Event()
ACTIVE_STATUSES = ("queued", "running", "waiting_user")


def active(key=None):
    return any(v["status"] in ACTIVE_STATUSES and (key is None or v["key"] == key)
               for v in JOBS.values())


def enqueue(key, label, owner="", kind="chat"):
    if MAINTENANCE.is_set():
        raise ValueError("正在空闲应用设备设置，请稍后再使用推理接口；已有任务不会被打断")
    pending = [v for v in JOBS.values() if v["key"] == key and v["status"] in ACTIVE_STATUSES]
    if len(pending) >= settings().get("queue_limit", 20):
        raise ValueError("该后端队列已满，请等待或取消其他请求")
    job = {"id": uid(), "key": key, "label": label[:100], "owner": owner, "kind": kind,
           "status": "queued", "created": time.time(), "stop": asyncio.Event()}
    JOBS[job["id"]] = job
    # Retain only bounded metadata, never prompts/attachments.
    finished = [v for v in JOBS.values() if v["status"] not in ACTIVE_STATUSES]
    for old in sorted(finished, key=lambda v: v["created"])[:-100]:
        JOBS.pop(old["id"], None)
    return job


def position(job):
    waiting = [v["id"] for v in JOBS.values() if v["key"] == job["key"] and v["status"] == "queued"]
    return waiting.index(job["id"]) + 1 if job["id"] in waiting else 0


async def acquire(job):
    if job.get("_holds_lock"):
        return job["_held_lock"]
    job["status"] = "queued"
    loop = asyncio.get_running_loop()
    # A TestClient or an application restart may have a different event loop.
    for old in [k for k in LOCKS if k[0].is_closed()]:
        LOCKS.pop(old, None)
    lock = LOCKS.setdefault((loop, job["key"]), asyncio.Lock())
    waiter = asyncio.create_task(lock.acquire())
    try:
        while not waiter.done():
            if job["stop"].is_set():
                raise asyncio.CancelledError()
            await asyncio.wait({waiter}, timeout=.15)
        await waiter
        job.update(_held_lock=lock, _holds_lock=True)
        if job["stop"].is_set():
            raise asyncio.CancelledError()
        job.update(status="running", started=time.time())
        return lock
    except BaseException:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if not waiter.cancelled() and waiter.exception() is None and waiter.result():
            # Cancellation raced with acquiring the slot.
            if job.get("_holds_lock"):
                release(job)
            elif lock.locked():
                lock.release()
        job.update(status="cancelled", ended=time.time())
        raise


def release(job):
    if job.pop("_holds_lock", False):
        lock=job.pop("_held_lock", None)
        if lock is not None:
            lock.release()

def suspend(job):
    release(job)
    job["status"]="waiting_user"

def finish(job, lock=None, status="done"):
    # A yielded job must never release a lock now held by another task.
    release(job)
    job.update(status="cancelled" if job["stop"].is_set() else status, ended=time.time())


@asynccontextmanager
async def slot(key, label="后台处理", stop=None, kind="background"):
    if OWNER.get() == key:
        yield None
        return
    job = enqueue(key, label, kind=kind)
    if stop is not None:
        job["stop"] = stop
    lock = await acquire(job)
    token = OWNER.set(key)
    outcome = "done"
    try:
        yield job
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        OWNER.reset(token)
        finish(job, lock, outcome)


def listing(page=1):
    values = sorted(JOBS.values(), key=lambda v: (v["status"] not in ACTIVE_STATUSES, -v["created"]))
    page = max(1, int(page))
    return {"items": [{**{k: v for k, v in item.items() if k != "stop" and not k.startswith("_")}, "position": position(item)}
                      for item in values[(page - 1) * 20:page * 20]], "total": len(values), "page": page, "page_size": 20}
