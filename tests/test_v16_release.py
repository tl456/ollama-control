"""Small synthetic release fixtures; no compiler, model download or live app data."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_v16", REPO / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

class Release16Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v16-release-synthetic-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.out = self.root / "output"
        self.payload = self.out / "LocalAIStudio"
        for name in release.PUBLIC_DOCS:
            self.write(self.source / name, "Synthetic public document: " + name)
        self.write(self.source / "main.py", "# synthetic source\n")
        self.write(self.source / "app/config.py", "VERSION = '1.6.0'\n")
        self.write(self.source / "packaging/启动.bat", "@echo off\n")
        self.write(self.source / "packaging/浏览器启动.bat", "@echo off\n")
        self.write(self.source / "web/app.ico", b"synthetic-icon")
        self.write(self.source / "plugins/examples/demo/tool.py", "def run(args, context): return {'ok': True}\n")
        self.write(self.source / "scripts/audit_public.py", (REPO / "scripts/audit_public.py").read_bytes())
        for name in ("config/secrets.json", "data/chat.json", "audit/tools.sqlite3", "models/private.bin", "runtime/private.bin", "cache/private.txt", ".git/config", "dist/private.txt", "docs/private/notes.md"):
            self.write(self.source / name, "SYNTHETIC_PRIVATE_MARKER")
        self.write(self.payload / "LocalAIStudio.exe", b"MZ-synthetic-gui-not-executable")
        self.write(self.payload / "LocalAIWorker.exe", b"MZ-synthetic-worker-not-executable")
        self.write(self.payload / "_internal/web/index.html", "<html>Synthetic UI</html>")
        seed = self.root / "seed"
        self.write(seed / "ollama/ollama.exe", b"MZ-synthetic-ollama-not-executable")
        self.write(seed / "ollama/lib/backend-a.dll", b"Synthetic backend A")
        self.write(seed / "ollama/lib/nested/backend-b.dll", b"Synthetic backend B")
        self.write(seed / "ollama/LICENSE", "Synthetic vendor license")
        self.write(seed / "python/python.exe", b"MZ-synthetic-python-not-executable")
        self.write(seed / "python/LICENSE.txt", "Synthetic Python license")
        for family in ("qwen35", "qwen3-embedding"):
            self.write(seed / "tokenizers" / family / "tokenizer.json", "{}")
            self.write(seed / "tokenizers" / family / "LICENSE", "Synthetic model license")
        config = b'{"model_format":"synthetic"}'
        weights = b"Synthetic model bytes, not real weights" * 30
        for blob in (config, weights):
            self.write(seed / "models/blobs" / ("sha256-" + hashlib.sha256(blob).hexdigest()), blob)
        descriptor = lambda blob: {"digest": "sha256:" + hashlib.sha256(blob).hexdigest(), "size": len(blob), "mediaType": "application/vnd.ollama.image.model"}
        manifest = {"config": descriptor(config), "layers": [descriptor(weights)]}
        for model in release.MODELS:
            family, tag = model.split(":")
            self.write(seed / "models/manifests/registry.ollama.ai/library" / family / tag, json.dumps(manifest))
        self.args = SimpleNamespace(output_dir=self.out, frozen_dir=None, ollama_dir=seed / "ollama",
            ollama_license=None, python_exec_dir=seed / "python", tokenizers_dir=seed / "tokenizers", models_dir=seed / "models", iscc=None)
        self.root_patch = patch.object(release, "ROOT", self.source)
        self.root_patch.start()
    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()
    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
    def prepared(self):
        result = release.prepare(self.args)
        self.assertTrue(result["prepared"])
        return json.loads((self.payload / "docs/release/bundle-manifest.json").read_text(encoding="utf-8"))
    def test_prepare_includes_audited_source_and_mit_without_private_roots(self):
        manifest = self.prepared()
        source = manifest["source_bundle"]
        with zipfile.ZipFile(self.payload / source["file"]) as archive:
            names = archive.namelist()
            self.assertIn("ollama-control/LICENSE", names)
            self.assertIn("ollama-control/plugins/examples/demo/tool.py", names)
            for name in names:
                self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", archive.read(name).decode("utf-8"))
                self.assertNotIn("/private/", name)
        self.assertTrue(release.check(self.args)["passed"])
        self.assertEqual(source["sha256"], release.digest(self.payload / source["file"]))
    def test_new_public_untracked_source_is_included(self):
        self.write(self.source / "app/new_module.py", "VALUE = 42\n")
        source = self.prepared()["source_bundle"]
        self.assertIn("app/new_module.py", [row["file"] for row in source["entries"]])
    def test_source_secret_and_protected_header_block_packaging(self):
        # Construct the test credential so the public source audit never treats
        # a literal live-looking credential inside this test as leaked data.
        for value in ("key = 'sk-" + "Z" * 30 + "'\n", b"%TSD-Header-001%Synthetic protected input"):
            self.write(self.source / "app/leak.py", value)
            with self.assertRaises(ValueError): release.source_inputs()
    def test_source_zip_reuse_rejects_changed_source(self):
        self.prepared()
        self.write(self.source / "app/config.py", "VERSION = '1.6.0'\nVALUE = 2\n")
        with self.assertRaisesRegex(ValueError, "ZIP member"):
            release.source_bundle(self.payload, [])
    def test_manifest_requires_source_archive(self):
        manifest = self.prepared()
        del manifest["source_bundle"]
        release.atomic_json(self.payload / "docs/release/bundle-manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "source ZIP"):
            release.check(self.args)
    def test_extra_runtime_or_missing_library_is_rejected(self):
        self.prepared()
        extra = self.payload / "runtime/ollama/lib/unexpected.dll"
        self.write(extra, b"extra")
        with self.assertRaises(ValueError): release.check(self.args)
        extra.unlink()
        (self.payload / "runtime/ollama/lib/nested/backend-b.dll").unlink()
        with self.assertRaises(ValueError): release.check(self.args)
    def test_portable_exact_complete_layout_crc_hash_and_independent_check(self):
        self.prepared()
        result = release.portable(self.args)
        self.assertTrue(result["crc_verified"])
        directory = self.out / "portable"
        metadata = json.loads((directory / "portable-manifest.json").read_text(encoding="utf-8"))
        with zipfile.ZipFile(directory / metadata["archive"]) as archive:
            self.assertTrue(all(name.startswith("LocalAIStudio/") for name in archive.namelist()))
            self.assertIn("LocalAIStudio/runtime/ollama/lib/nested/backend-b.dll", archive.namelist())
            for model in release.MODELS:
                family,tag=model.split(":")
                self.assertIn("LocalAIStudio/models/manifests/registry.ollama.ai/library/"+family+"/"+tag,archive.namelist())
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))
        # Verification uses the ZIP and sidecar, even if the original payload is gone.
        detached = self.root / "detached-payload"
        self.payload.rename(detached)
        self.assertTrue(release.portable_check(self.args)["passed"])
    def test_portable_refuses_existing_output(self):
        self.prepared()
        release.portable(self.args)
        with self.assertRaisesRegex(ValueError, "not empty"):
            release.portable(self.args)
    def test_zip64_structure_with_small_synthetic_threshold(self):
        self.prepared()
        # Exercise ZIP64 records without copying a multi-gigabyte fixture.
        with patch.object(zipfile,"ZIP64_LIMIT",64):
            result=release.portable(self.args)
        archive=self.out/"portable"/result["archive"]
        self.assertIn(b"PK\x06\x06",archive.read_bytes())
        self.assertTrue(release.portable_check(self.args)["passed"])
    def test_portable_tampering_is_detected(self):
        self.prepared()
        result=release.portable(self.args)
        path=self.out/"portable"/result["archive"]
        blob=bytearray(path.read_bytes());blob[100]^=1;path.write_bytes(blob)
        with self.assertRaisesRegex(ValueError, "digest"):
            release.portable_check(self.args)
    def test_zip_rejects_extra_missing_duplicate_traversal_and_wrong_hash(self):
        row={"file":"main.py","bytes":4,"sha256":hashlib.sha256(b"test").hexdigest()}
        for index,names in enumerate((["root/main.py","root/extra.txt"],["root/other.py"],["root/../main.py"])):
            path=self.root/(str(index)+".zip")
            with zipfile.ZipFile(path,"w") as archive:
                for name in names:archive.writestr(name,b"test")
            with self.assertRaises(ValueError):release.verify_zip(path,[row],"root/")
        path=self.root/"wrong-hash.zip"
        with zipfile.ZipFile(path,"w") as archive:archive.writestr("root/main.py",b"xxxx")
        with self.assertRaisesRegex(ValueError,"digest"):release.verify_zip(path,[row],"root/")
    def test_zip_crc_is_checked_not_only_file_listing(self):
        path=self.root/"crc.zip"
        with zipfile.ZipFile(path,"w",compression=zipfile.ZIP_STORED) as archive:archive.writestr("root/main.py",b"UNIQUE_SYNTHETIC_CONTENT")
        blob=path.read_bytes().replace(b"UNIQUE_SYNTHETIC_CONTENT",b"BROKEN_SYNTHETIC_CONTENT")
        self.assertEqual(len(blob),path.stat().st_size);path.write_bytes(blob)
        row={"file":"main.py","bytes":len(b"UNIQUE_SYNTHETIC_CONTENT"),"sha256":hashlib.sha256(b"UNIQUE_SYNTHETIC_CONTENT").hexdigest()}
        with self.assertRaisesRegex(ValueError,"CRC"):release.verify_zip(path,[row],"root/")

if __name__ == "__main__": unittest.main()
