"""Build public Windows distributions from explicit public runtime/model inputs.

No application import, configuration scan, model inference, download or installer
execution is performed. The compiler is invoked only by freeze/installer/all.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("qwen3.5:9b", "qwen3.5:4b", "qwen3-embedding:0.6b")
PUBLIC_DOCS = ("README.md", "LICENSE", "SECURITY.md", "PRIVACY.md", "DISCLAIMER.md", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "CONTRIBUTING.md")
PRIVATE = frozenset(("config", "data", "logs", "cache", "plugins", ".git", ".venv", "venv"))
SLICE_BYTES = 1_900_000_000  # Below GitHub Releases' 2 GiB per-asset limit.
NUMPY_SHA256 = "a804dff0ead9fadc5293456410bcbfc32bf024be9c4513459663fb7b442d2341"


def version():
    tree = ast.parse((ROOT / "app/config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id == "VERSION" for n in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+", value):
                return value
    raise ValueError("app/config.py must declare a numeric VERSION")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path, boundary):
    """Reject links and protected headers without attempting decryption."""
    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    require(path.is_relative_to(boundary), "Input path leaves its declared directory")
    cursor = path
    while True:
        require(not cursor.is_symlink() and not (hasattr(cursor, "is_junction") and cursor.is_junction()), "Linked build input/output is not accepted")
        if cursor == boundary:
            break
        cursor = cursor.parent
    require(path.is_file(), "Required public file missing: " + path.name)
    with path.open("rb") as stream:
        require(not stream.read(32).startswith(b"%TSD-Header"), "Protected public build file; use an authorized plain build directory")
    return path


def files(directory):
    directory = Path(directory).absolute()
    require(directory.is_dir() and not directory.is_symlink() and not (hasattr(directory, "is_junction") and directory.is_junction()), "Public input directory missing or linked")
    result = []
    for current, folders, names in os.walk(directory, followlinks=False):
        for name in folders:
            child = Path(current) / name
            require(not child.is_symlink() and not (hasattr(child, "is_junction") and child.is_junction()), "Linked public input directory")
        folders[:] = [n for n in folders if n != "__pycache__"]
        for name in names:
            path = Path(current) / name
            if path.suffix.lower() not in (".pyc", ".pyo"):
                result.append(regular(path, directory))
    return sorted(result)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_read(path):
    path = regular(path, Path(path).parent)
    require(path.stat().st_size <= 4 * 1024 * 1024, "Manifest is unexpectedly large")
    def reject(value):
        raise ValueError("Non-finite manifest constant: " + value)
    value = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject)
    require(isinstance(value, dict), "Manifest must be an object")
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "Linked metadata output")
    temporary = path.with_name(path.name + ".tmp")
    require(not temporary.exists(), "Temporary output already exists; inspect before retrying")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def clean_payload(payload):
    require(payload.is_dir(), "Freeze the program before preparing the complete payload")
    for path in files(payload):
        relative = path.relative_to(payload)
        require(relative.parts[0].casefold() not in PRIVATE, "Payload contains runtime/private data; use a fresh frozen output")
        require(path.name.casefold() not in ("auth.json", "secrets.json", ".env"), "Credential-like file in public payload")


def copy_public(source, source_root, destination, payload, records, expected=None):
    source = regular(source, source_root)
    destination = Path(destination)
    require(destination.is_relative_to(payload), "Copy destination outside payload")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        regular(destination, payload)
        actual = digest(source)
        require(digest(destination) == actual, "Existing output differs; do not overwrite an unknown file")
    else:
        temporary = destination.with_name(destination.name + ".part")
        require(not temporary.exists() and not temporary.is_symlink(), "Partial output already exists; inspect it first")
        hasher = hashlib.sha256()
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while block := reader.read(4 * 1024 * 1024):
                writer.write(block)
                hasher.update(block)
        actual = hasher.hexdigest()
        if expected is not None and actual != expected:
            raise ValueError("Public model digest mismatch; partial output retained for inspection")
        temporary.replace(destination)
    require(expected is None or actual == expected, "Public model digest mismatch")
    records.append({"file": destination.relative_to(payload).as_posix(), "bytes": destination.stat().st_size, "sha256": actual})


def freeze(args):
    require(os.name == "nt" and sys.version_info[:2] == (3, 12), "Release freezing requires Windows Python 3.12")
    out = args.output_dir.absolute()
    require(out != ROOT and not ROOT.is_relative_to(out), "Choose a separate build output directory")
    require(not (out / "LocalAIStudio").exists(), "Frozen output already exists; choose a fresh --output-dir")
    canonical = regular(ROOT / "packaging/public-licenses/numpy-2.5.4/LICENSE.txt", ROOT)
    require(digest(canonical) == NUMPY_SHA256, "Canonical NumPy license digest differs")
    for name in PUBLIC_DOCS:
        regular(ROOT / name, ROOT)
    require("'" + version() + "'" in (ROOT / "packaging/version.txt").read_text(encoding="utf-8"), "Update the Windows version resource before freezing")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--distpath", str(out), "--workpath", str(ROOT / "build/pyinstaller"), str(ROOT / "packaging/LocalAIStudio.spec")], cwd=ROOT, check=True)
    for name in ("LocalAIStudio.exe", "LocalAIWorker.exe"):
        regular(out / "LocalAIStudio" / name, out)
    return {"frozen": True, "directory": "LocalAIStudio", "version": version()}


def prepare(args):
    payload = (args.frozen_dir or args.output_dir / "LocalAIStudio").absolute()
    clean_payload(payload)
    for name in ("LocalAIStudio.exe", "LocalAIWorker.exe"):
        regular(payload / name, payload)
    inputs = {name: Path(getattr(args, name)).absolute() for name in ("ollama_dir", "python_exec_dir", "tokenizers_dir", "models_dir")}
    for source in inputs.values():
        require(source.is_dir() and not payload.is_relative_to(source) and not source.is_relative_to(payload), "Input and payload directories must be separate")
    records = [{"file": name, "bytes": (payload / name).stat().st_size, "sha256": digest(payload / name)}
               for name in ("LocalAIStudio.exe", "LocalAIWorker.exe")]
    ollama = inputs["ollama_dir"]
    require((ollama / "lib").is_dir(), "Supply the complete official Ollama directory, including lib")
    selected = [regular(ollama / "ollama.exe", ollama), *files(ollama / "lib")]
    for path in selected:
        copy_public(path, ollama, payload / "runtime/ollama" / path.relative_to(ollama), payload, records)
    vendor_license = args.ollama_license or next((p for p in (ollama / "LICENSE", ollama / "LICENSE.txt") if p.is_file()), None)
    require(vendor_license is not None, "Supply the official Ollama LICENSE with --ollama-license when it is absent from the vendor directory")
    copy_public(vendor_license, Path(vendor_license).parent, payload / "docs/release/Ollama-LICENSE.txt", payload, records)
    python = inputs["python_exec_dir"]
    for name in ("python.exe", "LICENSE.txt"):
        regular(python / name, python)
    for path in files(python):
        relative = path.relative_to(python)
        require(relative.parts[0].casefold() not in PRIVATE and path.name.casefold() not in ("auth.json", "secrets.json", ".env"), "Python input contains non-public data")
        copy_public(path, python, payload / "runtime/python-exec" / relative, payload, records)
    tokenizer = inputs["tokenizers_dir"]
    for family in ("qwen35", "qwen3-embedding"):
        folder = tokenizer / family
        for name in ("tokenizer.json", "LICENSE"):
            regular(folder / name, tokenizer)
        for path in files(folder):
            require(path.name.casefold() not in ("auth.json", "secrets.json", ".env"), "Tokenizer input contains non-public data")
            copy_public(path, tokenizer, payload / "runtime/tokenizers" / path.relative_to(tokenizer), payload, records)
    model_root = inputs["models_dir"]
    layers, manifests, seen = [], [], set()
    for model in MODELS:
        family, tag = model.split(":", 1)
        relative = Path("models/manifests/registry.ollama.ai/library") / family / tag
        source = model_root / Path(*relative.parts[1:])
        manifest = json_read(source)
        require(isinstance(manifest.get("config"), dict) and isinstance(manifest.get("layers"), list) and manifest["layers"], "Invalid public model manifest")
        copy_public(source, model_root, payload / relative, payload, manifests)
        for entry in [manifest["config"], *manifest["layers"]]:
            require(isinstance(entry, dict) and re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("digest", ""))) and type(entry.get("size")) is int and entry["size"] >= 0, "Invalid public layer size/digest")
            key = entry["digest"].replace(":", "-")
            source = model_root / "blobs" / key
            require(source.stat().st_size == entry["size"], "Public model layer size mismatch")
            if entry.get("mediaType") == "application/vnd.ollama.image.license":
                copy_public(source, model_root, payload / "docs/release/model-licenses" / (family + "-" + tag + ".txt"), payload, records, entry["digest"].split(":", 1)[1])
            if key in seen:
                continue
            seen.add(key)
            copy_public(source, model_root, payload / "models/blobs" / key, payload, layers, entry["digest"].split(":", 1)[1])
    top_copies = [(ROOT / name, name) for name in PUBLIC_DOCS]
    top_copies += [(ROOT / "packaging/启动.bat", "启动.bat"), (ROOT / "packaging/浏览器启动.bat", "浏览器启动.bat"), (ROOT / "web/app.ico", "app.ico")]
    for source, relative in top_copies:
        copy_public(source, ROOT, payload / relative, payload, records)
    metadata = {"version": version(), "prepared_at": datetime.now(timezone.utc).isoformat(), "models": list(MODELS),
                "runtime_files": records, "model_layers": layers, "model_manifests": manifests,
                "runtime_file_count": len(selected), "all_provided_ollama_libraries_retained": True,
                "source_paths_recorded": False, "private_data_included": False,
                "origin_verification": "Inputs must be obtained and signature-verified from official publishers by the builder; layer SHA checks alone do not establish provenance.",
                "installer_slice_bytes": SLICE_BYTES, "license": "MIT; bundled components retain their original licenses"}
    atomic_json(payload / "docs/release/bundle-manifest.json", metadata)
    generated = args.output_dir.absolute() / "build-metadata"
    generated.mkdir(parents=True, exist_ok=True)
    includes = []
    for row in [*layers, *manifests]:
        relative = row["file"].replace("/", "\\")
        parent = str(PurePosixPath(row["file"]).parent).replace("/", "\\")
        flags = "ignoreversion uninsneveruninstall onlyifdoesntexist"
        if row["bytes"] > 64 * 1024 * 1024:
            flags += " nocompression"
        includes.append(f'Source: "{{#FrozenDir}}\\{relative}"; DestDir: "{{app}}\\{parent}"; Flags: {flags}')
    (generated / "model-files.iss").write_text("\n".join(includes) + "\n", encoding="utf-8-sig")
    atomic_json(generated / "build.json", {"version": version(), "model_layer_count": len(layers), "model_manifest_count": len(manifests), "installer_slice_bytes": SLICE_BYTES})
    return {"prepared": True, "version": version(), "model_layers": len(layers), "models": list(MODELS), "ollama_files": len(selected), "input_paths_recorded": False}


def check(args):
    payload = (args.frozen_dir or args.output_dir / "LocalAIStudio").absolute()
    clean_payload(payload)
    manifest = json_read(payload / "docs/release/bundle-manifest.json")
    require(manifest.get("version") == version() and manifest.get("models") == list(MODELS), "Prepared payload version/model catalogue differs")
    require(manifest.get("private_data_included") is False and manifest.get("installer_slice_bytes") == SLICE_BYTES, "Payload declarations differ")
    declared = manifest.get("runtime_files", []) + manifest.get("model_layers", []) + manifest.get("model_manifests", [])
    names = set()
    for row in declared:
        name = row.get("file", "")
        rel = PurePosixPath(name)
        require(isinstance(name, str) and rel.parts and rel.as_posix() == name and not rel.is_absolute() and ".." not in rel.parts and "\\" not in name and ":" not in name and name.casefold() not in names, "Invalid/duplicate public manifest path")
        names.add(name.casefold())
        path = regular(payload.joinpath(*rel.parts), payload)
        require(type(row.get("bytes")) is int and path.stat().st_size == row["bytes"] and digest(path) == row.get("sha256"), "Prepared payload differs: " + name)
    for top in ("runtime", "models"):
        actual = {p.relative_to(payload).as_posix().casefold() for p in files(payload / top)}
        require(actual == {name for name in names if name.startswith(top + "/")}, "Prepared runtime/model set changed")
    return {"passed": True, "version": version(), "verified_public_files": len(declared), "model_count": len(MODELS), "installer_slice_bytes": SLICE_BYTES}


def installer(args):
    checked = check(args)
    compiler = args.iscc or shutil.which("ISCC.exe") or shutil.which("iscc")
    require(compiler and Path(compiler).is_file(), "Supply the official Inno Setup compiler with --iscc")
    payload = (args.frozen_dir or args.output_dir / "LocalAIStudio").absolute()
    metadata = args.output_dir.absolute() / "build-metadata"
    require((metadata / "model-files.iss").is_file(), "Run prepare before installer")
    output = args.output_dir.absolute() / "installer"
    output.mkdir(parents=True, exist_ok=True)
    require(not any(output.iterdir()), "Installer output is not empty; use a fresh output directory")
    subprocess.run([str(compiler), "/DAppVersion=" + version(), "/DFrozenDir=" + str(payload), "/DBuildMetadataDir=" + str(metadata), "/DReleaseDir=" + str(output), str(ROOT / "packaging/installer.iss")], cwd=ROOT, check=True)
    generated = []
    for path in sorted(output.iterdir()):
        regular(path, output)
        require(path.stat().st_size < 2 * 1024 ** 3, "Release asset reaches GitHub's 2 GiB limit")
        generated.append({"file": path.name, "bytes": path.stat().st_size, "sha256": digest(path)})
    require(any(row["file"].endswith(".exe") for row in generated) and any(row["file"].endswith(".bin") for row in generated), "Complete spanned installer outputs missing")
    (output / "SHA256SUMS.txt").write_text("\n".join(row["sha256"] + "  " + row["file"] for row in generated) + "\n", encoding="utf-8")
    return {"installer_built": True, "payload_check": checked, "files": generated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "prepare", "check", "installer", "all"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--frozen-dir", type=Path)
    parser.add_argument("--ollama-dir", type=Path)
    parser.add_argument("--ollama-license", type=Path)
    parser.add_argument("--python-exec-dir", type=Path)
    parser.add_argument("--tokenizers-dir", type=Path)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--iscc")
    args = parser.parse_args()
    if args.command in ("prepare", "all"):
        require(all(getattr(args, key) for key in ("ollama_dir", "python_exec_dir", "tokenizers_dir", "models_dir")), "prepare/all require four explicit public input directories")
    results = []
    if args.command in ("freeze", "all"):
        results.append(freeze(args))
    if args.command in ("prepare", "all"):
        results.append(prepare(args))
    if args.command == "check":
        results.append(check(args))
    if args.command in ("installer", "all"):
        results.append(installer(args))
    print(json.dumps({"passed": True, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(json.dumps({"passed": False, "error": str(error), "note": "No user data, downloads, inference or installer execution is performed."}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
