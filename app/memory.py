"""One freely editable, bounded Markdown memory with atomic serialized writes.

Full replacement is last-successful-save-wins. A content digest remains in read
responses for compatibility/diagnostics, never as a caller's write prerequisite.
Application writes share this service; external editors do not share its lock.
"""
from __future__ import annotations
import hashlib
import os
import stat
import threading
import uuid
from .config import ROOT, settings

LOCK = threading.RLock()
PATH = ROOT / "data/memory/memory.md"
HARD_LIMIT = 256 * 1024
TOOL_TEXT_LIMIT = 200000
INJECTION_HEADER = "[工作空间长期记忆｜可由用户或记忆工具维护]\n"
TRUNCATION_NOTICE = "\n[记忆超过当前注入预算，请精简；可调用 memory_read 查看全文]"
TOOLS = [
    {"id": "memory_read", "name": "读取长期记忆", "description": "读取工作空间共用的一份长期记忆 Markdown；不访问其他文件。改写前可查看当前内容，无须回传版本号。",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"id": "memory_update", "name": "维护长期记忆", "description": "自由维护同一份记忆：text 默认完整覆盖，可改写、合并、删改或重排，空文本清空；mode=append 可追加。无须版本号或逐次确认；全文按最后成功保存生效。只记明确长期偏好/事实，不存密钥、附件全文、临时问题或猜测。",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "maxLength": TOOL_TEXT_LIMIT, "description": "完整 Markdown；空文本可清空。追加时填写新增内容。"},
         "mode": {"type": "string", "enum": ["replace", "append"], "description": "默认 replace 完整覆盖；append 追加。"}},
         "required": ["text"], "additionalProperties": False}},
]


class MemoryOperationError(ValueError):
    """A user-facing reason, without pretending every failure is a revision race."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def token_usage(text, cfg=None):
    """Count the exact memory injection text under the declared local tokenizer.

    This is a Qwen text-token convention, never a claim about every cloud API's
    chat template. Empty memory injects no header and consumes zero text tokens.
    """
    from .context import tokenizer, estimate
    cfg = cfg or settings()
    rendered = INJECTION_HEADER + text if text else ""
    tok = tokenizer()
    used = len(tok.encode(rendered, add_special_tokens=False).ids) if tok is not None else estimate(rendered)
    limit = int(cfg["memory_tokens"])
    over = used > limit
    return {"used_tokens": used, "token_limit": limit, "token_budget": limit,
            "counting_method": "qwen3.5_tokenizer" if tok is not None else "conservative_estimate",
            "tokens_exact": tok is not None, "includes_injection_wrapper": True, "over_budget": over, "over_limit": over,
            "warning": "现有记忆超过当前Token预算，原文件已保留；聊天暂按预算截取，请精简后保存。" if over else ""}


def _validated_text(text):
    if not isinstance(text, str) or "\x00" in text:
        raise MemoryOperationError("memory_invalid_text", "记忆需要有效 Markdown 文本，不能含空字符")
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MemoryOperationError("memory_invalid_text", "记忆文本无法编码为 UTF-8，请检查异常字符") from error


def normalize_update_arguments(args):
    """Strict tool arguments; tolerate only the legacy, now-ignored revision key.

    New model definitions never expose revision. Keeping this adapter separate
    also permits old conversation tool requests without broadening the schema.
    """
    if not isinstance(args, dict) or set(args) - {"text", "mode", "revision"}:
        raise MemoryOperationError("memory_invalid_arguments", "记忆参数只支持 text 和可选 mode，不接受文件路径")
    if "text" not in args:
        raise MemoryOperationError("memory_invalid_arguments", "请填写记忆 text；空文本可清空")
    _validated_text(args["text"])
    if len(args["text"]) > TOOL_TEXT_LIMIT:
        raise MemoryOperationError("memory_limit", "记忆工具文本超过长度上限，请先精简")
    mode = args.get("mode", "replace")
    if mode not in ("replace", "append"):
        raise MemoryOperationError("memory_invalid_mode", "记忆 mode 只支持 replace 或 append")
    return {"text": args["text"], "mode": mode}


def _check_path():
    expected = ROOT / "data/memory/memory.md"
    if PATH.absolute() != expected.absolute():
        raise MemoryOperationError("memory_unsafe_path", "记忆只能写入工作空间固定文件")
    cursor, boundary = PATH.absolute(), ROOT.absolute()
    while True:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise MemoryOperationError("memory_unsafe_path", "记忆路径不能是链接或联接点")
        try:
            attrs = getattr(cursor.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attrs = 0
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise MemoryOperationError("memory_unsafe_path", "记忆路径不能包含重解析点")
        if cursor == boundary:
            break
        if cursor.parent == cursor or not cursor.is_relative_to(boundary):
            raise MemoryOperationError("memory_unsafe_path", "记忆路径超出工作空间")
        cursor = cursor.parent
    if PATH.exists() and not PATH.is_file():
        raise MemoryOperationError("memory_unsafe_path", "记忆目标必须是固定的普通文件")


def _read_blob(allow_oversize=False):
    _check_path()
    try:
        with PATH.open("rb") as stream:
            blob = stream.read(HARD_LIMIT + 1)
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise MemoryOperationError("memory_read_failed", "记忆读取失败，请检查目录权限或文件状态") from error
    if len(blob) > HARD_LIMIT:
        if allow_oversize:
            # A valid full replacement/clear must still be able to repair an
            # externally enlarged file, without first reading it all into RAM.
            return None
        raise MemoryOperationError("memory_read_limit", "记忆文件超过硬上限，请完整改写或清空以缩小")
    return blob


def _decode(blob):
    try:
        return blob.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MemoryOperationError("memory_invalid_encoding", "记忆不是有效 UTF-8；可以完整改写或清空修复") from error


def read():
    cfg = settings()
    with LOCK:
        blob = _read_blob()
        text = _decode(blob)
        return {"text": text, "revision": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob), "enabled": cfg["memory_enabled"], "ai_write": cfg["memory_ai_write"],
                "max_kb": cfg["memory_max_kb"], "token_budget": cfg["memory_tokens"],
                "write_policy": "last_successful_save_wins", "revision_required": False,
                **token_usage(text, cfg)}


def read_for_tool():
    """The model needs the text, not a long compatibility digest to copy back."""
    result = read()
    result.pop("revision", None)
    return result


def _write_result(blob, mode, changed, usage):
    return {"ok": True, "revision": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
            "mode": mode, "changed": changed, "write_policy": "last_successful_save_wins",
            **usage, "note": "长期记忆内容未变化" if not changed else "长期记忆已清空" if not blob
                    else "已追加到长期记忆" if mode == "append" else "长期记忆已更新"}


def update(text, revision=None, ai=False, mode="replace"):
    """Write the single MD freely; revision is accepted but deliberately ignored.

    A replace preserves exactly the submitted UTF-8 text, including empty text.
    Append uses the latest text while locked, adding a newline only if neither
    side supplies one. No semantic merge or invisible duplicate removal occurs.
    """
    submitted = _validated_text(text)
    if mode not in ("replace", "append"):
        raise MemoryOperationError("memory_invalid_mode", "记忆 mode 只支持 replace 或 append")
    with LOCK:
        cfg = settings()
        if ai and (not cfg["memory_enabled"] or not cfg["memory_ai_write"]):
            raise MemoryOperationError("memory_ai_disabled", "AI 记忆写入已关闭")
        limit = min(HARD_LIMIT, int(cfg["memory_max_kb"]) * 1024)
        if len(submitted) > limit:
            raise MemoryOperationError("memory_limit", "记忆超过设置的大小上限，请先精简")
        current = _read_blob(allow_oversize=mode == "replace")
        if mode == "append":
            prior = _decode(current)
            separator = "\n" if prior and text and not prior.endswith("\n") and not text.startswith("\n") else ""
            # Do not normalize existing bytes/BOM/newlines during an append.
            blob = current + separator.encode("utf-8") + submitted
        else:
            blob = submitted
        if len(blob) > limit:
            raise MemoryOperationError("memory_limit", "追加后的记忆超过设置的大小上限，请先精简或完整改写")
        usage = token_usage(_decode(blob), cfg)
        if usage["over_budget"]:
            raise MemoryOperationError("memory_token_limit",
                f"记忆含注入说明共 {usage['used_tokens']} Token，超过 {usage['token_limit']} Token 上限；请精简后保存，原文件未改变。", usage)
        if current == blob and PATH.is_file():
            return _write_result(blob, mode, False, usage)
        temporary = PATH.with_name("memory-" + uuid.uuid4().hex + ".tmp")
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            _check_path()
            with temporary.open("xb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, PATH)
        except OSError as error:
            raise MemoryOperationError("memory_write_failed", "记忆保存失败，请检查目录权限或磁盘空间") from error
        finally:
            temporary.unlink(missing_ok=True)
        return _write_result(blob, mode, True, usage)


def injection():
    cfg = settings()
    if not cfg["memory_enabled"]:
        return ""
    from .context import take_text, estimate
    value = read()
    if not value["text"]:
        return ""
    if not value["over_budget"]:
        return INJECTION_HEADER + value["text"]
    # Legacy/external edits and a lowered setting retain the full original file.
    # Count the warning too; no silent write or automatic destructive compaction.
    available = max(0, int(cfg["memory_tokens"]) - estimate(INJECTION_HEADER + TRUNCATION_NOTICE))
    selected = take_text(value["text"], available)
    while selected and estimate(INJECTION_HEADER + selected + TRUNCATION_NOTICE) > int(cfg["memory_tokens"]):
        selected = take_text(selected, max(0, estimate(selected) - 1))
    return INJECTION_HEADER + selected + TRUNCATION_NOTICE if selected else ""


def definitions(writable=True):
    cfg = settings()
    if not cfg["memory_enabled"]:
        return []
    return [{"type": "function", "function": {"name": t["id"], "description": t["description"] +
                (f" 含注入说明的全文最多{cfg['memory_tokens']} Token；超限不会覆盖旧内容，请精简后完整重写。" if t["id"] == "memory_update" else ""), "parameters": t["parameters"]}}
            for t in TOOLS if t["id"] == "memory_read" or (cfg["memory_ai_write"] and writable)]
