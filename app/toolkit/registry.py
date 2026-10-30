"""Definitions are collected from each built-in's own directory."""
import json
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
BUILTINS = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DIRECTORY.glob("*/manifest.json")) if p.parent.name != "python_run"]
EXAMPLES = {value["id"]: value.get("example", {}) for value in BUILTINS}
EXAMPLES["python_run"] = {"code": "print(2 + 3)"}
