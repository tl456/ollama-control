"""Build only the explicitly listed original adapter files, never CAD data or SDK binaries."""
from pathlib import Path
import argparse
import zipfile
FILES=("manifest.json","tool.py","nx_reader.py","README.md","LICENSE")
def build(output):
    root=Path(__file__).resolve().parent
    output=Path(output)
    if output.exists():
        raise ValueError("Output already exists")
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,"x",zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path=root/name
            if path.is_symlink() or not path.is_file():raise ValueError("Invalid plugin input")
            blob=path.read_bytes()
            if blob.startswith(b"%TSD-Header"):raise ValueError("Protected source bytes")
            archive.writestr(name,blob)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():raise ValueError("ZIP CRC failed")
    return output
if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("output")
    print(build(parser.parse_args().output))
