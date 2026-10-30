"""Small, explicit task guidance. A skill never grants tool or filesystem rights."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "presets/skills"
MAX_SELECTED = 2


def _read(ident):
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", ident):
        raise ValueError("技能编号无效")
    folder = CATALOG / ident
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        instructions = (folder / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        raise ValueError("技能不存在或文件不可读：" + ident) from error
    if manifest.get("id") != ident or len(instructions.encode("utf-8")) > 12000:
        raise ValueError("技能内容无效或超过短规则上限")
    # The familiar frontmatter is part of the portable skill file, not extra instructions.
    body = instructions.split("---", 2)[-1].strip() if instructions.startswith("---\n") else instructions.strip()
    return {**manifest, "revision": hashlib.sha256(body.encode()).hexdigest(),
            "instructions": body, "available": True,
            "permissions_note": "仅装载任务规则；不会自动勾选工具、扩大文件范围或授予执行权限。"}


def listing():
    items = []
    if CATALOG.is_dir():
        for path in sorted(CATALOG.iterdir()):
            if path.is_dir() and not path.is_symlink():
                item = _read(path.name)
                items.append({k: v for k, v in item.items() if k != "instructions"})
    return {"items": items, "max_selected": MAX_SELECTED}


def detail(ident):
    return _read(ident)


def snapshot(ids):
    if not isinstance(ids, list) or len(ids) > MAX_SELECTED or any(not isinstance(v, str) for v in ids):
        raise ValueError("每次最多选择 2 个技能，技能编号须为文字列表")
    if len(set(ids)) != len(ids):
        raise ValueError("技能不能重复选择")
    return [{k: item[k] for k in ("id", "name", "version", "revision", "instructions", "requires_tools")}
            for item in map(_read, ids)]


def injection(opts):
    selected = opts.get("skill_snapshot")
    if selected is None:
        selected = snapshot(opts.get("skill_ids", []))
    if not isinstance(selected, list) or len(selected) > MAX_SELECTED:
        raise ValueError("技能快照无效")
    sections = []
    for item in selected:
        text = item.get("instructions", "")
        if (not isinstance(text, str) or len(text.encode("utf-8")) > 12000 or
                hashlib.sha256(text.encode()).hexdigest() != item.get("revision")):
            raise ValueError("技能快照已变化，请重新选择技能后发送")
        sections.append("[已选择技能：" + item["name"] + "｜版本 " + item["version"] + "]\n" + text)
    if not sections:
        return ""
    return "\n\n".join(sections) + "\n技能不增加权限。没有选中的工具不能使用；工具关闭时只可分析已提供文字，并明确未完成程序核验。"
