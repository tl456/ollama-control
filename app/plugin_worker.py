import contextlib
import importlib.util
import io
import json
import sys

from .config import ROOT

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    payload = json.loads(sys.stdin.buffer.read())
    target = ROOT / "plugins/imported" / sys.argv[1] / "tool.py"
    # Imported plugins are trusted native code. Isolation is for lifecycle only.
    with contextlib.redirect_stdout(io.StringIO()):
        spec = importlib.util.spec_from_file_location("local_plugin", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run(payload["arguments"], payload["context"])
    print(json.dumps(result, ensure_ascii=False))
