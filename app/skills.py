"""Portable text-only skill management; skills never grant tool rights."""
from __future__ import annotations
import hashlib
import io
import json
import re
import threading
import uuid
import zipfile
from pathlib import Path
from .config import ROOT, write_json
CATALOG = Path(__file__).resolve().parents[1] / "presets/skills"
MAX_SELECTED = 2
MAX_BYTES = 12000
LOCK = threading.RLock()

def _id(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}",value):
        raise ValueError("技能编号无效")
    return value

def _user(ident):
    return ROOT/"data/skills"/(_id(ident)+".json")

def _validate(raw):
    if not isinstance(raw,dict):
        raise ValueError("技能需要对象")
    result={key:str(raw.get(key,"")).strip() for key in ("id","name","description","version","instructions")}
    _id(result["id"])
    if not result["name"] or len(result["name"])>100 or len(result["description"])>1000:
        raise ValueError("名称不能为空且最多100字，简介最多1000字")
    if not result["instructions"] or len(result["instructions"].encode("utf-8"))>MAX_BYTES:
        raise ValueError("规则不能为空且不得超过12000字节")
    result["version"]=result["version"] or "1.0"
    if len(result["version"])>32:
        raise ValueError("版本号最多32字")
    requires=raw.get("requires_tools",[])
    if not isinstance(requires,list) or len(requires)>30 or any(not isinstance(x,str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}",x) for x in requires):
        raise ValueError("建议工具须为工具编号列表")
    result["requires_tools"]=list(dict.fromkeys(requires))
    return result

def _read(ident):
    path=_user(ident)
    if path.is_symlink():
        raise ValueError("技能文件不能是链接")
    if path.exists():
        raw=json.loads(path.read_text(encoding="utf-8"))
        builtin=False
    else:
        folder=CATALOG/ident
        try:
            raw=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
            body=(folder/"SKILL.md").read_text(encoding="utf-8")
            raw["instructions"]=body.split("---",2)[-1].strip() if body.startswith("---\n") else body
        except (OSError,ValueError) as error:
            raise ValueError("技能不存在或文件不可读："+ident) from error
        builtin=True
    raw=_validate(raw)
    return {**raw,"builtin":builtin,"revision":hashlib.sha256(raw["instructions"].encode()).hexdigest(),"available":True,
            "permissions_note":"仅装载任务规则；不会自动勾选工具或授予执行权限。"}

def listing(page=None,search=""):
    with LOCK:
        ids={p.name for p in CATALOG.iterdir() if p.is_dir() and not p.is_symlink()} if CATALOG.is_dir() else set()
        folder=ROOT/"data/skills"
        if folder.is_dir():
            ids.update(p.stem for p in folder.glob("*.json") if not p.is_symlink())
        items=[]
        for ident in sorted(ids):
            try:
                item=_read(ident)
            except (ValueError,OSError):
                continue
            if search.casefold() in (item["name"]+" "+item["description"]).casefold():
                items.append({k:v for k,v in item.items() if k!="instructions"})
        total=len(items)
        if page is not None:
            page=max(1,min(int(page),max(1,(total+19)//20)))
            items=items[(page-1)*20:page*20]
        return {"items":items,"total":total,"page":page or 1,"page_size":20,"max_selected":MAX_SELECTED}

def detail(ident):
    with LOCK:
        return _read(ident)

def save(body,ident=None):
    with LOCK:
        raw=dict(body)
        raw["id"]=_id(ident) if ident else "skill-"+uuid.uuid4().hex[:16]
        raw=_validate(raw)
        if ident and _read(ident)["builtin"]:
            raise ValueError("内置示例请先复制，副本可以自由编辑")
        path=_user(raw["id"])
        path.parent.mkdir(parents=True,exist_ok=True)
        write_json(path,raw)
        return _read(raw["id"])

def remove(ident):
    with LOCK:
        if _read(ident)["builtin"]:
            raise ValueError("内置示例保留供学习；可复制后编辑")
        _user(ident).unlink()
        return {"ok":True}

def duplicate(ident):
    item=detail(ident)
    item["name"]=item["name"][:95]+" 副本"
    return save(item)

def export(ident,format="zip"):
    item=detail(ident)
    if format=="md":
        return item["instructions"].encode("utf-8"),item["id"]+".md","text/markdown; charset=utf-8"
    if format!="zip":
        raise ValueError("导出格式须为zip或md")
    blob=io.BytesIO()
    with zipfile.ZipFile(blob,"w",zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json",json.dumps({k:item[k] for k in ("id","name","description","version","requires_tools")},ensure_ascii=False,indent=2))
        archive.writestr("SKILL.md",item["instructions"])
        archive.writestr("README.md","技能是文字工作方法，不自动运行代码。编辑SKILL.md后重新导入会创建独立副本。建议工具需在对话中勾选。")
    return blob.getvalue(),item["id"]+".zip","application/zip"

def import_file(name,blob):
    if len(blob)>1024*1024:
        raise ValueError("技能包最多1MiB")
    if name.lower().endswith((".md",".markdown")):
        try:
            text=blob.decode("utf-8-sig")
        except UnicodeError as error:
            raise ValueError("请导入UTF-8 Markdown") from error
        return save({"name":Path(name).stem[:100],"instructions":text,"description":"导入的Markdown技能","requires_tools":[]})
    if not name.lower().endswith(".zip"):
        raise ValueError("只支持Markdown或ZIP技能包")
    try:
        archive=zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as error:
        raise ValueError('ZIP技能包无法读取') from error
    with archive:
        entries=archive.infolist()
        if len(entries)>10 or sum(e.file_size for e in entries)>64000:
            raise ValueError("技能包内容过大")
        names=[e.filename for e in entries]
        if len(set(names))!=len(names) or any(n not in {"manifest.json","SKILL.md","README.md"} for n in names):
            raise ValueError("技能包仅接收根目录manifest.json、SKILL.md、README.md")
        if not {"manifest.json","SKILL.md"}.issubset(names):
            raise ValueError("技能包缺少manifest.json或SKILL.md")
        try:
            raw=json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            raw["instructions"]=archive.read("SKILL.md").decode("utf-8-sig")
        except (UnicodeError,ValueError,TypeError) as error:
            raise ValueError("技能包不是可读的UTF-8规则") from error
    return save(raw)

def snapshot(ids):
    if not isinstance(ids,list) or len(ids)>MAX_SELECTED or any(not isinstance(v,str) for v in ids) or len(set(ids))!=len(ids):
        raise ValueError("每次最多选择2个不同技能")
    return [{k:item[k] for k in ("id","name","version","revision","instructions","requires_tools")} for item in map(detail,ids)]

def injection(opts):
    selected=opts.get("skill_snapshot")
    if selected is None:
        selected=snapshot(opts.get("skill_ids",[]))
    if not isinstance(selected,list) or len(selected)>MAX_SELECTED:
        raise ValueError("技能快照无效")
    sections=[]
    for item in selected:
        text=item.get("instructions","")
        if not isinstance(text,str) or len(text.encode("utf-8"))>MAX_BYTES or hashlib.sha256(text.encode()).hexdigest()!=item.get("revision"):
            raise ValueError("技能快照已变化，请重新选择技能后发送")
        sections.append("[已选择技能："+item["name"]+"｜版本 "+item["version"]+"]\n"+text)
    return "\n\n".join(sections)+("\n技能不增加权限；未勾选的工具不可调用，未执行的核验不得声称完成。" if sections else "")
