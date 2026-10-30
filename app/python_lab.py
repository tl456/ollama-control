"""Trusted local Python, not a security sandbox. Separate runtime and per-run I/O."""
from __future__ import annotations
import json
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote
import psutil
from .config import ROOT, read_json, write_json
from .store import uid

LOCK = threading.RLock()
RUNS = {}
OWNERS = {}
INTERNAL_FILES = {"run.json", "experiment.py", "stdin.txt", "stdout.txt", "stderr.txt", "inputs.json", "workbench.py"}
SCHEMA = {"type": "object", "properties": {
    "enabled": {"type": "boolean", "default": True, "title": "启用受信任 Python 执行", "description": "功能默认可用，但需在对话中勾选。完整 Python 可访问当前 Windows 用户权限内的资源；独立环境并非安全沙箱。"},
    "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10, "title": "运行超时（秒）", "description": "超时终止本次实验进程树，不影响 Ollama。"},
    "output_kb": {"type": "integer", "minimum": 1, "maximum": 64, "default": 20, "title": "输出上限（KiB）", "description": "stdout/stderr 超限终止，避免无限 print 积累。"}}, "required": ["enabled", "timeout", "output_kb"], "additionalProperties": False}

def config():
    from .toolkit.common import configuration_override
    override = configuration_override("python_run")
    if override is not None:
        return {"enabled": True, "timeout": 10, "output_kb": 20, **override}
    return {"enabled": True, "timeout": 10, "output_kb": 20, **read_json(ROOT / "config/tools.json", {}).get("python_run", {}), **read_json(ROOT / "config/tools/python_run.json", {})}

def active_ids():
    with LOCK:
        return set(RUNS)

def stop(ident=None, owner=None):
    with LOCK:
        selected = list(RUNS.items()) if ident is None else [(ident, RUNS.get(ident))]
        if owner is not None:
            selected = [(key, process) for key, process in selected if OWNERS.get(key) == owner]
    for _, process in selected:
        if process is None:
            continue
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            parent.kill()
        except psutil.Error:
            pass

def interpreter():
    embedded = ROOT / "runtime/python-exec/python.exe"
    if embedded.is_file():
        return str(embedded)
    bundled = Path(sys.executable).parent / "runtime/python-exec/python.exe"
    if getattr(sys, "frozen", False) and bundled.is_file():
        return str(bundled)
    if not getattr(sys, "frozen", False):
        return sys.executable
    raise ValueError("离线 Python 运行时缺失，请重新安装完整包")

def folder(ident):
    if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
        raise ValueError("实验编号无效")
    return ROOT / "data/python_runs" / ident

def prepare_inputs(target, args, scope):
    from . import attachments
    scope = scope or {}
    allowed = list(dict.fromkeys(scope.get("attachment_ids", [])))
    selected = args.get("attachment_ids", allowed)
    if not isinstance(selected, list) or any(ident not in allowed for ident in selected):
        raise ValueError("Python 输入只能选择当前对话已发送附件；不能使用其他对话的编号")
    if len(selected) > 20 or len(set(selected)) != len(selected):
        raise ValueError("一次最多复制 20 个不重复附件，请用 attachment_ids 明确选择")
    candidates, total = [], 0
    for ident in selected:
        meta, source = attachments.raw_source(ident)
        total += source.stat().st_size + len(meta.get("text", "").encode("utf-8"))
        candidates.append((meta, source))
    if total > 100 * 1024 * 1024:
        raise ValueError("Python 输入副本总量超过 100 MiB，请用 attachment_ids 只选相关附件；未用聊天截断片段替代")
    inputs = target / "inputs"
    inputs.mkdir()
    records = []
    for meta, source in candidates:
        dest = inputs / (meta["id"] + source.suffix.lower())
        shutil.copyfile(source, dest)
        with dest.open("rb") as reader:
            digest = hashlib.file_digest(reader, "sha256").hexdigest()
        if digest != meta["sha256"]:
            raise ValueError("附件副本 SHA-256 变化，请重新上传；未执行代码")
        record = {"id": meta["id"], "name": meta["name"], "path": dest.relative_to(target).as_posix(),
                  "bytes": dest.stat().st_size, "sha256": digest, "raw_archive": bool(meta.get("raw_archive"))}
        if not meta.get("raw_archive") and meta.get("text"):
            text = meta["text"]
            extracted = inputs / (meta["id"] + ".extracted.txt")
            extracted.write_text(text, encoding="utf-8", newline="\n")
            record.update(text_path=extracted.relative_to(target).as_posix(), text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                          text_method=meta.get("method", ""))
        records.append(record)
    write_json(target / "inputs.json", records)
    previous = args.get("previous_run_id")
    if previous:
        if previous not in scope.get("python_run_ids", []):
            raise ValueError("previous_run_id 不属于当前对话已有 Python 结果")
        source = folder(previous)
        meta = read_json(source / "run.json", {})
        destination = target / "previous"
        destination.mkdir()
        count, total = 0, 0
        for item in meta.get("files", []):
            name = item.get("name", "")
            path = source / name
            if (not name or name != Path(name).name or ":" in name or "\\" in name or
                    path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024):
                continue
            total += path.stat().st_size
            if total > 100 * 1024 * 1024 or count >= 50:
                raise ValueError("前次输出副本超过 50 文件/100 MiB，请分次处理")
            shutil.copyfile(path, destination / name)
            count += 1
    return records


def execute(args, scope=None):
    cfg = config()
    if not cfg["enabled"]:
        raise ValueError("请先在工具工作空间启用受信任 Python，并了解它不是安全沙箱")
    code, stdin = args["code"], args.get("stdin", "")
    ident = uid()
    target = folder(ident)
    target.mkdir(parents=True)
    meta = {"id": ident, "created": time.time(), "code": code, "stdin": stdin, "status": "preparing", "trusted_mode": True,
            "inputs": [], "previous_run_id": args.get("previous_run_id")}
    write_json(target / "run.json", meta)
    try:
        records = prepare_inputs(target, args, scope)
    except Exception as error:
        meta.update(status="failed", note=str(error)[:300], files=[])
        write_json(target / "run.json", meta)
        raise
    meta.update(status="running", inputs=records)
    write_json(target / "run.json", meta)
    # Keep actual user-supplied snippets as task data, never append to app sources.
    (target / "experiment.py").write_text(code, encoding="utf-8")
    (target / "stdin.txt").write_text(stdin, encoding="utf-8")
    runner = Path(__file__).with_name("python_runner.py")
    shutil.copyfile(Path(__file__).with_name("python_workbench.py"), target / "workbench.py")
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    process = None
    reason = None
    with LOCK:
        if RUNS:
            raise ValueError("已有 Python 实验运行，请先停止或等待")
        with (target / "stdout.txt").open("wb") as out, (target / "stderr.txt").open("wb") as err:
            process = subprocess.Popen([interpreter(), "-I", "-B", "-u", str(runner), str(target / "experiment.py"), str(target / "stdin.txt")],
                                       cwd=target, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        RUNS[ident] = process
        from .jobs import CURRENT_JOB
        OWNERS[ident] = CURRENT_JOB.get()
    try:
        while process.poll() is None:
            if time.monotonic() - started > cfg["timeout"]:
                reason = "运行超时，已停止"
            elif sum((target / name).stat().st_size for name in ("stdout.txt", "stderr.txt")) > cfg["output_kb"] * 1024:
                reason = "输出超过上限，已停止"
            else:
                try:
                    if psutil.Process(process.pid).memory_info().rss > 512 * 1024 * 1024:
                        reason = "实验进程内存超过 512 MiB，已停止（采样保护，非硬沙箱限制）"
                except psutil.Error:
                    pass
            if reason:
                stop(ident)
                break
            time.sleep(.04)
        process.wait(timeout=5)
    finally:
        with LOCK:
            RUNS.pop(ident, None)
            OWNERS.pop(ident, None)
    limit = cfg["output_kb"] * 1024
    outputs = []
    for name in ("stdout.txt", "stderr.txt"):
        path = target / name
        with path.open("rb") as reader:
            outputs.append(reader.read(limit).decode("utf-8", errors="replace"))
        if path.stat().st_size > limit:
            with path.open("r+b") as writer:
                writer.truncate(limit)
    stdout, stderr = outputs
    generated = []
    for path in target.iterdir():
        if path.is_file() and not path.is_symlink() and path.name not in INTERNAL_FILES and not path.name.endswith((".exe", ".dll", ".bat", ".cmd", ".ps1", ".com", ".scr")):
            if path.stat().st_size > 20 * 1024 * 1024:
                continue
            with path.open("rb") as reader:
                digest = hashlib.file_digest(reader, "sha256").hexdigest()
            generated.append({"name": path.name, "bytes": path.stat().st_size, "sha256": digest,
                              "url": "/api/python/runs/" + ident + "/file?name=" + quote(path.name)})
            if len(generated) == 50:
                break
    meta.update(status="finished" if process.returncode == 0 and not reason else "failed", exit_code=process.returncode,
                seconds=round(time.monotonic() - started, 3), stdout=stdout, stderr=stderr, note=reason, files=generated,
                evidence_note="evidence.jsonl 的文字核对只证明来源出现，不等于条件匹配；计算记录需复查业务含义。",
                next_step="检查 exit_code/stdout/stderr；可用 previous_run_id 复读生成文件，文件只有实际生成后才可下载。")
    write_json(target / "run.json", meta)
    return {k: v for k, v in meta.items() if k not in ("code", "stdin")}

def listing(page=1, page_size=10):
    root = ROOT / "data/python_runs"
    root.mkdir(exist_ok=True)
    records = [read_json(p / "run.json") for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / "run.json").is_file()]
    records = sorted((v for v in records if v), key=lambda x: x["created"], reverse=True)
    page_size = max(1, min(30, page_size))
    page = max(1, min(page, max(1, (len(records) + page_size - 1) // page_size)))
    return {"items": records[(page - 1) * page_size:page * page_size], "page": page, "page_size": page_size, "total": len(records), "active": list(active_ids())}
