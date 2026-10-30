from __future__ import annotations

import sys
from pathlib import Path


def worker_command(kind: str, *arguments) -> list[str]:
    if getattr(sys, "frozen", False):
        worker = Path(sys.executable).with_name("LocalAIWorker.exe")
        return [str(worker), "--worker", kind, *map(str, arguments)]
    return [sys.executable, "-m", "app." + kind + "_worker", *map(str, arguments)]
