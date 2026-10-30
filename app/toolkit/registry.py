"""Definitions are collected from each built-in's own directory."""
import json
import importlib
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
BUILTINS = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DIRECTORY.glob("*/manifest.json")) if p.parent.name != "python_run"]
EXAMPLES = {value["id"]: value.get("example", {}) for value in BUILTINS}
EXAMPLES["python_run"] = {"code": "print(2 + 3)"}

# Small dispatch metadata; tool implementations never call one another.
HANDLERS = {
    "system_time": ("app.toolkit.system_time", "current_time", "none"),
    "list_directory": ("app.toolkit.list_directory", "directory_list", "args"),
    "file_info": ("app.toolkit.file_info", "file_info", "args"),
    "search_files": ("app.toolkit.search_files", "search_files", "args"),
    "read_file": ("app.toolkit.read_file", "read_file", "args"),
    "read_attachment": ("app.attachments", "read_authorized", "scope"),
    "python_run": ("app.python_lab", "execute", "scope"),
}
for definition in BUILTINS:
    HANDLERS.setdefault(definition["id"], ("app.toolkit." + definition["id"], "run", "scope"))

def dispatch(name, args, scope=None, settings_override=None):
    from .common import CONFIG_OVERRIDES
    token = CONFIG_OVERRIDES.set({**CONFIG_OVERRIDES.get(), name: dict(settings_override)}) if settings_override is not None else None
    try:
        return _dispatch(name, args, scope)
    finally:
        if token is not None:
            CONFIG_OVERRIDES.reset(token)


def _dispatch(name, args, scope=None):
    module, attribute, signature = HANDLERS[name]
    function = getattr(importlib.import_module(module), attribute)
    if signature == "none": return function()
    if signature == "args": return function(args)
    return function(args, scope)
