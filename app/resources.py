"""Cheap OS memory telemetry; optional NVIDIA sampling is cached/circuit limited."""
from __future__ import annotations
import ctypes
import os
import shutil
import subprocess
import threading
import time
import csv
import io
from datetime import datetime
from .config import ROOT, settings
import psutil

LOCK = threading.Lock()
GPU_CACHE = {"items": [], "checked": 0.0, "next": 0.0, "error": "",
             "failures": 0, "paused": False, "status": "waiting", "sampled_at": None}
GPU_INTERVAL = 30
GPU_BACKOFF = 300
GPU_MAX_FAILURES = 2
FAULTS = {}
GIB = 1024 ** 3


def memory_status():
    ram = psutil.virtual_memory()
    result = {"ram_total": ram.total, "ram_available": ram.available,
              "commit_used": None, "commit_limit": None, "commit_available": None}
    if os.name == "nt":
        class PerformanceInfo(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in
                ("CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
                 "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
                (name, ctypes.c_ulong) for name in ("HandleCount", "ProcessCount", "ThreadCount")]
        data = PerformanceInfo()
        data.cb = ctypes.sizeof(data)
        if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(data), data.cb):
            result.update(commit_used=data.CommitTotal * data.PageSize,
                          commit_limit=data.CommitLimit * data.PageSize,
                          commit_available=(data.CommitLimit - data.CommitTotal) * data.PageSize)
    return result


def inventory():
    """Read installed-adapter registry metadata, not an active driver/NVML probe."""
    if os.name != "nt":
        return []
    import winreg
    result = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Video") as video:
            for index in range(winreg.QueryInfoKey(video)[0]):
                name = winreg.EnumKey(video, index)
                try:
                    with winreg.OpenKey(video, name + r"\0000") as key:
                        title = winreg.QueryValueEx(key, "DriverDesc")[0]
                        size = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0]
                        size = int.from_bytes(size, "little") if isinstance(size, bytes) else int(size)
                        if size > 0 and not any(g["name"] == title for g in result):
                            result.append({"name": title, "total_mb": size // (1024 ** 2),
                                           "used_mb": None, "free_mb": None, "utilization": None,
                                           "source": "安装信息（非实时显卡探测）"})
                except OSError:
                    continue
    except OSError:
        pass
    return result


def _query_nvidia():
    """One bounded query; callers own cache/backoff. Never retries or loads models."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        raise OSError("NVIDIA 查询程序不可用")
    process = subprocess.run([executable, "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,driver_version",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise ValueError("NVIDIA 查询失败")
    rows = []
    for values in csv.reader(io.StringIO(process.stdout)):
        if not values:
            continue
        if len(values) != 6:
            raise ValueError("NVIDIA 返回格式无效")
        name, total, used, free, utilization, driver = [value.strip() for value in values]
        total, used, free, utilization = int(total), int(used), int(free), int(utilization)
        if not name or total <= 0 or min(used, free, utilization) < 0 or utilization > 100:
            raise ValueError("NVIDIA 返回数值无效")
        rows.append({"name": name, "total_mb": total, "used_mb": used,
                     "free_mb": free, "utilization": utilization, "driver": driver,
                     "source": "NVIDIA 采样（30 秒缓存）"})
    if not rows:
        raise ValueError("NVIDIA 未返回显卡状态")
    return rows


def _monitor_status(enabled):
    return {"status": GPU_CACHE.get("status", "waiting") if enabled else "disabled",
            "error": GPU_CACHE.get("error", "") if enabled else "",
            "failures": GPU_CACHE.get("failures", 0), "paused": GPU_CACHE.get("paused", False),
            "next_probe_seconds": max(0, int(GPU_CACHE.get("next", 0) - time.monotonic())),
            "sampled_at": GPU_CACHE.get("sampled_at")}


def reset_monitor():
    """Explicit resume only changes the circuit; the next normal poll samples."""
    with LOCK:
        GPU_CACHE.update(next=0.0, checked=0.0, failures=0, paused=False, error="", status="waiting")
        return _monitor_status(settings().get("gpu_monitor", False))


def hardware(force=False):
    now = time.monotonic()
    enabled = settings().get("gpu_monitor", False)
    environment_pause = os.environ.get("LOCAL_AI_DISABLE_GPU_PROBE") == "1"
    with LOCK:
        if not GPU_CACHE["checked"]:
            GPU_CACHE["items"] = inventory()
            GPU_CACHE["checked"] = now
        supported = any("nvidia" in item.get("name", "").casefold() or
                        "geforce" in item.get("name", "").casefold() or
                        "quadro" in item.get("name", "").casefold() or
                        "tesla" in item.get("name", "").casefold()
                        for item in GPU_CACHE["items"])
        if enabled and environment_pause:
            GPU_CACHE.update(status="disabled_environment", error="启动环境已暂停主动显卡采样")
        elif enabled and not supported:
            GPU_CACHE.update(status="unavailable", error="未检测到 NVIDIA 安装信息，未执行主动查询")
        elif enabled and not GPU_CACHE.get("paused") and now >= GPU_CACHE["next"]:
            # Force never bypasses backoff after an error.
            GPU_CACHE["next"] = now + GPU_INTERVAL
            try:
                rows = _query_nvidia()
                GPU_CACHE.update(items=rows, error="", checked=now, failures=0, status="ready",
                                 sampled_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                failures = GPU_CACHE.get("failures", 0) + 1
                paused = failures >= GPU_MAX_FAILURES
                GPU_CACHE.update(failures=failures, paused=paused, status="paused" if paused else "backoff",
                                 error=("显卡监测连续异常，已暂停本次运行的探测；可手动恢复" if paused else
                                        "显卡监测不可用，已退避 5 分钟；不会因此切换加载模式"),
                                 next=now + GPU_BACKOFF)
        monitor = _monitor_status(enabled)
        gpus, error = list(GPU_CACHE["items"]), monitor["error"]
    return {**memory_status(), "disk_free": shutil.disk_usage(ROOT).free,
            "cpu_percent": psutil.cpu_percent(), "gpus": gpus, "gpu_monitor_error": error,
            "gpu_monitor_enabled": enabled, "gpu_monitor_status": monitor["status"],
            "gpu_monitor_paused": monitor["paused"], "gpu_monitor_failures": monitor["failures"],
            "gpu_monitor_next_probe_seconds": monitor["next_probe_seconds"],
            "gpu_monitor_sampled_at": monitor["sampled_at"], "root": str(ROOT),
            "models_path": str(ROOT / "models")}


def admit(context=8192, model_bytes=0, embedding=False, allow_large=False):
    value = memory_status()
    free_commit = value["commit_available"]
    if value["ram_available"] < GIB:
        raise ValueError("系统可用物理内存低于 1 GiB，已停止加载。请关闭其他程序后手动重试，不会自动切 CPU。")
    if embedding and value["ram_available"] < model_bytes * 1.4 + GIB:
        raise ValueError("CPU 嵌入模型所需的物理内存余量不足，请关闭其他程序或使用 0.6B；不会自动换用显卡。")
    if free_commit is not None and (free_commit < 2 * GIB or value["commit_used"] > value["commit_limit"] * .90):
        raise ValueError("系统提交内存接近上限，已停止加载以避免耗尽。请检查任务管理器“已提交”及分页文件；不会重复加载。")
    if context > 32768 and not allow_large:
        raise ValueError("超过 32K 的上下文属于高资源设置。请先在设置中确认允许大上下文；模型理论上限不代表本机安全容量。")
    # This is a conservative admission reserve, NOT an exact VRAM/KV estimate.
    reserve = max(2 * GIB, int(model_bytes * (1.2 if embedding else .35)) + int(context) * 16384)
    if free_commit is not None and free_commit < reserve:
        raise ValueError(f"剩余系统提交额度不足保守预留 {reserve / GIB:.1f} GiB，未加载。请减少上下文或关闭其他程序。此预留不是精确运行内存预测。")
    return value


def fault(key, error):
    text = str(error).lower()
    if any(x in text for x in ("out of memory", "allocation", "allocate", "cuda error", "ggml_assert", "0xc0000409", "device lost", "nvml")):
        FAULTS[key] = {"until": time.monotonic() + 60, "reason": "运行器分配/驱动异常，已冷却 60 秒；不会自动降级或重试"}


def check_fault(key):
    value = FAULTS.get(key)
    if value and value["until"] > time.monotonic():
        raise ValueError(value["reason"])


def reset_fault(key):
    FAULTS.pop(key, None)
