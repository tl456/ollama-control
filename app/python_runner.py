"""Standalone stdlib-only runner, usable with the official embedded CPython ZIP."""
import io
import sys
import traceback
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
source, input_path = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(source.parent))
from workbench import INPUTS
sys.stdin = io.StringIO(input_path.read_text(encoding="utf-8"))
scope = {"__name__": "__main__", "__file__": str(source), "INPUTS": INPUTS}
try:
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), scope, scope)
except BaseException:
    traceback.print_exc()
    sys.exit(1)
