"""Split a verified portable ZIP into bounded release assets; no runtime app imports.

The Windows merge script uses only PowerShell/.NET, validates each part and the
whole reconstructed ZIP, and never starts the application.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

SLICE_BYTES = 1_900_000_000
FORMAT = "LocalAIStudioSplitV1"
MANIFEST = "portable-parts.json"

MERGE_CMD = '@echo off\r\nsetlocal\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Merge-Portable.ps1"\r\nset "merge_result=%ERRORLEVEL%"\r\necho.\r\npause\r\nexit /b %merge_result%\r\n'
MERGE_PS1 = r'''# MIT License. Copyright (c) 2026 tl456.
# Offline byte merge only. No downloads, installation or program launch.
param([string]$OutputDirectory = $PSScriptRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$maximumPartBytes = [long]1900000000
$mergeSuccess = $false
$temporaryCreated = $false
$temporary = $null
$outputStream = $null
$wholeHash = $null
function Require([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function LeafName([string]$Name) {
    return ($Name.Length -gt 0 -and $Name.Length -le 200 -and
        [IO.Path]::GetFileName($Name) -ceq $Name -and
        $Name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -lt 0 -and
        -not $Name.EndsWith('.') -and -not $Name.EndsWith(' ') -and
        -not [IO.Path]::IsPathRooted($Name) -and
        $Name -notmatch '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)')
}
function FileDigest([string]$Path) {
    $stream = $null
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $hasher.Dispose()
    }
}
try {
    $inputDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
    $manifestPath = [IO.Path]::Combine($inputDirectory, 'portable-parts.json')
    $manifestFile = Get-Item -LiteralPath $manifestPath
    Require ($manifestFile.Length -gt 0 -and $manifestFile.Length -le 2097152) '分卷清单大小无效。'
    Require (-not ($manifestFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) '分卷清单不能是链接。'
    $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    Require ($manifest.format -ceq 'LocalAIStudioSplitV1') '分卷清单格式不支持。'
    $archiveName = [string]$manifest.archive
    Require ((LeafName $archiveName) -and $archiveName.EndsWith('.zip', [StringComparison]::OrdinalIgnoreCase)) '目标 ZIP 名称无效。'
    $parts = @($manifest.parts)
    Require ($parts.Count -ge 1 -and $parts.Count -le 9999) '分卷数量无效。'
    $chunkBytes = [long]$manifest.chunk_bytes
    Require ($chunkBytes -gt 0 -and $chunkBytes -le $maximumPartBytes) '分卷大小超过限制。'
    Require ([string]$manifest.archive_sha256 -cmatch '^[0-9a-f]{64}$') '完整 ZIP 摘要无效。'
    $expectedTotal = [long]0
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $part = $parts[$index]
        $expectedName = $archiveName + ('.{0:D3}' -f ($index + 1))
        Require ((LeafName ([string]$part.name)) -and [string]$part.name -ceq $expectedName) '分卷名称或顺序无效。请保留下载时的原文件名。'
        Require ([string]$part.sha256 -cmatch '^[0-9a-f]{64}$') '分卷摘要无效。'
        $partBytes = [long]$part.bytes
        Require ($partBytes -gt 0 -and $partBytes -le $chunkBytes) '分卷字节数无效。'
        if ($index -lt $parts.Count - 1) { Require ($partBytes -eq $chunkBytes) '中间分卷大小不完整。' }
        $partPath = [IO.Path]::Combine($inputDirectory, $expectedName)
        $partFile = Get-Item -LiteralPath $partPath
        Require (-not $partFile.PSIsContainer -and -not ($partFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) '分卷必须是普通文件。'
        Require ($partFile.Length -eq $partBytes) ('分卷不完整：' + $expectedName)
        $expectedTotal += $partBytes
    }
    Require ($expectedTotal -eq [long]$manifest.archive_bytes) '分卷总大小与清单不一致。'
    $targetDirectory = [IO.Path]::GetFullPath($OutputDirectory)
    [void][IO.Directory]::CreateDirectory($targetDirectory)
    $target = [IO.Path]::Combine($targetDirectory, $archiveName)
    Require (-not [IO.File]::Exists($target) -and -not [IO.Directory]::Exists($target)) '目标 ZIP 已存在。请保留或移走已有文件后重试，不会覆盖。'
    $temporary = $target + '.partial-' + [Guid]::NewGuid().ToString('N')
    $outputStream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $temporaryCreated = $true
    $wholeHash = [Security.Cryptography.SHA256]::Create()
    $buffer = New-Object byte[] (4 * 1024 * 1024)
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $part = $parts[$index]
        Write-Host ('正在校验并合并 {0}/{1}：{2}' -f ($index + 1), $parts.Count, $part.name)
        $inputStream = $null
        $partHash = [Security.Cryptography.SHA256]::Create()
        $copied = [long]0
        try {
            $inputStream = [IO.File]::Open([IO.Path]::Combine($inputDirectory, [string]$part.name), [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outputStream.Write($buffer, 0, $read)
                [void]$partHash.TransformBlock($buffer, 0, $read, $buffer, 0)
                [void]$wholeHash.TransformBlock($buffer, 0, $read, $buffer, 0)
                $copied += $read
            }
            [void]$partHash.TransformFinalBlock([byte[]]@(), 0, 0)
            $actual = [BitConverter]::ToString($partHash.Hash).Replace('-', '').ToLowerInvariant()
            Require ($copied -eq [long]$part.bytes -and $actual -ceq [string]$part.sha256) ('分卷校验失败，请重新下载：' + $part.name)
        } finally {
            if ($null -ne $inputStream) { $inputStream.Dispose() }
            $partHash.Dispose()
        }
    }
    [void]$wholeHash.TransformFinalBlock([byte[]]@(), 0, 0)
    $mergedHash = [BitConverter]::ToString($wholeHash.Hash).Replace('-', '').ToLowerInvariant()
    Require ($mergedHash -ceq [string]$manifest.archive_sha256) '重组后的完整 ZIP 摘要不一致。'
    $outputStream.Flush($true)
    $outputStream.Dispose()
    $outputStream = $null
    Write-Host '正在从磁盘回读核对完整 ZIP，请稍候……'
    Require ((Get-Item -LiteralPath $temporary).Length -eq $expectedTotal -and (FileDigest $temporary) -ceq [string]$manifest.archive_sha256) '落盘后的 ZIP 无法通过摘要校验；未生成最终文件。'
    [IO.File]::Move($temporary, $target)
    $mergeSuccess = $true
    Write-Host ('合并完成：' + $target)
    Write-Host '请自行解压 ZIP，再进入 LocalAIStudio 文件夹运行启动.bat。本脚本不会自动启动程序。'
} catch {
    Write-Host ('合并失败：' + $_.Exception.Message) -ForegroundColor Red
} finally {
    if ($null -ne $outputStream) { $outputStream.Dispose() }
    if ($null -ne $wholeHash) { $wholeHash.Dispose() }
    if (-not $mergeSuccess -and $temporaryCreated -and [IO.File]::Exists($temporary)) {
        # Delete only the unique temporary file created by this invocation.
        [IO.File]::Delete($temporary)
    }
}
if ($mergeSuccess) { exit 0 } else { exit 1 }
'''

HELP = r"""完整离线解压包：分卷合并说明

1. 下载本版本的全部 .zip.001、.zip.002……分卷，以及 portable-parts.json、
   Merge-Portable.ps1、合并便携包.cmd、portable-parts-SHA256SUMS.txt。
   放在同一文件夹，保持文件名不变；不要单独解压某个分卷。
2. 确保磁盘至少能再放下一份完整 ZIP；后续解压还需程序和模型占用的空间。
3. 双击“合并便携包.cmd”。Windows 自带 PowerShell/.NET 即可，无需 Python、
   无需联网。脚本按清单顺序逐片校验并合并，再回读验证完整 ZIP 的 SHA-256。
4. 显示完成后，自行解压完整 ZIP，打开 LocalAIStudio 文件夹，运行启动.bat。
   合并不会自动安装或启动程序，也不会覆盖已经存在的完整 ZIP。
5. 缺片、改名、大小或摘要不一致时会停止，只清除本次创建的不完整输出；
   原始分卷保留，可重新下载提示的分卷后重试。

CMD 只为本次合并的 PowerShell 进程设置执行策略，不更改机器或用户级策略。
如企业策略禁止脚本运行，请遵循所在环境管理要求；不修改该策略。
合并脚本仅访问清单指定的同目录分卷，不搜索本机文件，不发送网络请求。
请从同一正式发行页获取清单与脚本；摘要用于检验传输完整性。

可选：在 PowerShell 中运行
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\Merge-Portable.ps1 -OutputDirectory D:\\Downloads
可将合并后的 ZIP 放到另一个有足够空间的文件夹。
"""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction()), "Expected a regular file")
    with path.open("rb") as stream:
        require(not stream.read(32).startswith(b"%TSD-Header"), "Protected input/output bytes; no decryption attempted")
    return path


def leaf(name):
    return (isinstance(name, str) and 0 < len(name) <= 200 and name not in {".", ".."} and
            not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name) and not name.endswith((".", " ")) and
            not re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name))


def digest(path):
    with regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_manifest(directory):
    path = regular(Path(directory) / MANIFEST)
    require(path.stat().st_size <= 2 * 1024 * 1024, "Parts manifest is too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict) and value.get("format") == FORMAT, "Unsupported parts manifest")
    name = value.get("archive", "")
    require(leaf(name) and name.lower().endswith(".zip"), "Invalid archive name")
    require(re.fullmatch(r"[0-9a-f]{64}", str(value.get("archive_sha256", ""))), "Invalid whole archive hash")
    parts, size = value.get("parts"), value.get("chunk_bytes")
    require(isinstance(parts, list) and 1 <= len(parts) <= 9999 and type(size) is int and 0 < size <= SLICE_BYTES, "Invalid part count or size")
    total = 0
    for index, part in enumerate(parts, 1):
        require(isinstance(part, dict), "Invalid part metadata")
        expected = name + f".{index:03d}"
        require(part.get("name") == expected and leaf(part.get("name")), "Unexpected part name or order")
        require(type(part.get("bytes")) is int and 0 < part["bytes"] <= size and
                (index == len(parts) or part["bytes"] == size), "Invalid part size")
        require(re.fullmatch(r"[0-9a-f]{64}", str(part.get("sha256", ""))), "Invalid part hash")
        total += part["bytes"]
    require(type(value.get("archive_bytes")) is int and total == value["archive_bytes"], "Whole archive size differs")
    return value


def verify_split(directory):
    directory = Path(directory)
    manifest = read_manifest(directory)
    whole = hashlib.sha256()
    for part in manifest["parts"]:
        path = regular(directory / part["name"])
        require(path.stat().st_size == part["bytes"], "Part size differs: " + part["name"])
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(4 * 1024 * 1024):
                hasher.update(block)
                whole.update(block)
        require(hasher.hexdigest() == part["sha256"], "Part digest differs: " + part["name"])
    require(whole.hexdigest() == manifest["archive_sha256"], "Reassembled ZIP digest differs")
    return {"passed": True, "parts": len(manifest["parts"]), "bytes": manifest["archive_bytes"],
            "sha256": manifest["archive_sha256"], "reassembled_file_written": False}


def split(archive, directory, *, chunk_bytes=SLICE_BYTES, expected_sha256=None):
    archive = regular(archive).absolute()
    directory = Path(directory).absolute()
    require(leaf(archive.name) and archive.name.lower().endswith(".zip") and zipfile.is_zipfile(archive), "Input must be a readable ZIP/ZIP64 archive")
    require(type(chunk_bytes) is int and 0 < chunk_bytes <= SLICE_BYTES, "Part size exceeds release asset limit")
    require(expected_sha256 is None or re.fullmatch(r"[0-9a-f]{64}", expected_sha256), "Invalid expected archive SHA-256")
    require(not directory.is_symlink() and not (hasattr(directory, "is_junction") and directory.is_junction()), "Output directory cannot be a link")
    directory.mkdir(parents=True, exist_ok=True)
    require(not any(directory.iterdir()), "Output directory must be empty; existing files are never overwritten")
    before = archive.stat()
    require(before.st_size > 0 and (before.st_size + chunk_bytes - 1) // chunk_bytes <= 9999, "ZIP size or part count is invalid")
    parts = []
    whole = hashlib.sha256()
    with archive.open("rb") as source:
        remaining = before.st_size
        while remaining:
            name = archive.name + f".{len(parts) + 1:03d}"
            target = directory / name
            size = min(chunk_bytes, remaining)
            copied = 0
            hasher = hashlib.sha256()
            with target.open("xb") as output:
                while copied < size:
                    block = source.read(min(4 * 1024 * 1024, size - copied))
                    require(block, "Source ZIP ended early")
                    output.write(block)
                    hasher.update(block)
                    whole.update(block)
                    copied += len(block)
            parts.append({"name": name, "bytes": copied, "sha256": hasher.hexdigest()})
            remaining -= copied
        require(not source.read(1), "Source ZIP grew while splitting")
    after = archive.stat()
    require(before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns, "Source ZIP changed while splitting")
    archive_hash = whole.hexdigest()
    require(expected_sha256 is None or archive_hash == expected_sha256, "Source ZIP differs from the verified portable archive")
    manifest = {"format": FORMAT, "archive": archive.name, "archive_bytes": before.st_size,
                "archive_sha256": archive_hash, "chunk_bytes": chunk_bytes, "parts": parts}
    (directory / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # BOM is required for Windows PowerShell 5.1 Chinese text, not for the CMD.
    (directory / "Merge-Portable.ps1").write_bytes(b"\xef\xbb\xbf" + MERGE_PS1.replace("\n", "\r\n").encode("utf-8"))
    (directory / "合并便携包.cmd").write_bytes(MERGE_CMD.encode("ascii"))
    (directory / "分卷使用说明.txt").write_bytes(b"\xef\xbb\xbf" + HELP.replace("\n", "\r\n").encode("utf-8"))
    result = verify_split(directory)  # independently reread on-disk part bytes
    checksums = [(p.name, digest(p)) for p in sorted(directory.iterdir()) if p.is_file()]
    (directory / "portable-parts-SHA256SUMS.txt").write_text("".join(h + "  " + n + "\n" for n, h in checksums), encoding="utf-8")
    return {**result, "manifest": MANIFEST, "windows_merge": "合并便携包.cmd", "part_bytes_limit": SLICE_BYTES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("split-portable", "verify-split"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256", help="Optional SHA-256 from portable-manifest.json")
    args = parser.parse_args()
    if args.command == "split-portable":
        require(args.archive is not None, "split-portable requires --archive")
        result = split(args.archive, args.output_dir, expected_sha256=args.expected_sha256)
    else:
        result = verify_split(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
