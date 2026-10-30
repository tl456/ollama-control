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
from datetime import datetime
from pathlib import Path

import jsonschema

from .config import LOCK, ROOT, read_json, settings, write_json
from .runtime import worker_command

from .toolkit.registry import BUILTINS, EXAMPLES
from .toolkit.common import resolve_args, allowed_path, READ_LOCK, configuration as tool_configuration, save_configuration as save_tool_configuration
from .toolkit.system_time import current_time
from .toolkit.file_info import file_info
from .toolkit.search_files import search_files
from .toolkit.list_directory import directory_list
from .toolkit.read_file import read_file

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
    definition = next((t for t in list_tools() if t["id"] == name and t["enabled"] and name in selected), None)
    if not definition:
        raise ValueError("工具未启用或不存在")
    errors = list(jsonschema.Draft202012Validator(definition["parameters"]).iter_errors(args))
    if errors:
        first = errors[0]
        field = ".".join(map(str, first.absolute_path)) or "参数对象"
        # Avoid echoing a whole code snippet/document in validation error messages.
        raise ValueError("工具参数不符合格式：" + field + "（" + str(first.validator) + "）；请查看参数说明和示例后修正，不要重复原参数。")
    if name == "system_time":
        return current_time()
    if name == "list_directory":
        return directory_list(args)
    if name == "file_info":
        return file_info(args)
    if name == "search_files":
        return search_files(args)
    if name == "read_file":
        return read_file(args)
    if name == "read_attachment":
        from . import attachments
        return attachments.read_authorized(args, scope)
    if name == "python_run":
        from . import python_lab
        return python_lab.execute(args, scope)
    folder = ROOT / "plugins/imported" / name
    integrity = read_json(folder / ".integrity.json", {})
    if not integrity or any(not (folder / f).is_file() or hashlib.sha256((folder / f).read_bytes()).hexdigest() != digest for f, digest in integrity.items()):
        raise ValueError("插件文件已变更，请重新导入核对后启用")
    payload = json.dumps({"arguments": args, "context": {"roots": settings()["roots"], "settings": tool_configuration(name)["values"]}}, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run(worker_command("plugin", name), input=payload, capture_output=True, cwd=ROOT,
                          timeout=min(30, int(definition.get("timeout", 10))), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode:
        raise ValueError("插件执行失败，请用工具测试查看详情")
    if len(proc.stdout) > 64000:
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
