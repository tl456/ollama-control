"""Native dialogs: selections are suggestions, not directory authorizations."""
from __future__ import annotations

import threading

_PICKER_LOCK = threading.Lock()


def choose_directory() -> str:
    if not _PICKER_LOCK.acquire(blocking=False):
        raise ValueError("文件夹选择窗口已打开，请先完成或取消选择")
    window = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=window, title="选择允许小工具读取的文件夹", mustexist=True
        )
        return str(selected) if selected else ""
    except ImportError as error:
        raise ValueError("此环境无法打开文件夹选择窗口，请手动填写完整路径") from error
    finally:
        if window is not None:
            window.destroy()
        _PICKER_LOCK.release()
