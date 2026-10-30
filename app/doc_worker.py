"""Separate Word instance, read-only without macros, never saves source."""
import sys

if __name__ == "__main__":
    import pythoncom
    import psutil
    import win32com.client
    import win32process
    sys.stdout.reconfigure(encoding="utf-8")
    pythoncom.CoInitialize()
    word = None
    document = None
    app_kind = "word"
    existing_pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() in ("winword.exe", "excel.exe", "powerpnt.exe")}
    owned_app = False
    security_before = None

    def is_new_application(application, process_name):
        # Office versions expose HWND inconsistently: property, method, or absent.
        for attribute in ("Hwnd", "HWND"):
            try:
                handle = getattr(application, attribute)
                handle = handle() if callable(handle) else handle
                pid = win32process.GetWindowThreadProcessId(int(handle))[1]
                return pid not in existing_pids
            except Exception:
                continue
        new_pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == process_name} - existing_pids
        return len(new_pids) == 1
    try:
        from pathlib import Path
        ext = Path(sys.argv[1]).suffix.lower()
        if ext in (".doc", ".docx"):
            word = win32com.client.DispatchEx("Word.Application")
            owned_app = is_new_application(word, "winword.exe")
            word.Visible = False
            word.DisplayAlerts = 0
            security_before = word.AutomationSecurity
            word.AutomationSecurity = 3
            document = word.Documents.Open(sys.argv[1], ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False)
            print(document.Content.Text)
        elif ext in (".xls", ".xlsx"):
            app_kind = "excel"
            word = win32com.client.DispatchEx("Excel.Application")
            owned_app = is_new_application(word, "excel.exe")
            word.Visible = False
            word.DisplayAlerts = False
            security_before = word.AutomationSecurity
            word.AutomationSecurity = 3
            document = word.Workbooks.Open(sys.argv[1], UpdateLinks=0, ReadOnly=True, IgnoreReadOnlyRecommended=True, AddToMru=False)
            for sheet in document.Worksheets:
                print(f"[工作表：{sheet.Name}]")
                used = sheet.UsedRange
                rows, columns = min(used.Rows.Count, 10000), min(used.Columns.Count, 256)
                for row in range(1, rows + 1):
                    print(" | ".join(str(used.Cells(row, col).Text) for col in range(1, columns + 1)))
                if used.Rows.Count > rows or used.Columns.Count > columns:
                    print("[达到提取上限，内容未完整读取]")
        else:
            app_kind = "powerpoint"
            word = win32com.client.DispatchEx("PowerPoint.Application")
            owned_app = is_new_application(word, "powerpnt.exe")
            security_before = word.AutomationSecurity
            word.AutomationSecurity = 3
            document = word.Presentations.Open(sys.argv[1], ReadOnly=True, Untitled=False, WithWindow=False)
            for slide in document.Slides:
                print(f"[幻灯片 {slide.SlideIndex}]")
                for shape in slide.Shapes:
                    if shape.HasTextFrame and shape.TextFrame.HasText:
                        print(shape.TextFrame.TextRange.Text)
                    if shape.HasTable:
                        for row in range(1, shape.Table.Rows.Count + 1):
                            print(" | ".join(shape.Table.Cell(row, col).Shape.TextFrame.TextRange.Text for col in range(1, shape.Table.Columns.Count + 1)))
    finally:
        try:
            if document is not None:
                if app_kind == "powerpoint":
                    document.Close()
                else:
                    document.Close(False)
        finally:
            try:
                if word is not None:
                    try:
                        if security_before is not None:
                            word.AutomationSecurity = security_before
                    finally:
                        # PowerPoint may share an existing application despite DispatchEx.
                        # Never quit a process which was present before this extraction.
                        if owned_app:
                            word.Quit()
            finally:
                pythoncom.CoUninitialize()
