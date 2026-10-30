"""Frozen-build smoke checks; synthetic fixtures only, no directory scanning."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

if __name__ == "__main__":
    from . import attachments, tools
    from .config import DEFAULTS, ROOT, VERSION, save_settings, write_json
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation
    from pptx.util import Inches
    from PIL import Image, ImageDraw, ImageFont
    import tkinter

    checks = {}
    if (ROOT / "config/settings.json").exists():
        raise ValueError("Self-test requires a fresh, isolated LOCAL_AI_ROOT; refusing to overwrite existing settings")
    checks["folder_picker_runtime"] = tkinter.TkVersion
    write_json(ROOT / "config/settings.json", DEFAULTS)
    doc = Document(); doc.add_paragraph("Synthetic Word 2026")
    buf = io.BytesIO(); doc.save(buf)
    doc_meta = attachments.upload("synthetic.docx", buf.getvalue())
    result = attachments.extract(doc_meta["id"])
    assert result["status"] == "ready", result
    checks["word_native"] = "Synthetic Word" in result["text"]
    if "--office" in sys.argv:
        doc_meta["read_mode"] = "office"; attachments.save(doc_meta)
        result = attachments.extract(doc_meta["id"])
        checks["word_com"] = result["status"] == "ready" and "Synthetic Word" in result["text"]
        if not checks["word_com"]:
            checks["word_com_error"] = result.get("error")
    book = Workbook(); book.active["A1"] = "Synthetic Excel 2026"
    buf = io.BytesIO(); book.save(buf)
    meta = attachments.upload("synthetic.xlsx", buf.getvalue()); result = attachments.extract(meta["id"])
    checks["excel_native"] = "Synthetic Excel" in result.get("text", "")
    if "--office" in sys.argv:
        meta["read_mode"] = "office"; attachments.save(meta); result = attachments.extract(meta["id"])
        checks["excel_com"] = result["status"] == "ready" and "Synthetic Excel" in result.get("text", "")
        if not checks["excel_com"]:
            checks["excel_com_error"] = result.get("error")
    ppt = Presentation(); slide = ppt.slides.add_slide(ppt.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text = "Synthetic PowerPoint"
    buf = io.BytesIO(); ppt.save(buf)
    meta = attachments.upload("synthetic.pptx", buf.getvalue()); result = attachments.extract(meta["id"])
    checks["ppt_native"] = "Synthetic PowerPoint" in result.get("text", "")
    if "--office" in sys.argv:
        meta["read_mode"] = "office"; attachments.save(meta); result = attachments.extract(meta["id"])
        checks["ppt_com"] = result["status"] == "ready" and "Synthetic PowerPoint" in result.get("text", "")
        if not checks["ppt_com"]:
            checks["ppt_com_error"] = result.get("error")
    im = Image.new("RGB", (900, 180), "white")
    ImageDraw.Draw(im).text((20, 45), "LOCAL TEST 2026", fill="black", font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 50))
    buf = io.BytesIO(); im.save(buf, "PNG")
    meta = attachments.upload("synthetic.png", buf.getvalue()); result = attachments.extract(meta["id"])
    checks["local_ocr"] = "2026" in result.get("text", "")
    checks["guide"] = (Path(__file__).resolve().parents[1] / "docs/软件教程.md").is_file()
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        for file in (Path(__file__).resolve().parents[1] / "plugins/examples/time_demo").glob("*"):
            if file.is_file():
                archive.write(file, file.name)
    tools.import_plugin(package.getvalue())
    save_settings({"plugins": ["time_demo"]})
    tools.save_tool_configuration("time_demo", {"format": "clock"})
    checks["plugin_worker"] = "time" in tools.execute("time_demo", {}, ["time_demo"])
    from . import python_lab
    tools.save_tool_configuration("python_run", {"enabled": True, "timeout": 2, "output_kb": 4})
    lab = python_lab.execute({"code": 'import statistics\nprint(statistics.mean([2,4,6]))\nprint(input())', "stdin": "SYNTHETIC-IN\n"})
    checks["python_offline_io"] = lab["status"] == "finished" and "4" in lab["stdout"] and "SYNTHETIC-IN" in lab["stdout"]
    checks["python_timeout"] = "超时" in python_lab.execute({"code": "while True: pass"}).get("note", "")
    from . import context, memory, knowledge
    checks["offline_tokenizer"] = context.tokenizer("qwen35") is not None and context.tokenizer("qwen3-embedding") is not None
    checks["unknown_dat"] = attachments.extract(attachments.upload("synthetic.dat", "合成纯文本 DAT".encode())["id"])["status"] == "ready"
    memory.update("# 合成记忆\n优先使用毫米。", memory.read()["revision"])
    checks["fixed_memory"] = "毫米" in memory.injection()
    memory.update("# 合成完整重写\n只保留这条。", "obsolete-revision", ai=True)
    memory.update("追加合成内容", mode="append", ai=True)
    checks["memory_revision_free_full_replace"] = memory.read()["text"] == "# 合成完整重写\n只保留这条。\n追加合成内容"
    snapshot = context.runtime_state({"provider": "local"})
    checks["runtime_snapshot_prompt_match"] = snapshot["local_time"] in context.runtime_context({"provider": "local"}, snapshot)[0]["content"] and not snapshot["tool_called"]
    from .api import admin as api_admin, memory as api_memory, knowledge as api_knowledge
    checks["api_adapter_modules"] = all(callable(module.build_router) for module in (api_admin, api_memory, api_knowledge))
    kb = knowledge.create("合成冻结测试库")
    checks["knowledge_sqlite"] = kb["config"]["model"] == "qwen3-embedding:0.6b"
    from . import skills, store, turn_queue
    chosen = skills.snapshot(["stiffness-excel"])
    checks["builtin_skill_and_snapshot"] = bool(chosen and "自主" in skills.injection({"skill_snapshot": chosen}))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("synthetic-A.dat", "group=A;axis=Z;stiffness=123.45 N/mm\n")
    raw = attachments.extract(attachments.upload("synthetic.zip", archive.getvalue())["id"])
    scope = {"attachment_ids": [raw["id"]], "python_run_ids": []}
    listing = tools.execute("read_attachment", {"attachment_id": raw["id"], "action": "list"}, ["read_attachment"], scope)
    checks["scoped_zip_attachment"] = listing["total"] == 1 and listing["items"][0]["member"] == "synthetic-A.dat"
    tools.save_tool_configuration("python_run", {"enabled": True, "timeout": 10, "output_kb": 8})
    program = '''from pathlib import Path
import zipfile
from openpyxl import Workbook, load_workbook
from workbench import verify_text, record_calculation, export_evidence
proof=verify_text(INPUTS[0]['id'],'stiffness=123.45 N/mm',member='synthetic-A.dat')
assert proof['occurrences']==1
assert verify_text(INPUTS[0]['id'],'stiffness=999 N/mm',member='synthetic-A.dat')['status']=='not_found'
w=Workbook();w.active.append(['来源','实测N/mm','状态']);w.active.append(['synthetic-A.dat / group A',123.45,'条件待确认']);w.save('synthetic-result.xlsx')
r=load_workbook('synthetic-result.xlsx',read_only=True);assert r.active['B2'].value==123.45;r.close()
export_evidence('synthetic-evidence.json')
print('FROZEN_XLSX_REOPEN_OK')
'''
    generated = python_lab.execute({"code": program}, scope)
    checks["python_attachment_evidence_and_excel_reopen"] = generated["status"] == "finished" and "FROZEN_XLSX_REOPEN_OK" in generated["stdout"]
    checks["python_generated_files_listed"] = {"synthetic-result.xlsx", "synthetic-evidence.json"} <= {p["name"] for p in generated["files"]}
    with store.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    checks["persistent_turn_queue_schema"] = "chat_turns" in tables
    from . import service_config
    checks["port_roles_v15"] = (service_config.GATEWAY_PORT, service_config.CHAT_PORT, service_config.EMBED_PORT) == (11436, 11437, 11438)
    toolkit = Path(__file__).resolve().parent / "toolkit"
    checks["modular_tool_packages"] = all((toolkit / name / "README.md").is_file() for name in ("system_time", "list_directory", "file_info", "search_files", "read_file", "read_attachment", "python_run"))
    before_memory = memory.read()["text"]
    try:
        memory.update("SYNTHETIC " * 1200)
        checks["memory_token_hard_limit"] = False
    except memory.MemoryOperationError as error:
        checks["memory_token_hard_limit"] = error.code == "memory_token_limit" and memory.read()["text"] == before_memory
    doc = knowledge.create_markdown(kb["id"], "synthetic.md", "# 合成设备\n首版信息")
    knowledge.update_document_source(kb["id"], doc["id"], {"name": "synthetic-renamed.md", "text": "# 合成设备\n第二版信息"})
    source = knowledge.document_source(kb["id"], doc["id"])
    checks["markdown_source_edit_lifecycle"] = source["name"] == "synthetic-renamed.md" and "第二版" in source["text"]
    knowledge.delete_document(kb["id"], doc["id"])
    output = json.dumps({"version": VERSION, "root": str(ROOT), "frozen": bool(getattr(sys, "frozen", False)), "synthetic_only": True, "checks": checks}, ensure_ascii=False, indent=2)
    (ROOT / "logs/frozen-selftest.json").write_text(output, encoding="utf-8")
    print(output, flush=True)
    if not all(v for k, v in checks.items() if not k.endswith("_error")):
        raise SystemExit(1)
