"""Shared file-level pre-edit snapshots with bounded local storage."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import uuid
import weakref
from .config import ROOT, read_json, write_json

LOCK = threading.RLock()
FILE_LOCKS = weakref.WeakValueDictionary()
@contextmanager
def file_lock(path):
    key = str(Path(path).resolve()).casefold()
    with LOCK:
        lock = FILE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield

def limits():
    from .toolkit.common import configuration_override
    override=configuration_override("file_manage")
    cfg=override if override is not None else read_json(ROOT/"config/tools/file_manage.json",{})
    return max(1,min(10000,int(cfg.get("snapshot_count",100)))), max(1,min(102400,int(cfg.get("snapshot_mb",2048))))*1024*1024

def folder():
    p=ROOT/"data/snapshots"
    p.mkdir(parents=True,exist_ok=True)
    return p

def _items():
    return sorted((read_json(p,{}) for p in folder().glob("*.json")),key=lambda r:r.get("created_at",""),reverse=True)

def _cleanup():
    count,cap=limits(); used=0
    for i,row in enumerate(_items()):
        used+=row.get("bytes",0)
        if i>=count or used>cap:
            (folder()/(row["id"]+".bin")).unlink(missing_ok=True)
            (folder()/(row["id"]+".json")).unlink(missing_ok=True)

def create(path, action="edit", call_id=""):
    source=Path(path).resolve(strict=True)
    if not source.is_file(): raise ValueError("快照仅支持普通文件")
    count,cap=limits()
    if source.stat().st_size>cap: raise ValueError("文件超过快照总容量设置；未修改原文件，请在文件操作器设置中增加容量")
    with file_lock(source), LOCK:
        ident=uuid.uuid4().hex; destination=folder()/(ident+".bin")
        try:
            shutil.copy2(source,destination)
            with source.open("rb") as f: source_hash=hashlib.file_digest(f,"sha256").hexdigest()
            with destination.open("rb") as f: saved_hash=hashlib.file_digest(f,"sha256").hexdigest()
            if source_hash!=saved_hash: raise ValueError("快照过程中原文件发生变化；未开始编辑")
            row={"id":ident,"path":str(source),"created_at":datetime.now(timezone.utc).isoformat(),
                 "bytes":destination.stat().st_size,"action":str(action)[:80],"sha256":saved_hash,"call_id":str(call_id)[:160]}
            write_json(folder()/(ident+".json"),row)
            _cleanup()
            return row
        except BaseException:
            destination.unlink(missing_ok=True)
            (folder()/(ident+".json")).unlink(missing_ok=True)
            raise

def listing(page=1,page_size=30):
    with LOCK:
        _cleanup()
        rows=_items(); page=max(1,int(page)); page_size=min(100,max(1,int(page_size)))
        count,cap=limits()
        return {"items":rows[(page-1)*page_size:page*page_size],"total":len(rows),"page":page,"page_size":page_size,
                "max_count":count,"max_bytes":cap,"bytes":sum(x["bytes"] for x in rows)}

def restore(ident):
    if not re.fullmatch(r"[a-f0-9]{32}",ident): raise ValueError("无效快照编号")
    with LOCK:
        row=read_json(folder()/(ident+".json"),{})
        if not row: raise ValueError("快照不存在或已过期")
        source=folder()/(ident+".bin")
        with source.open("rb") as f:
            if hashlib.file_digest(f,"sha256").hexdigest()!=row["sha256"]: raise ValueError("快照校验失败")
        # Hold snapshot bytes before create() potentially evicts this oldest snapshot.
        staging=folder()/(uuid.uuid4().hex+".restore")
        shutil.copyfile(source,staging)
    path=Path(row["path"])
    try:
        with file_lock(path):
            previous=create(path,"before_restore") if path.exists() else None
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name("."+path.name+"."+uuid.uuid4().hex+".tmp")
            try:
                shutil.copyfile(staging,temporary)
                os.replace(temporary,path)
            finally: temporary.unlink(missing_ok=True)
            return {"ok":True,"path":str(path),"snapshot_id":previous["id"] if previous else None,"restored_id":ident}
    finally: staging.unlink(missing_ok=True)
