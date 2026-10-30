"""Scan only public source or Git-tracked files; never print matching values.

This is a heuristic pre-publication check, not proof that arbitrary files are safe.
Runtime/configuration directories are not opened by the source allowlist.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path

SOURCE_DIRS = {"app", "web", "docs", "presets", "plugins", "packaging", "scripts", "tests", ".github"}
SOURCE_FILES = {"main.py", "README.md", "LICENSE", "requirements.txt", "requirements-build.txt",
                "requirements-lock.txt", "启动.bat", "浏览器启动.bat", ".gitignore", ".gitattributes",
                "SECURITY.md", "PRIVACY.md", "DISCLAIMER.md", "CONTRIBUTING.md", "CHANGELOG.md",
                "THIRD_PARTY_NOTICES.md"}
PRIVATE_PARTS = {"config", "data", "models", "runtime", "logs", "audit", "cache", "imported", ".venv", "venv",
                 "dist", "build", "release", "__pycache__", ".git"}
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".json", ".md", ".txt", ".html", ".css", ".svg",
                 ".bat", ".ps1", ".iss", ".spec", ".yml", ".yaml", ".toml", ".xml"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".pfx", ".p12", ".pyc", ".pyo"}
RULES = {
    "github_credential": re.compile(r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"),
    "api_secret": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "personal_windows_path": re.compile(r"(?i)\b[a-z]:[\\/]+users[\\/]+([^\\/\s\"'<>]+)"),
    "personal_unix_path": re.compile(r"/(?:Users|home)/([^/\s\"'<>]+)"),
}
PLACEHOLDERS = re.compile(r"(?i)(synthetic|fixture|placeholder|example|not.?real|your[-_]|dummy|mock)")
PUBLIC_USERS = {"public", "default", "user", "username", "test", "testuser", "runner", "example", "synthetic"}


def allowed_name(name: str) -> bool:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or any(p.casefold() in PRIVATE_PARTS for p in path.parts):
        return False
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES or path.name.startswith(".env"):
        return False
    return name in SOURCE_FILES or (len(path.parts) > 1 and path.parts[0] in SOURCE_DIRS)


def inspect_bytes(name: str, payload: bytes) -> list[dict]:
    findings = []
    if not allowed_name(name):
        return [{"file": name, "line": None, "type": "file_outside_public_source_allowlist"}]
    if payload.startswith(b"%TSD-Header"):
        return [{"file": name, "line": None, "type": "protected_file_header"}]
    path = Path(name)
    is_text = path.suffix.casefold() in TEXT_SUFFIXES or name in SOURCE_FILES
    if not is_text:
        return findings
    try:
        text = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return [{"file": name, "line": None, "type": "invalid_utf8_public_text"}]
    if "\x00" in text:
        findings.append({"file": name, "line": None, "type": "nul_in_public_text"})
    for kind, pattern in RULES.items():
        for match in pattern.finditer(text):
            if kind.startswith("personal_") and match.group(1).casefold() in PUBLIC_USERS:
                continue
            if kind in {"api_secret", "github_credential"} and PLACEHOLDERS.search(match.group()):
                continue
            findings.append({"file": name, "line": text.count("\n", 0, match.start()) + 1, "type": kind})
    if path.suffix.casefold() == ".py":
        try:
            ast.parse(text, filename=name)
        except SyntaxError as error:
            findings.append({"file": name, "line": error.lineno, "type": "invalid_python_syntax"})
    return findings


def source_names(root: Path) -> list[str]:
    names = [name for name in SOURCE_FILES if (root / name).is_file()]
    for directory in SOURCE_DIRS:
        start = root / directory
        if not start.exists():
            continue
        for path in start.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Source links are not scanned: " + relative)
            if path.is_file() and not any(part.casefold() in PRIVATE_PARTS for part in path.relative_to(root).parts):
                names.append(relative)
    return sorted(set(names))


def index_payloads(root: Path) -> tuple[dict[str, bytes], list[dict]]:
    """Read the exact staged blobs, never private paths or working-tree substitutes."""
    result = subprocess.run(["git", "-C", str(root), "ls-files", "--stage", "-z"], check=True, capture_output=True)
    entries, findings, seen = [], [], set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        header, encoded_name = raw.split(b"\t", 1)
        mode, object_id, stage = header.decode("ascii").split()
        name = encoded_name.decode("utf-8")
        if not allowed_name(name):
            findings.append({"file": name, "line": None, "type": "file_outside_public_source_allowlist"})
            continue
        if mode not in {"100644", "100755"} or stage != "0" or name.casefold() in seen:
            findings.append({"file": name, "line": None, "type": "index_link_conflict_or_duplicate"})
            continue
        seen.add(name.casefold())
        entries.append((name, object_id))
    blobs = {}
    if entries:
        result = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
            input=("\n".join(value for _, value in entries) + "\n").encode("ascii"), check=True, capture_output=True)
        stream = io.BytesIO(result.stdout)
        for name, expected in entries:
            actual, kind, size = stream.readline().decode("ascii").strip().split()
            if actual != expected or kind != "blob":
                raise ValueError("Unexpected staged Git object")
            payload = stream.read(int(size))
            if len(payload) != int(size) or stream.read(1) != b"\n":
                raise ValueError("Incomplete staged Git object")
            blobs[name] = payload
        if stream.read():
            raise ValueError("Unexpected trailing staged-object data")
    return blobs, findings


def scan(root: Path, *, tracked: bool = False) -> dict:
    root = root.resolve()
    staged, findings = index_payloads(root) if tracked else ({}, [])
    names = sorted(staged) if tracked else source_names(root)
    records = []
    for required in ("README.md", "LICENSE", "main.py", "app/config.py"):
        if required not in names:
            findings.append({"file": required, "line": None, "type": "required_public_source_missing"})
    for name in names:
        if not allowed_name(name):
            findings.append({"file": name, "line": None, "type": "file_outside_public_source_allowlist"})
            continue
        path = root / name
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            findings.append({"file": name, "line": None, "type": "source_link"})
            continue
        payload = staged[name] if tracked else path.read_bytes()
        findings.extend(inspect_bytes(name, payload))
        records.append({"file": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return {"passed": not findings, "tracked_files": tracked, "reads_exact_staged_blobs": tracked, "files_checked": len(records),
            "findings": findings, "files": records,
            "limitations": "Heuristic source audit only; no claim of detecting every secret or all enterprise encryption."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true", help="Check the Git index instead of the public source allowlist")
    parser.add_argument("--output", type=Path, help="Optional private report path outside the repository")
    args = parser.parse_args()
    report = scan(args.root, tracked=args.tracked)
    if args.output:
        if args.output.resolve().is_relative_to(args.root.resolve()):
            raise ValueError("Keep audit reports outside the public source tree")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
