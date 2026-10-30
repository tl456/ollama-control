"""Stdlib helper copied into each trusted Python task; not a fact-verification oracle."""
import hashlib
import json
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
INPUTS = json.loads((BASE / "inputs.json").read_text(encoding="utf-8"))


def _log(value):
    value["recorded_at"] = time.time()
    with (BASE / "evidence.jsonl").open("a", encoding="utf-8") as writer:
        writer.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
    return value


def _text(blob):
    if blob.startswith((b"PK\x03\x04", b"MZ", b"\xd0\xcf\x11\xe0", b"%PDF", b"\x89PNG", b"\xff\xd8\xff")):
        raise ValueError("不是纯文本；Office 可使用 INPUTS 的 text_path 提取文字，ZIP 需指定文本 member")
    encodings = ["utf-8-sig", "gb18030"]
    if blob.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    for encoding in encodings:
        try:
            value = blob.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            if "\x00" in value or sum(ord(c) < 32 and c not in "\n\t\f" for c in value) > max(1, len(value) * .01):
                continue
            return value
        except UnicodeError:
            pass
    raise ValueError("无法可靠按文本读取；请先检查格式/编码，不应把乱码当试验数据")


def verify_text(attachment_id, quote, member=None):
    """Locate literal evidence. This does NOT certify model interpretation or acceptance."""
    item = next((v for v in INPUTS if v["id"] == attachment_id), None)
    if item is None:
        raise ValueError("附件编号不在本次授权 INPUTS 中")
    if not isinstance(quote, str) or not quote or len(quote) > 6000:
        raise ValueError("quote 须为 1～6000 字符原文，不是解释或估算值")
    path = BASE / item["path"]
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != item["sha256"]:
        raise ValueError("本次输入副本已被修改，不能据此声称核对原件")
    representation = "原件文本"
    extra = {}
    if member is not None:
        if not item.get("raw_archive"):
            raise ValueError("只有 ZIP 输入可指定 member")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            selected = [v for v in entries if v.filename == member and not v.is_dir()]
            if len(selected) != 1 or selected[0].file_size > 20 * 1024 * 1024:
                raise ValueError("ZIP 条目不存在、不唯一或超过 20 MiB")
            blob = archive.read(selected[0])
        extra = {"member": member, "member_sha256": hashlib.sha256(blob).hexdigest()}
    elif item.get("raw_archive"):
        raise ValueError("ZIP 请指定确切 member；压缩包清单不是条目全文")
    elif item.get("text_path"):
        blob = (BASE / item["text_path"]).read_bytes()
        if hashlib.sha256(blob).hexdigest() != item["text_sha256"]:
            raise ValueError("提取文字副本已被修改，不能据此核对来源")
        representation = "提取文字 / " + item.get("text_method", "")
    text = _text(blob)
    quote = quote.replace("\r\n", "\n").replace("\r", "\n")
    locations, start, count = [], 0, 0
    while True:
        index = text.find(quote, start)
        if index < 0:
            break
        count += 1
        if len(locations) < 20:
            locations.append({"offset": index, "line": text.count("\n", 0, index) + 1,
                              "nearby": text[max(0, index-100):index+len(quote)+100]})
        start = index + max(1, len(quote))
    return _log({"kind": "literal_source_check", "status": "literal_found" if count else "not_found",
                 "attachment_id": attachment_id, "file": item["name"], "source_sha256": item["sha256"],
                 "representation": representation, "quote": quote, "occurrences": count, "locations": locations,
                 "semantic_status": "未自动核验样件/方向/频率/振幅/单位/预载/阶段或结论，仍须结合记录复查", **extra})


def record_calculation(label, inputs, formula, result, source_checks=None):
    """Record reproducible inputs/formula, without falsely certifying independent validation."""
    return _log({"kind": "calculation_record", "label": str(label), "inputs": inputs,
                 "formula": str(formula), "result": result, "source_checks": source_checks or [],
                 "status": "计算依据记录；非独立重算或业务语义核验，需复读结果与检查条件"})


def export_evidence(name="复核记录.json"):
    if not name or name != Path(name).name or ":" in name or "\\" in name or not name.lower().endswith(".json"):
        raise ValueError("复核记录须保存为当前目录下的普通 JSON 文件名")
    path = BASE / "evidence.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    (BASE / name).write_text(json.dumps({"note": "来源文字核对与计算依据，不是自动合格证明；受信任代码可修改本目录，记录不是防篡改审计。", "records": records},
                                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return name
