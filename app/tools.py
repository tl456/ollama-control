from __future__ import annotations

import hashlib
import fnmatch
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
import threading
import time
import importlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import jsonschema

from .config import LOCK, ROOT, read_json, settings, write_json
from .runtime import worker_command

from .toolkit.registry import BUILTINS, EXAMPLES, HANDLERS, dispatch
from .toolkit.common import resolve_args, allowed_path, READ_LOCK, configuration as tool_configuration, save_configuration as save_tool_configuration
from .toolkit.system_time import current_time
from .toolkit.file_info import file_info
from .toolkit.search_files import search_files
from .toolkit.list_directory import directory_list
from .toolkit.read_file import read_file
EXECUTION_LOCK = threading.RLock()
ACTIVE_CALLS = set()
MAINTENANCE = False


def active_calls():
    with EXECUTION_LOCK:
        return len(ACTIVE_CALLS)


@contextmanager
def maintenance():
    global MAINTENANCE
    with EXECUTION_LOCK:
        if ACTIVE_CALLS or MAINTENANCE:
            raise ValueError("请等待工具操作完成，再初始化")
        MAINTENANCE = True
    try:
        yield
    finally:
        with EXECUTION_LOCK:
            MAINTENANCE = False

def list_tools():
    cfg=settings()
    result=[{**t,'builtin':True,'enabled':True} for t in BUILTINS]
    from .toolkit.python_run import definition
    result.append(definition())
    for folder in (ROOT/'plugins/imported').iterdir():
        if folder.is_dir():
            manifest=read_json(folder/'manifest.json',{})
            if manifest:result.append({**manifest,'builtin':False,'enabled':manifest['id'] in cfg['plugins']})
    return result

def execute(name, args, selected, scope=None):
    from . import audit
    scope = dict(scope or {})
    with EXECUTION_LOCK:
        if MAINTENANCE:
            raise ValueError("工作台正在初始化，请稍后调用工具")
        call_id = audit.start(name, args, scope)
        ACTIVE_CALLS.add(call_id)
    scope["_call_id"] = call_id
    started = time.monotonic()
    try:
        result = _execute(name, args, selected, scope)
        if not isinstance(result, dict):
            raise ValueError("工具返回值必须是JSON对象，请检查工具实现")
    except BaseException as error:
        audit.finish(call_id, name, args, scope, status="error", duration=time.monotonic()-started, error_type=type(error).__name__)
        raise
    finally:
        with EXECUTION_LOCK:
            ACTIVE_CALLS.discard(call_id)
    failed = bool(result.get("error") or result.get("ok") is False or
                  isinstance(result.get("status"), int) and result["status"] >= 400 or
                  name.removeprefix("copy_") == "python_run" and (result.get("status") != "finished" or result.get("exit_code") != 0))
    audit.finish(call_id, name, args, scope, result=result, status="failed" if failed else "success", duration=time.monotonic()-started)
    return result


def _execute(name, args, selected, scope=None):
    definition = next((t for t in list_tools() if t["id"] == name and t["enabled"] and name in selected), None)
    if not definition:
        raise ValueError("工具未启用或不存在")
    errors = list(jsonschema.Draft202012Validator(definition["parameters"]).iter_errors(args))
    if errors:
        first = errors[0]
        field = ".".join(map(str, first.absolute_path)) or "参数对象"
        # Avoid echoing a whole code snippet/document in validation error messages.
        raise ValueError("工具参数不符合格式：" + field + "（" + str(first.validator) + "）；请查看参数说明和示例后修正，不要重复原参数。")
    if name in HANDLERS:
        return dispatch(name, args, scope)
    folder = ROOT / "plugins/imported" / name
    integrity = read_json(folder / ".integrity.json", {})
    if not integrity or any(not (folder / f).is_file() or hashlib.sha256((folder / f).read_bytes()).hexdigest() != digest for f, digest in integrity.items()):
        raise ValueError("插件文件已变更，请重新导入核对后启用")
    payload = json.dumps({"arguments": args, "context": {"roots": settings()["roots"], "settings": tool_configuration(name)["values"],
                         "scope": {k:v for k,v in (scope or {}).items() if k in ("_call_id","attachment_ids","python_run_ids","model","provider")}}}, ensure_ascii=False).encode("utf-8")
    code_root = ROOT if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    proc = subprocess.run(worker_command("plugin", name), input=payload, capture_output=True, cwd=code_root,
                          env={**os.environ, "LOCAL_AI_ROOT": str(ROOT)},
                          timeout=max(1, min(650, int(definition.get("timeout", 30)))), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode:
        raise ValueError("插件执行失败，请用工具测试查看详情")
    if len(proc.stdout) > 8 * 1024 * 1024:
        raise ValueError("插件返回内容过大")
    return json.loads(proc.stdout.decode("utf-8"))

def import_plugin(blob):
    if len(blob) > 20 * 1024 * 1024:
        raise ValueError("工具包最大 20 MiB")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if sum(x.file_size for x in archive.infolist()) > 40 * 1024 * 1024:
            raise ValueError("工具包展开后过大")
        manifest_names = [n for n in archive.namelist() if n.rstrip("/").endswith("manifest.json")]
        if len(manifest_names) != 1:
            raise ValueError("工具包需要唯一的 manifest.json")
        prefix = manifest_names[0][:-len("manifest.json")]
        manifest = json.loads(archive.read(manifest_names[0]))
        ident = manifest.get("id", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,48}", ident) or ident in {t["id"] for t in BUILTINS} | {"python_run", "memory_read", "memory_update", "knowledge_search", "knowledge_read"}:
            raise ValueError("无效或保留的插件 ID")
        for key in ("name", "version", "description", "parameters", "permissions"):
            if key not in manifest:
                raise ValueError("缺少字段：" + key)
        jsonschema.Draft202012Validator.check_schema(manifest["parameters"])
        if manifest.get("entrypoint", "tool.py") != "tool.py":
            raise ValueError("首版入口固定为 tool.py 中的 run(arguments, context)")
        folder = ROOT / "plugins/imported" / ident
        if folder.exists():
            raise ValueError("该 ID 已导入，请停用并移除后重新导入")
        safe = []
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = item.filename
            rel = Path(name[len(prefix):]) if name.startswith(prefix) else None
            if rel is None or rel.is_absolute() or rel.drive or ".." in rel.parts or "\\" in name or ":" in name or rel.name.startswith("."):
                raise ValueError("工具包包含不安全路径")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("工具包不允许符号链接")
            safe.append((rel, archive.read(item)))
        if not any(str(r) == "tool.py" for r, _ in safe):
            raise ValueError("缺少 tool.py")
        folder.mkdir()
        integrity = {}
        for rel, data in safe:
            dest = folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            integrity[str(rel)] = hashlib.sha256(data).hexdigest()
        write_json(folder / ".integrity.json", integrity)
    return manifest

def definitions(selected, scope=None):
    result=[]
    for tool in list_tools():
        if tool['id'] not in selected or not tool['enabled']: continue
        description=tool['description']
        if tool['id'] in EXAMPLES:
            description+=' 参数示例（路径必须用实际值）：'+json.dumps(EXAMPLES[tool['id']],ensure_ascii=False,separators=(',',':'))
        result.append({'type':'function','function':{'name':tool['id'],'description':description,'parameters':tool['parameters']}})
    return result


def export_plugin(ident):
    """Export public source only. No saved settings, credentials or execution data."""
    item=next((t for t in list_tools() if t["id"]==ident),None)
    if not item: raise ValueError("工具不存在")
    if item.get("exportable") is False:
        raise ValueError("此工具依赖当前聊天的交互通道，不能独立导出；完整源码包中包含实现和教程")
    manifest={k:v for k,v in item.items() if k not in ("builtin","enabled")}
    entries={}
    if item["builtin"]:
        manifest["id"]="copy_"+ident
        manifest["name"]=item["name"]+" · 副本"
        entries["tool.py"]=("# Built-in adapter for LocalAIWorkbench 1.6.0.\n"
                            "from app.toolkit.registry import dispatch\n\n"
                            "def run(arguments, context):\n"
                            "    return dispatch("+repr(ident)+", arguments, context.get('scope', {}), settings_override=context.get('settings', {}))\n").encode("utf-8")
        entries["README.md"]=("# "+manifest["name"]+"\n\n内置工具适配示例，依赖本工作台1.6.0的共享服务。导入后需启用；未导出原工具配置、凭据和记录。\n\n"+item["description"]).encode("utf-8")
    else:
        folder=ROOT/"plugins/imported"/ident
        integrity=read_json(folder/".integrity.json",{})
        for name,digest in integrity.items():
            rel=Path(name)
            if rel.is_absolute() or ".." in rel.parts or any(x in {"config","data","logs","cache"} for x in rel.parts): continue
            path=folder/rel
            if path.is_symlink() or not path.is_file(): raise ValueError("工具源码文件缺失或为链接")
            blob=path.read_bytes()
            if hashlib.sha256(blob).hexdigest()!=digest: raise ValueError("工具源码已变更，请核对后重新导入再导出")
            entries[rel.as_posix()]=blob
    entries["manifest.json"]=(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
    if "tool.py" not in entries: raise ValueError("缺少可导出的工具入口")
    if sum(map(len,entries.values()))>40*1024*1024: raise ValueError("导出工具源码过大")
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,blob in sorted(entries.items()): archive.writestr(name,blob)
    return output.getvalue()
