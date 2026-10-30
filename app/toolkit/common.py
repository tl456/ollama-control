"""Shared ordinary filesystem/setting services. No per-tool business dispatch."""
from pathlib import Path
import os
import re
import stat
import threading
from contextvars import ContextVar
import jsonschema
from ..config import ROOT, LOCK, settings, read_json, write_json

READ_LOCK = threading.Lock()
FILESYSTEM = ("list_directory", "file_info", "search_files", "read_file")
CONFIG_OVERRIDES = ContextVar("tool_configuration_overrides", default={})


def configuration_override(ident):
    value = CONFIG_OVERRIDES.get().get(ident)
    return dict(value) if value is not None else None


def checked(path):
    target = Path(path)
    if not target.is_absolute() or "\x00" in str(path):
        raise ValueError("请提供当前Windows账户可读取的完整绝对路径")
    try:
        target = target.resolve(strict=True)
        info = target.stat()
    except (OSError, RuntimeError) as error:
        raise ValueError("目标不存在或当前Windows账户没有读取权限") from error
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise ValueError("仅支持普通文件和目录，不读取设备或管道")
    return target


def allowed_path(root_id, relative_path="."):
    if root_id == "__absolute__":
        target = checked(relative_path)
        return (target if target.is_dir() else target.parent), target
    root = next((r for r in settings().get("roots", []) if r["id"] == root_id), None)
    if not root:
        raise ValueError("旧目录别名不存在，请改用path完整路径；无需授权目录")
    base = Path(root["path"]).resolve()
    target = checked(base / relative_path)
    return (base if target.is_relative_to(base) else target.parent), target


def resolve_args(args):
    if args.get("path"):
        target = checked(args["path"])
        return {**args, "path": str(target), "root_id": "__absolute__", "relative_path": str(target)}
    if args.get("root_id"):
        _, target = allowed_path(args["root_id"], args.get("relative_path", "."))
        return {**args, "path": str(target), "root_id": "__absolute__", "relative_path": str(target)}
    raise ValueError("请传path完整路径，例如C:\\资料；当前账户可访问目录不限，文件工具只读")


def tool_values(ident):
    defaults = {"start_directory": ""}
    if ident == "search_files": defaults["search_depth"] = settings().get("search_depth", 1)
    if ident == "read_file": defaults["tool_read_mode"] = settings().get("tool_read_mode", "office")
    override = configuration_override(ident)
    return {**defaults, **(override if override is not None else read_json(ROOT / "config/tools" / (ident + ".json"), {}))}


def configuration(ident):
    override = configuration_override(ident)
    if override is not None:
        from .registry import BUILTINS
        if ident == "python_run":
            from ..python_lab import SCHEMA
            schema = SCHEMA
        else:
            schema = next(t for t in BUILTINS if t["id"] == ident).get("settings_schema", {"type":"object", "properties":{}})
        defaults = {key: prop["default"] for key, prop in schema.get("properties", {}).items() if "default" in prop}
        return {"schema": schema, "values": {**defaults, **override}}
    if ident == "python_run":
        from .. import python_lab
        return {"schema": python_lab.SCHEMA, "values": python_lab.config()}
    if ident in FILESYSTEM:
        from .registry import BUILTINS
        schema = next(t["settings_schema"] for t in BUILTINS if t["id"] == ident)
        props = schema["properties"]
        values = tool_values(ident)
        return {"schema": schema, "values": {k: values[k] for k in props}}
    from .. import tools
    item = next((t for t in tools.list_tools() if t["id"] == ident), None)
    if not item: raise ValueError("工具不存在")
    schema = item.get("settings_schema", {"type": "object", "properties": {}, "additionalProperties": False})
    defaults = {key: prop["default"] for key, prop in schema.get("properties", {}).items() if "default" in prop}
    return {"schema": schema, "values": {**defaults, **read_json(ROOT / "config/tools" / (ident + ".json"), {})}}


def save_configuration(ident, values):
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,48}", ident): raise ValueError("无效工具编号")
    values = dict(values)
    legacy = values.pop("roots", None) if ident in FILESYSTEM else None
    jsonschema.validate(values, configuration(ident)["schema"])
    with LOCK:
        if legacy is not None:
            # Old API aliases remain conveniences, never a permission boundary.
            if not isinstance(legacy, list) or len(legacy) > 100: raise ValueError("目录别名格式无效")
            if len({r.get("id") for r in legacy if isinstance(r, dict)}) != len(legacy): raise ValueError("目录别名不可重复")
            for root in legacy:
                if not isinstance(root, dict) or not re.fullmatch(r"[A-Za-z0-9_]+", root.get("id", "")) or not Path(root.get("path", "")).is_absolute(): raise ValueError("目录别名需要完整路径")
            old = read_json(ROOT / "config/tools.json", {})
            old.setdefault("filesystem", {})["roots"] = legacy
            write_json(ROOT / "config/tools.json", old)
        folder = ROOT / "config/tools"
        folder.mkdir(exist_ok=True)
        write_json(folder / (ident + ".json"), {**(tool_values(ident) if ident in FILESYSTEM else configuration(ident)["values"]), **values})
    return configuration(ident)
