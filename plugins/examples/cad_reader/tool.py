"""MIT tl456. Independent read-only CAD adapter; no UI/ROT attachment or Save calls."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
FORMATS = {"caxa": (".dwg", ".dxf", ".exb"), "nx": (".stp", ".step", ".prt")}
DEFAULTS = {"nx_directory": "", "timeout_seconds": 120, "page_size": 50, "max_file_mb": 100}

def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def nx_install(settings):
    candidates = []
    if settings.get("nx_directory"):
        candidates.append(Path(settings["nx_directory"]))
    elif os.name == "nt":
        if os.environ.get("UGII_BASE_DIR"):
            candidates.append(Path(os.environ["UGII_BASE_DIR"]))
        folder = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Siemens"
        if folder.is_dir():
            candidates.extend(sorted((p for p in folder.iterdir() if p.is_dir() and p.name.upper().startswith("NX")), reverse=True)[:20])
    for candidate in candidates:
        exe = candidate / "NXBIN" / "run_journal.exe"
        if exe.is_file():
            return candidate.resolve()
    return None

def caxa_registered(progid):
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"):
            return True
    except OSError:
        return False

def probe(settings):
    nx = nx_install(settings)
    return {"ok": True, "action": "probe", "read_only": True,
            "backends": [
                {"id": "caxa", "available": any(caxa_registered(x) for x in ("CAXAInfo.DWGInfo", "CAXAInfo.EBInfo")),
                 "dwg_component": caxa_registered("CAXAInfo.DWGInfo"), "exb_component": caxa_registered("CAXAInfo.EBInfo"),
                 "interface": "CAXAInfo.DWGInfo / CAXAInfo.EBInfo (独立进程内COM)",
                 "formats": list(FORMATS["caxa"]), "fields": ["图纸数量", "图幅与比例", "标题栏", "明细表", "文字及位置"],
                 "license": "未验证；以实际OpenFile结果为准"},
                {"id": "nx", "available": nx is not None, "installation": str(nx) if nx else None,
                 "interface": "NX Open / run_journal（独立后台）",
                 "formats": list(FORMATS["nx"]), "fields": ["单位", "实体/面/边数量", "包围盒", "装配首层"],
                 "license": "未验证；需要NX Open与相应转换器许可"}],
            "limitations": ["探测到安装不等于许可证有效或文件格式一定可读", "本版不提供CAD编辑、保存、联网或质量推算", "不连接用户正在编辑的CAD会话"]}

def caxa_read(path, page, size):
    import pythoncom
    import win32com.client
    progid = "CAXAInfo.EBInfo" if path.suffix.lower() == ".exb" else "CAXAInfo.DWGInfo"
    if not caxa_registered(progid):
        raise ValueError("所需CAXA信息提取组件未注册；请安装官方对应位数组件")
    pythoncom.CoInitialize()
    instance = None
    try:
        # This is an information component, not CaxaDraft.Application / user UI.
        instance = win32com.client.DispatchEx(progid, clsctx=pythoncom.CLSCTX_INPROC_SERVER)
        if not instance.OpenFile(str(path)):
            raise ValueError("CAXA OpenFile返回失败：文件版本、内容或组件许可不可用；未读取成功")
        values, missing, clipped = {}, [], []
        methods = (("paper_count", "GetPaperCount"), ("paper_size_and_scale", "GetPaperSizeAndScale"),
                   ("title_block", "GetHeaderInfo"), ("bom_columns", "GetBomHeaderInfo"), ("bom", "GetBomInfo"))
        for field, method in methods:
            try:
                result = getattr(instance, method)(str(path)) if progid.endswith("EBInfo") and method == "GetPaperCount" else getattr(instance, method)()
                if isinstance(result, tuple) and result and isinstance(result[0], bool) and not result[0]:
                    missing.append(field)
                else:
                    values[field] = bounded(result)
                    if len(json.dumps(values[field], ensure_ascii=False).encode("utf-8")) > 3000:
                        values[field] = {"truncated": True, "preview": clip_utf8(json.dumps(values[field], ensure_ascii=False), 2500)}
                        clipped.append(field)
            except Exception:
                missing.append(field)
        text_items, total = [], 0
        try:
            texts, positions = instance.GetAllTextAndPosInDB()
            texts, positions = list(texts or []), list(positions or [])
            total = len(texts)
            start = (page - 1) * size
            for i in range(start, min(total, start + size)):
                text = str(texts[i])
                text_items.append({"index": i + 1, "text": clip_utf8(text, 200),
                                   "truncated": len(text.encode("utf-8")) > 200,
                                   "position_raw": clip_utf8(str(positions[i]), 80) if i < len(positions) else None})
        except Exception:
            missing.append("text_and_positions")
        return {"ok": True, "backend": "caxa", "interface": progid, "values": values,
                "texts": text_items, "page": page, "page_size": size, "total_texts": total,
                "not_obtained": missing, "truncated_fields": clipped,
                "scope": "图纸信息组件返回；标题栏/明细表依赖图纸结构，不等于完整几何解析；单条文字限200 UTF-8字节，截短会标记",
                "not_supported": ["实体拓扑", "三维体积/质量", "完整图层列表"]}
    finally:
        if instance is not None:
            try:
                instance.CloseFile()
            except Exception:
                pass
            instance = None
        pythoncom.CoUninitialize()

def bounded(value):
    if isinstance(value, str):
        return value[:8000]
    if isinstance(value, (tuple, list)):
        return [bounded(v) for v in value[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:1000]

def clip_utf8(value, limit):
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")

def run_journal(command, env, folder, timeout):
    """Assign a suspended new process to our job BEFORE it can create children.

    Never finds processes by name and never attaches to an existing NX session.
    No source CAD or CAD output is written into product logs.
    """
    import win32event
    import win32job
    import win32process
    job = win32job.CreateJobObject(None, "")
    process = thread = None
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        process, thread, _, _ = win32process.CreateProcess(
            command[0], subprocess.list2cmdline(command), None, None, False,
            subprocess.CREATE_NO_WINDOW | win32process.CREATE_SUSPENDED,
            env, str(folder), win32process.STARTUPINFO())
        try:
            win32job.AssignProcessToJobObject(job, process)
        except Exception:
            win32process.TerminateProcess(process, 1)
            raise ValueError("无法隔离NX后台进程，未继续执行")
        deadline = time.monotonic() + max(5, min(120, int(timeout)))
        win32process.ResumeThread(thread)
        if win32event.WaitForSingleObject(process, max(5, min(120, int(timeout))) * 1000) == win32event.WAIT_TIMEOUT:
            raise TimeoutError("NX后台读取超时，已结束本工具创建的任务；未修改原文件")
        # Some NX releases leave a translator child finishing after the launcher
        # exits. Wait for our whole job so we do not cut off its result file.
        while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
            if time.monotonic() >= deadline:
                raise TimeoutError("NX转换器读取超时，已结束本工具创建的任务")
            time.sleep(0.1)
    finally:
        job.Close()  # terminates only this newly-created process tree
        if process is not None:
            win32event.WaitForSingleObject(process, 5000)
            process.Close()
        if thread is not None:
            thread.Close()

def nx_read(path, settings, folder, page, size):
    installation = nx_install(settings)
    if installation is None:
        raise ValueError("未找到NX官方run_journal，请在工具设置中指定已安装NX目录")
    request = folder / "request.json"
    output = folder / "result.json"
    request.write_text(json.dumps({"path": str(path), "output": str(output), "page": page,
                                  "page_size": size, "nx_directory": str(installation)}, ensure_ascii=True), encoding="utf-8")
    exe = installation / "NXBIN" / "run_journal.exe"
    env = {**os.environ, "UGII_BASE_DIR": str(installation), "UGII_TMP_DIR": str(folder)}
    env.pop("UGII_JOURNAL", None)
    command = [str(exe), str(ROOT / "nx_reader.py"), "-args", str(request)]
    run_journal(command, env, folder, settings["timeout_seconds"])
    if not output.is_file():
        raise ValueError("NX未返回有效结果；请检查许可证、转换器与NX版本。未把进程退出当作读取成功")
    if output.stat().st_size > 60000:
        raise ValueError("NX返回内容超过限制，请减小工具的每页条数")
    result = json.loads(output.read_text(encoding="utf-8-sig"))
    if not result.get("ok"):
        raise ValueError("NX读取失败：" + str(result.get("error", "接口未返回成功"))[:250])
    return result

def run(arguments, context):
    settings = {**DEFAULTS, **context.get("settings", {})}
    action = arguments.get("action")
    if action == "probe":
        return probe(settings)
    if action != "inspect":
        raise ValueError("action须为probe或inspect")
    source = Path(str(arguments.get("path", "")))
    if not source.is_absolute() or not source.is_file():
        raise ValueError("请提供实际存在的CAD文件完整路径")
    source = source.resolve()
    extension = source.suffix.lower()
    backend = arguments.get("backend", "auto")
    if backend == "auto":
        backend = next((key for key, formats in FORMATS.items() if extension in formats), "")
    if backend not in FORMATS or extension not in FORMATS[backend]:
        raise ValueError("所选后端不支持此格式：CAXA支持DWG/DXF/EXB，NX支持PRT/STP/STEP")
    if source.stat().st_size > max(1, min(512, int(settings["max_file_mb"]))) * 1024 * 1024:
        raise ValueError("CAD文件超过工具设置的大小上限")
    page = max(1, int(arguments.get("page", 1)))
    size = max(1, min(100, int(settings["page_size"])))
    before = digest(source)
    started = time.monotonic()
    # Work only on a disposable file copy; source CAD is never handed to a writer.
    with tempfile.TemporaryDirectory(prefix="workbench-cad-") as temporary:
        folder = Path(temporary)
        working = folder / ("input" + extension)
        shutil.copyfile(source, working)
        if digest(working) != before:
            raise ValueError("CAD副本字节校验失败，未调用软件")
        result = caxa_read(working, page, size) if backend == "caxa" else nx_read(working, settings, folder, page, size)
    if digest(source) != before:
        raise ValueError("原文件在读取期间被外部修改，结果已作废，请重新读取")
    return {**result, "source": {"name": source.name, "bytes": source.stat().st_size, "sha256": before},
            "read_only": True, "source_unchanged": True, "seconds": round(time.monotonic()-started,3),
            "external_references": "不主动加载或复制外部引用；未加载组件不代表源装配中不存在"}
