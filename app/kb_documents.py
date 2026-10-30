"""Managed Markdown source I/O; identifiers, never user names, select paths."""
from __future__ import annotations
import os
import re
import stat
import uuid
from pathlib import Path

MAX_CHARS = 2_000_000
MAX_BYTES = 8 * 1024 * 1024


def validate_text(text):
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("知识库正文需要有效文本，不能含空字符")
    try:
        blob = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("知识库正文无法编码为UTF-8") from error
    if len(text) > MAX_CHARS or len(blob) > MAX_BYTES:
        raise ValueError("单份知识资料最多200万字符/8MiB，请拆分保存；未静默截断")
    return blob


def name(value):
    value = str(value or "").strip()
    if not value or len(value) > 100 or any(c in value for c in "\x00\r\n/\\"):
        raise ValueError("请输入1–100字符的文档名称，不含目录分隔符")
    return value if value.casefold().endswith(".md") else value + ".md"


def source_path(root, kb, doc):
    if any(not re.fullmatch(r"[0-9a-f]{32}", str(v)) for v in (kb, doc)):
        raise ValueError("知识文档编号无效")
    root = Path(root).absolute()
    path = root / "data/knowledge/sources" / kb / (doc + ".md")
    cursor = path
    while True:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("知识文档自管路径不能包含链接或联接点")
        if cursor.exists() and getattr(cursor.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("知识文档自管路径不能包含重解析点")
        if cursor == root:
            break
        if not cursor.is_relative_to(root):
            raise ValueError("知识文档路径越界")
        cursor = cursor.parent
    return path


def read(root, kb, doc):
    path = source_path(root, kb, doc)
    with path.open("rb") as stream:
        blob = stream.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise ValueError("知识文档超过读取上限")
    text = blob.decode("utf-8-sig")
    validate_text(text)
    return text


def write(root, kb, doc, text):
    blob = validate_text(text)
    path = source_path(root, kb, doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    source_path(root, kb, doc)
    temporary = path.with_name(doc + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def remove(root, kb, doc):
    source_path(root, kb, doc).unlink(missing_ok=True)
