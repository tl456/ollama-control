"""Source-faithful Markdown sections and bounded token windows for local RAG."""
from __future__ import annotations
import bisect
import re


def token_windows(text, size=512, overlap=64):
    from .context import tokenizer, take_text
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("分段长度应为正数，重叠应小于分段长度")
    tok = tokenizer("qwen3-embedding")
    if tok:
        offsets = tok.encode(text, add_special_tokens=False).offsets
        if not offsets:
            if text:
                yield text, 0, len(text)
            return
        ends = [item[1] for item in offsets]
        start = 0
        while start < len(offsets):
            end = min(len(offsets), start + size)
            left = offsets[start][0] if start else 0
            right = len(text) if end == len(offsets) else offsets[end-1][1]
            # Prefer a paragraph boundary in the latter half, then a newline.
            floor = offsets[min(end-1, start + size//2)][0]
            boundary = text.rfind("\n\n", floor, right)
            if boundary < 0:
                boundary = text.rfind("\n", floor, right)
            if boundary > left and end != len(offsets):
                right = boundary + 1
                end = max(start + 1, bisect.bisect_right(ends, right))
            yield text[left:right], left, right
            if right == len(text):
                return
            start = max(start + 1, end - overlap)
    else:
        left = 0
        while left < len(text):
            piece = take_text(text[left:], size)
            if not piece:
                raise ValueError("分段失败，未跳过原文")
            right = left + len(piece)
            yield piece, left, right
            if right == len(text):
                return
            back = len(take_text(piece[::-1], overlap)) if overlap else 0
            left = max(left + 1, right - back)


def heading_sections(text, enabled=True):
    """Yield raw source spans; fenced code never changes the heading hierarchy."""
    if not enabled:
        if text:
            yield 0, len(text), []
        return
    sections, headings, start, offset, fence = [], [], 0, 0, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
        elif fence is None:
            match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)\r?\n?$", line)
            if match:
                if offset > start:
                    sections.append((start, offset, [title for _, title in headings]))
                level = len(match[1])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip()[:200]
                headings = [(rank, value) for rank, value in headings if rank < level]
                headings.append((level, title))
                start = offset
        offset += len(line)
    if len(text) > start:
        sections.append((start, len(text), [title for _, title in headings]))
    yield from sections


def source_sections(text):
    result = []
    for match in re.finditer(r"(?m)^\[(?:工作表|幻灯片)[^\n]{0,100}\]", text):
        first = text[match.end():match.end()+600].lstrip("\n").split("\n", 1)[0][:500]
        result.append((match.start(), match.group() + ("\n[该节首行参考] " + first if "|" in first or "\t" in first else "")))
    return result


def _context(left, headings, markers, events):
    result = ["[标题路径] " + " > ".join(title[:150] for title in headings)] if headings else []
    index = bisect.bisect_right(markers, (left, chr(0x10ffff))) - 1
    if index >= 0:
        result.append(markers[index][1])
    position = bisect.bisect_right(events, (left, chr(0x10ffff))) - 1
    if position >= 0 and events[position][1]:
        result.append(events[position][1])
    return "\n".join(result)[:1000]


def split_chunks(text, size=512, overlap=64, markdown_headers=True):
    markers = source_sections(text)
    scopes = {}
    for begin, finish, headings in heading_sections(text, markdown_headers):
        # Inherit only the active parent sections, never a previous sibling's
        # test conditions. Build offset-labelled contexts once, avoiding an
        # O(document length * chunk count) scan for every emitted chunk.
        key = tuple(headings)
        parent = next((scopes[key[:n]] for n in range(len(key)-1, -1, -1) if key[:n] in scopes), ({}, ""))
        conditions, header = dict(parent[0]), parent[1]
        def label():
            values = (["[表头参考] " + header] if header else [])
            if conditions:
                values.append("[原文条件参考] " + "；".join(list(conditions.values())[-4:]))
            return "\n".join(values)
        events, offset, previous = [(begin, label())], begin, ""
        for line in text[begin:finish].splitlines(keepends=True):
            clean = line.rstrip("\r\n")
            match = re.match(r"^\s*(型号|零件号|工况|试验条件|条件|单位|温度|频率|振幅)\s*[:：][^\n]{1,180}$", clean)
            changed = False
            if match:
                conditions[match[1]] = clean.strip()
                changed = True
            if re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", clean):
                header = previous[:500]
                changed = True
            offset += len(line)
            if changed:
                events.append((offset, label()))
            previous = clean
        scopes[key] = (conditions, header)
        for piece, left, right in token_windows(text[begin:finish], size, overlap):
            left, right = begin + left, begin + right
            yield {"text": piece, "start": left, "end": right, "heading_path": headings,
                   "source_context": _context(left, headings, markers, events)}
