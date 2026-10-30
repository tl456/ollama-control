"""Explicit release upgrade policy; code replacement is not data migration.

Public releases preserve existing data by default. test-reset is an explicit
development/testing option. No model, memory, chat or secret is read here.
"""
from __future__ import annotations
import copy
import os
import stat
import time
from pathlib import Path
from . import config

RELEASE_UPGRADE_MODE = "preserve"
SCHEMA_VERSION = 1
MODES = frozenset(("test-reset", "preserve"))


def resolve_policy(policy=None):
    value = policy if policy is not None else os.environ.get("LOCAL_AI_UPGRADE_MODE", RELEASE_UPGRADE_MODE)
    if value not in MODES:
        raise ValueError("升级策略必须为 test-reset（测试版出厂）或 preserve（保留资料）")
    return value


def _managed_config_file(path):
    boundary = (config.ROOT / "config").resolve()
    path = Path(path)
    if path.parent != config.ROOT / "config":
        raise ValueError("升级仅允许管理固定配置文件")
    for item in (config.ROOT / "config", path):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("升级不跟随配置链接")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("升级不跟随配置重解析点")
    if path.resolve().parent != boundary:
        raise ValueError("升级配置路径越界")
    return path


def preserve(previous):
    """Merge only missing defaults, retaining all saved values and unknown keys."""
    target = _managed_config_file(config.ROOT / "config/settings.json")
    marker_path = _managed_config_file(config.ROOT / "config/version.json")
    with config.LOCK:
        saved = config.read_json(target, {})
        if not isinstance(saved, dict):
            raise ValueError("原设置不是有效对象，未清空或迁移用户资料")
        merged = {**copy.deepcopy(config.DEFAULTS), **saved}
        added = sorted(set(config.DEFAULTS) - set(saved))
        config.write_json(target, merged)
        marker = {**previous, "version": config.VERSION, "schema_version": SCHEMA_VERSION,
                  "upgrade_mode": "preserve", "upgraded_at": time.time(),
                  "previous_version": previous.get("version"), "defaults_added": added}
        config.write_json(marker_path, marker)
    return {"ok": True, "reset": False, "upgraded": True, "upgrade_mode": "preserve",
            "previous_version": previous.get("version"), "defaults_added": added,
            "models_preserved": True, "runtime_preserved": True, "data_preserved": True}
