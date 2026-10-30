"""Lightweight Windows tray supervisor; owns UI, not arbitrary Ollama processes."""
from __future__ import annotations
import ctypes
import hashlib
import json
import logging
import threading
import time
import urllib.request
from .config import ROOT, VERSION, read_json, write_json

def notify_existing(url):
    try:
        local_token = read_json(ROOT / "cache/control.json", {}).get("token", "")
        request = urllib.request.Request(url + "/api/ui/show", data=b"{}", headers={"Content-Type": "application/json", "X-Local-Control": local_token})
        urllib.request.urlopen(request, timeout=2).close()
        return True
    except Exception:
        return False

class Tray:
    def __init__(self, url, opener, on_exit):
        self.url, self.opener, self.on_exit = url, opener, on_exit
        self.icon = None
        self.window = None
        self.quitting = False
        self._exit_lock = threading.Lock()
        self.done = threading.Event()
        self.last_request = time.time()
        self.status = "正在连接本地工作台"

    def show(self, *args):
        if self.window:
            try:
                self.window.show(); self.window.restore()
                return
            except Exception:
                logging.exception("Window restore failed")
        self.opener()

    def closing(self, *args):
        from .config import settings
        if not self.quitting and self.icon and settings()["close_to_tray"]:
            self.window.hide()
            return False
        return True

    def request(self, path, body=None):
        try:
            if path not in ("/api/ui/show", "/api/ui/quit", "/api/ui/stop-all", "/api/ui/unload-all"):
                raise ValueError("托盘控制仅允许明确的生命周期操作")
            local_token = read_json(ROOT / "cache/control.json", {}).get("token", "")
            request = urllib.request.Request(self.url + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json", "X-Local-Control": local_token})
            urllib.request.urlopen(request, timeout=5).close()
        except Exception:
            logging.exception("Tray action failed")

    def exit(self, *args):
        with self._exit_lock:
            if self.quitting:
                return
            self.quitting = True
        try:
            try:
                self.request("/api/ui/stop-all")
            except Exception:
                logging.exception("Tray stop-all failed during exit")
            try:
                if self.window:
                    self.window.destroy()
            except Exception:
                logging.exception("Window cleanup failed during exit")
            try:
                if self.icon:
                    self.icon.stop()
            except Exception:
                logging.exception("Tray icon cleanup failed during exit")
            try:
                self.on_exit()
            except Exception:
                logging.exception("Owned backend cleanup failed during exit")
        finally:
            self.done.set()

    def start(self):
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new("RGBA", (64, 64), "#151c22")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=10, outline="#f0a34a", width=3)
        draw.line((22, 18, 22, 43, 43, 43), fill="#e4edf3", width=6)
        from pathlib import Path
        icon_path = Path(__file__).resolve().parents[1] / "web/app.ico"
        if icon_path.is_file():
            image = Image.open(icon_path).convert("RGBA").resize((64, 64))
        self.icon = pystray.Icon("LocalAIStudio", image, "本地智能工作台 " + VERSION,
            pystray.Menu(pystray.MenuItem("打开工作台", self.show, default=True),
                         pystray.MenuItem(lambda item: self.status, lambda *a: None, enabled=False),
                         pystray.MenuItem("停止当前任务", lambda *a: self.request("/api/ui/stop-all")),
                         pystray.MenuItem("释放本地模型占用", lambda *a: self.request("/api/ui/unload-all")),
                         pystray.Menu.SEPARATOR, pystray.MenuItem("退出工作台（后台按设置处理）", self.exit)))
        self.icon.run_detached()
        def poll():
            ticks = 0
            while not self.done.wait(1):
                ticks += 1
                if ticks % 10 == 1:
                    try:
                        with urllib.request.urlopen(self.url + "/health", timeout=2) as response:
                            health = json.load(response)
                        self.status = "控制台 " + health["version"] + " · 服务在线"
                    except Exception:
                        self.status = "控制台服务离线"
                    self.icon.title = "本地智能工作台 · " + self.status
                    self.icon.update_menu()
                record = read_json(ROOT / "cache/ui-show.json", {})
                if record.get("time", 0) > self.last_request:
                    self.last_request = record["time"]
                    self.show()
        threading.Thread(target=poll, daemon=True).start()
        return self

def acquire_singleton():
    name = "LocalAIStudio-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, name)
    return handle, kernel.GetLastError() != 183
