# Explicit public allowlist: excludes personal config, conversations and attachments.
from pathlib import Path
import hashlib
from importlib.metadata import version as distribution_version
from packaging.requirements import Requirement
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

project = Path(SPECPATH).parent
data = [(str(project / "web"), "web"), (str(project / "docs"), "docs"),
        (str(project / "plugins/examples"), "plugins/examples"),
        (str(project / "presets"), "presets"),
        (str(project / "LICENSE"), "."),
        (str(project / "app/python_runner.py"), "app"),
        (str(project / "app/python_workbench.py"), "app"),
        (str(project / "app/toolkit"), "app/toolkit")]
data += [(str(project / name), ".") for name in ("README.md", "SECURITY.md", "PRIVACY.md", "DISCLAIMER.md", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "CONTRIBUTING.md")]
binaries = []
hidden = ["app.server", "app.doc_worker", "app.plugin_worker", "app.selftest_worker", "app.tray", "app.python_lab", "app.lifecycle", "app.context",
          "app.network", "app.jobs", "app.memory", "app.knowledge", "app.resources", "app.turn_queue", "app.skills",
          "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
          "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto",
          "uvicorn.lifespan.on", "win32crypt", "win32com.client", "pythoncom", "pywintypes", "win32com.shell.shell", "win32file", "win32con", "win32clipboard", "win32job", "win32event", "win32process", "tkinter", "tkinter.filedialog"]
for package in ("rapidocr_onnxruntime", "webview", "onnxruntime", "docx", "openpyxl", "pptx", "xlrd", "pystray", "tokenizers", "pypdfium2", "pypdfium2_raw"):
    found_data, found_binaries, found_hidden = collect_all(package)
    # Package collectors can add entire dist-info directories. Handle metadata
    # below so canonical licenses and local-origin exclusions apply uniformly.
    data += [(source, target) for source, target in found_data if not any(part.endswith((".dist-info", ".egg-info")) for part in (*Path(source).parts, *Path(target).parts))]
    binaries += found_binaries
    hidden += found_hidden
hidden += collect_submodules("uvicorn") + collect_submodules("app.api") + collect_submodules("app.toolkit") + ["app.upgrades", "app.rag_query", "app.rag_chunks", "app.kb_documents", "app.downloads", "app.service_config", "app.office_worker", "app.office_operations", "app.port_manager"]
locked = []
for line in (project / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.lstrip().startswith("#"):
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
            if distribution_version(requirement.name) not in requirement.specifier:
                raise ValueError("构建依赖版本与requirements-lock.txt不同：" + requirement.name)
            locked.append(requirement.name)
for distribution in locked:
    metadata = copy_metadata(distribution)
    canonical = project / "packaging/public-licenses/numpy-2.5.4/LICENSE.txt"
    if distribution == "numpy":
        if not canonical.is_file() or hashlib.sha256(canonical.read_bytes()).hexdigest() != "a804dff0ead9fadc5293456410bcbfc32bf024be9c4513459663fb7b442d2341":
            raise ValueError("仓库内NumPy公开许可证校验失败，请在有权输出明文的构建目录重新准备官方原文")
    pdfium_license = project / "packaging/public-licenses/pypdfium2-5.14.0/CC-BY-4.0.txt"
    if distribution == "pypdfium2" and (not pdfium_license.is_file() or hashlib.sha256(pdfium_license.read_bytes()).hexdigest() != "f1b1748301cd4274f46733c218d510929661b4990855de5fc645dacd69c4371c"):
        raise ValueError("仓库内pypdfium2公开许可证校验失败，请重新准备官方原文")
    for directory, destination in metadata:
        if distribution == "numpy" and Path(directory).name != "numpy-2.5.4.dist-info":
            raise ValueError("请使用requirements-build.txt中的NumPy版本；升级版本时重新核对公开许可证")
        for item in Path(directory).rglob("*"):
            # Editable-install origins can contain local paths and are not
            # needed by the application. They are never distributed.
            if item.is_file() and item.name != "direct_url.json":
                relative = item.relative_to(directory)
                selected = canonical if distribution == "numpy" and relative.as_posix() == "licenses/LICENSE.txt" else item
                if distribution == "pypdfium2" and relative.as_posix() == "licenses/LICENSES/CC-BY-4.0.txt":
                    selected = pdfium_license
                with selected.open("rb") as stream:
                    if stream.read(32).startswith(b"%TSD-Header"):
                        raise ValueError("公开依赖元数据含保护头；请在有权输出明文的目录重新安装官方依赖")
                data.append((str(selected), str(Path(destination) / relative.parent)))
a = Analysis([str(project / "main.py")], pathex=[str(project)], binaries=binaries, datas=data,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["pytest", "matplotlib", "IPython", "notebook", "torch", "tensorflow"], noarchive=False)
# Directory data collectors must not ship developer bytecode/cache paths.
a.datas = [row for row in a.datas if "__pycache__" not in Path(row[0]).parts and Path(row[0]).suffix.lower() not in (".pyc", ".pyo")]
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalAIStudio", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(project / "web/app.ico"), version=str(project / "packaging/version.txt"))
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalAIWorker", debug=False,
             bootloader_ignore_signals=False, strip=False, upx=False, console=True,
             icon=str(project / "web/app.ico"), version=str(project / "packaging/version.txt"))
collect = COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name="LocalAIStudio")
