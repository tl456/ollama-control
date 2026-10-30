from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
import runpy
from pathlib import Path
from app.service_config import WEB_PORT

ROOT = Path(os.environ.get("LOCAL_AI_ROOT", Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
os.chdir(ROOT)
os.environ.setdefault("PYTHONUTF8", "1")
for stream in (sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
os.environ["LOCAL_AI_ROOT"] = str(ROOT)
os.environ["OLLAMA_MODELS"] = str(ROOT / "models")
URL = f"http://127.0.0.1:{WEB_PORT}"


def alive():
    try:
        with urllib.request.urlopen(URL + "/health", timeout=1) as response:
            value = json.load(response)
            return value.get("app") == "Local AI Studio" and value.get("root") == str(ROOT)
    except Exception:
        return False


def launch_window(mode=None, tray=None):
    from app.config import settings
    cfg = settings()
    mode = mode or cfg.get("launch_mode", "app")
    if mode == "webview2":
        try:
            import webview
            window = webview.create_window("本地智能工作台", URL, width=1280, height=850, min_size=(920, 640), background_color="#0e131a", text_select=True)
            if tray:
                tray.window = window
                window.events.closing += tray.closing
            webview.start(gui="edgechromium", debug=False, private_mode=False, storage_path=str(ROOT / "cache/webview"), icon=str(Path(__file__).resolve().parent / "web/app.ico"))
            return "webview2"
        except Exception:
            logging.exception("WebView2 unavailable; falling back to application mode")
            mode = "app"
    if mode == "app":
        candidates = [
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for exe in candidates:
            if exe.is_file():
                subprocess.Popen([str(exe), f"--app={URL}", f"--user-data-dir={ROOT / 'cache/browser'}", "--no-first-run", "--no-default-browser-check"], cwd=ROOT)
                return "browser-app"
    webbrowser.open(URL)
    return "default-browser"


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        kind = sys.argv[2]
        if kind not in ("doc", "plugin", "selftest", "office"):
            raise ValueError("Unknown worker")
        sys.argv = ["app." + kind + "_worker", *sys.argv[3:]]
        runpy.run_module("app." + kind + "_worker", run_name="__main__")
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--browser", choices=("app", "webview2", "default"), default=None)
    parser.add_argument("--port", type=int, default=WEB_PORT)
    parser.add_argument("--tray", action="store_true", help="Start in tray without opening a window")
    args = parser.parse_args()
    if os.name == "nt":
        import ctypes
        # Stable Windows identity and a shared ICO keep native taskbar/tray icons coherent.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("tl456.LocalAIWorkbench")
    global URL
    URL = "http://127.0.0.1:" + str(args.port)
    (ROOT / "logs").mkdir(exist_ok=True)
    from app.lifecycle import ManagedLogHandler
    logging.basicConfig(handlers=[ManagedLogHandler(ROOT / "logs/app.log")],
                        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.serve:
        from app.config import write_json
        from app.config import settings
        from app.lifecycle import initialize_version
        initialize_version()
        import uvicorn
        import psutil
        from app import port_manager
        sock, args.port = port_manager.bind_available("web", settings()["web_bind"], args.port)
        URL = "http://127.0.0.1:" + str(args.port)
        if settings().get("ollama_management", "managed") == "managed":
            port_manager.prepare_managed()
        write_json(ROOT / "cache/server.json", {"pid": os.getpid(), "url": URL, "created": psutil.Process().create_time(), "exe": psutil.Process().exe()})
        from app.server import app
        server = uvicorn.Server(uvicorn.Config(app, host=settings()["web_bind"], port=args.port, log_config=None, access_log=False))
        app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
        server.run(sockets=[sock])
        return
    from app.tray import Tray, acquire_singleton, notify_existing
    from app.config import read_json
    previous = read_json(ROOT / "cache/server.json", {})
    previous_url = previous.get("url", "")
    if previous_url.startswith("http://127.0.0.1:"):
        candidate = URL
        URL = previous_url
        if not alive():
            URL = candidate
    mutex, first = acquire_singleton()
    if not first:
        notify_existing(URL)
        return
    owned_server = None
    if not alive():
        from app.lifecycle import rotate_closed_log, capture_process_log
        rotate_closed_log(ROOT / "logs/server.log")
        command = [sys.executable, "--serve", "--port", str(args.port)] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "main.py"), "--serve", "--port", str(args.port)]
        owned_server = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        capture_process_log(owned_server, ROOT / "logs/server.log")
        for _ in range(80):
            record = read_json(ROOT / "cache/server.json", {})
            if record.get("pid") == owned_server.pid:
                URL = record["url"]
            if alive():
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("本地服务未启动，请查看 logs/server.log")
    def exit_owned():
        # Request lifespan shutdown so queue state and owned services are settled.
        try:
            from app.config import read_json
            control = read_json(ROOT / "cache/control.json", {}).get("token", "")
            request = urllib.request.Request(URL + "/api/ui/quit", data=b"{}", headers={"Content-Type": "application/json", "X-Local-Control": control})
            with urllib.request.urlopen(request, timeout=3):
                pass
            for _ in range(50):
                if not alive():
                    return
                time.sleep(.2)
        except Exception:
            logging.exception("Graceful shutdown endpoint unavailable")
        from app.providers import shutdown_owned_services
        shutdown_owned_services()
        if owned_server and owned_server.poll() is None:
            owned_server.terminate()
        else:
            from app.config import read_json
            import psutil
            record = read_json(ROOT / "cache/server.json", {})
            try:
                process = psutil.Process(record["pid"])
                if record.get("created") and abs(process.create_time() - record["created"]) < 1 and Path(process.exe()).resolve() == Path(record["exe"]).resolve() and Path(process.cwd()).resolve() == ROOT and "--serve" in process.cmdline():
                    process.terminate()
            except (KeyError, psutil.Error):
                logging.exception("UI server ownership not confirmed; left running")
    try:
        tray = Tray(URL, lambda: launch_window(args.browser), exit_owned).start()
    except Exception:
        logging.exception("Tray unavailable; opening UI normally")
        launch_window(args.browser)
        return
    if not args.tray:
        launch_window(args.browser, tray)
        if tray.window and not tray.quitting:
            tray.exit()
    tray.done.wait()


if __name__ == "__main__":
    main()
