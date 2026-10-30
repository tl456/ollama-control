from ...config import ROOT, read_json, settings, write_json
from ..common import resolve_args, allowed_path, READ_LOCK, tool_values
import hashlib

def read_file(args):
    from ... import attachments
    args = resolve_args(args)
    _, target = allowed_path(args["root_id"], args["relative_path"])
    if not target.is_file():
        raise ValueError("读取目标必须是普通文件")
    if target.stat().st_size > settings()["upload_mb"] * 1024 * 1024:
        raise ValueError("文件超过附件大小限制")
    info = target.stat()
    stamp = hashlib.sha256((str(target) + str(info.st_mtime_ns) + str(info.st_size) + tool_values("read_file")["tool_read_mode"]).encode()).hexdigest()
    with READ_LOCK:
        cache_path = ROOT / "cache/file-reads.json"
        cache = read_json(cache_path, {})
        meta = None
        if stamp in cache and not args.get("refresh"):
            try:
                meta = attachments.load(cache[stamp])
            except (KeyError, ValueError, OSError):
                pass
        if meta is None:
            meta = attachments.upload(target.name, target.read_bytes(), tool_values("read_file")["tool_read_mode"])
            meta = attachments.extract(meta["id"], original_path=target)
            if meta["status"] == "ready":
                cache[stamp] = meta["id"]
                write_json(cache_path, dict(list(cache.items())[-64:]))
    if meta["status"] != "ready":
        raise ValueError(meta.get("error", "文件无法读取"))
    offset, limit = args.get("offset", 0), args.get("max_chars", 6000)
    text = meta["text"]
    if args.get("mode") == "full":
        offset = 0
    line_meta = {}
    if args.get("mode") == "lines":
        lines = text.splitlines(keepends=True)
        start_line = args.get("start_line", 1)
        offset = sum(len(line) for line in lines[:start_line-1])
        section = "".join(lines[start_line-1:start_line-1+args.get("line_count", 200)])
        selected = section[:limit]
        partial_line = len(section) > limit and not selected.endswith(("\n", "\r"))
        if partial_line and "\n" in selected:
            selected = selected[:selected.rfind("\n")+1]
            partial_line = False
        returned_lines = len(selected.splitlines())
        line_meta = {"start_line": start_line, "returned_lines": returned_lines, "total_lines": len(lines),
                     "next_line": start_line + returned_lines if not partial_line and offset + len(selected) < len(text) else None,
                     "partial_line": partial_line,
                     "resume_hint": "单行超长，请用 mode=segment 和 next_offset 继续，不跳过该行剩余文字" if partial_line else ""}
    else:
        selected = text[offset:offset + limit]
    return {"path": str(target), "root_id": args["root_id"], "relative_path": args["relative_path"], "attachment_id": meta["id"],
            "name": target.name, "method": meta["method"], "text": selected, "offset": offset,
            "full_chars": len(text), "returned_chars": len(selected), "truncated": offset + len(selected) < len(text),
            "next_offset": offset + len(selected) if offset + len(selected) < len(text) else None,
            "complete": offset == 0 and len(selected) == len(text), "sha256": meta["sha256"], **line_meta,
            "note": "附件参考资料；范围和下一段位置如上，原文件未修改"}
