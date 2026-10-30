"""Transactional Office automation and bounded, locally rendered preview artifacts."""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from .config import ROOT, read_json, write_json
from .runtime import worker_command

OFFICE_LOCK=threading.RLock()
PREVIEW_ROOT=ROOT/"cache/office-previews"
KINDS={".doc":"word",".docx":"word",".xls":"excel",".xlsx":"excel",".ppt":"powerpoint",".pptx":"powerpoint"}
DEFAULTS={"timeout_seconds":120,"preview_pages":3,"preview_cache_mb":256,"preview_retention_days":7,"preview_after_edit":True}

def configuration():
    from .toolkit import common
    override=getattr(common,"configuration_override",lambda ident:None)("office_operator")
    if override is not None:
        return {**DEFAULTS,**override}
    return {**DEFAULTS,**read_json(ROOT/"config/tools/office_operator.json",{})}

def macro_free(path,creating=False):
    if creating:return True
    if path.suffix.lower()!=".pptx":return False
    import zipfile
    try:
        with zipfile.ZipFile(path) as package:
            return not any(n.lower().endswith("vbaproject.bin") for n in package.namelist())
    except zipfile.BadZipFile:
        owned=read_json(ROOT/"cache/office-created.json",{})
        return owned.get(str(path))==fingerprint(path)

def record_created(path):
    file=ROOT/"cache/office-created.json"
    owned=read_json(file,{})
    owned.pop(str(path),None);owned[str(path)]=fingerprint(path)
    while len(owned)>100:owned.pop(next(iter(owned)))
    write_json(file,owned)

def fingerprint(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()

def path_value(value, *, exists=True):
    path=Path(str(value))
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("需要完整文件路径")
    path=path.resolve()
    if path.suffix.lower() not in KINDS:
        raise ValueError("Office支持doc/docx/xls/xlsx/ppt/pptx")
    if exists and not path.is_file():
        raise ValueError("Office文件不存在")
    if not exists and not path.parent.is_dir():
        raise ValueError("目标目录不存在，请先创建目录")
    return path

@contextmanager
def require_available(path):
    """Detect Office sharing locks without closing the user's open document."""
    if os.name!="nt":
        raise ValueError("Office COM操作需要Windows和已安装的Microsoft Office")
    import win32file
    import win32con
    handle=None
    try:
        handle=win32file.CreateFile(str(path),win32con.GENERIC_READ,win32con.FILE_SHARE_READ|win32con.FILE_SHARE_DELETE,None,win32con.OPEN_EXISTING,win32con.FILE_ATTRIBUTE_NORMAL,None)
    except Exception as error:
        raise ValueError("文件正被其他程序使用或无权访问，请先保存并关闭该文件后重试") from error
    try:
        yield
    finally:
        if handle is not None: handle.Close()

def image_arguments(operations,scope):
    from PIL import Image
    from . import attachments
    result=[]
    for raw in operations:
        step=dict(raw)
        if step.get("op")=="insert_picture":
            if step.get("attachment_id"):
                if step["attachment_id"] not in (scope or {}).get("attachment_ids",[]):
                    raise ValueError("图片附件不属于当前对话")
                _,source=attachments.raw_source(step["attachment_id"])
            else:
                source=Path(str(step.get("image_path","")))
                if not source.is_absolute() or not source.is_file():
                    raise ValueError("插图需要有效image_path或本对话attachment_id")
            if source.stat().st_size>50*1024*1024:
                raise ValueError("图片最大50MiB")
            with Image.open(source) as image:
                if image.width*image.height>40_000_000:
                    raise ValueError("图片像素过大，请先缩小")
                image.verify()
            step["image_path"]=str(source.resolve())
        result.append(step)
    return result

def kill_owned(record):
    import psutil
    info=read_json(record,{})
    try:
        p=psutil.Process(info["pid"])
        if abs(p.create_time()-info["created"])<.01 and p.name().lower()==info["name"]:
            p.terminate()
            return True
    except (KeyError,psutil.Error):
        pass
    return False

def run_worker(payload,timeout):
    job=ROOT/"cache/office-jobs"/uuid.uuid4().hex
    job.mkdir(parents=True)
    record=job/"owner.json"
    payload={**payload,"ownership":str(record)}
    try:
        proc=subprocess.run(worker_command("office"),input=json.dumps(payload,ensure_ascii=False),encoding="utf-8",
            capture_output=True,timeout=timeout,cwd=Path(__file__).resolve().parents[1],
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if len(proc.stdout)>4*1024*1024:
            raise ValueError("Office返回过大，请缩小读取范围")
        try:
            result=json.loads(proc.stdout)
        except (ValueError,TypeError) as error:
            raise ValueError("Office未返回有效结果；请检查Office安装和激活状态") from error
        if proc.returncode or not result.get("ok"):
            raise ValueError(result.get("error","Office执行失败"))
        return result
    except subprocess.TimeoutExpired as error:
        stopped=kill_owned(record)
        detail="已停止本工具独立创建的Office进程" if stopped else "未关闭用户Office进程；请检查是否有工具临时文稿残留"
        raise ValueError("Office任务超时，未提交原件修改；"+detail) from error
    finally:
        if record.exists(): record.unlink()
        job.rmdir()

def fonts():
    if os.name!="nt":
        return {"fonts":[],"available":False}
    import winreg
    names=set()
    for hive in (winreg.HKEY_LOCAL_MACHINE,winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive,r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name,_,_=winreg.EnumValue(key,i)
                    names.add(re.sub(r"\s*\([^)]*\)$","",name))
        except OSError:
            pass
    return {"fonts":sorted(names,key=str.casefold),"available":True,"sample":"设备检查记录 ABC 123"}

def cleanup_previews():
    PREVIEW_ROOT.mkdir(parents=True,exist_ok=True)
    cfg=configuration()
    total=0; groups=[]
    for folder in PREVIEW_ROOT.iterdir():
        if not folder.is_dir() or folder.is_symlink() or not re.fullmatch("[a-f0-9]{32}",folder.name): continue
        if not (folder/"metadata.json").is_file(): continue
        size=sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
        groups.append((folder.stat().st_mtime,folder,size)); total+=size
    groups.sort()
    budget=max(16,min(4096,int(cfg["preview_cache_mb"])))*1024*1024
    age=max(1,min(30,int(cfg["preview_retention_days"])))*86400
    for stamp,folder,size in groups:
        if time.time()-stamp>age or total>budget or len(groups)>100:
            if folder.resolve().parent!=PREVIEW_ROOT.resolve(): raise ValueError("预览清理路径异常")
            shutil.rmtree(folder); total-=size; groups=[x for x in groups if x[1]!=folder]

def preview_info(ident):
    if not re.fullmatch("[a-f0-9]{32}",ident): raise ValueError("预览编号无效")
    data=read_json(PREVIEW_ROOT/ident/"metadata.json",{})
    if not data: raise ValueError("预览不存在或已自动清理，请重新渲染")
    return data

def preview_file(ident,name):
    data=preview_info(ident)
    valid={Path(item["url"]).name for item in data["items"]}
    if data.get("pdf_url"): valid.add("document.pdf")
    if name not in valid: raise ValueError("预览文件不存在")
    path=PREVIEW_ROOT/ident/name
    if not path.is_file() or path.is_symlink(): raise ValueError("预览文件不可用")
    return path

def finish_preview(folder,result,target):
    render=result.pop("render",None)
    if not render: return
    ident=folder.name
    items=[{"page":page,"url":f"/api/office/previews/{ident}/page-{page}.png","name":f"第{page}页"} for page in render["pages"]]
    info={"id":ident,"items":items,"total":len(items),"total_pages":render.get("total_pages"),"created":time.time(),"source_name":target.name,"method":render.get("method","Office打印页"),"range":render.get("range")}
    if render.get("pdf_name"):
        info["pdf_url"]=f"/api/office/previews/{ident}/document.pdf"
    write_json(folder/"metadata.json",info)
    result.update(preview_id=ident,preview=info)

def execute(args,scope=None):
    action=args.get("action","inspect")
    if action=="fonts": return fonts()
    if action=="help":
        manual=(Path(__file__).resolve().parent/"toolkit/office_operator/README.md").read_text(encoding="utf-8")
        application=args.get("application")
        title={"word":"## Word 动作","excel":"## Excel 动作","powerpoint":"## PPT 动作"}.get(application)
        if title:
            section=manual.split(title,1)[1].split("\n## ",1)[0]
            return {"application":application,"help":title+section,"positions":"编号从1开始；尺寸单位磅；先inspect实际结构再编辑"}
        return {"help":"action: inspect/create/edit/copy/preview/fonts；指定application=word/excel/powerpoint并action=help读取动作参数教程。所有工具统一，不区分模型能力。"}
    if action not in ("inspect","create","edit","preview","copy"):
        raise ValueError("Office动作不支持")
    if os.name!="nt": raise ValueError("Office工具需要Windows")
    source=path_value(args.get("path",""),exists=action!="create")
    target=path_value(args["output_path"],exists=False) if args.get("output_path") else source
    if action in ("create","copy") and target.exists(): raise ValueError("新建/另存目标已存在，请换名")
    if target!=source and target.exists(): raise ValueError("另存目标已存在，请换名")
    kind=KINDS[source.suffix.lower()]
    if target.suffix.lower()!=source.suffix.lower(): raise ValueError("另存须保持原格式")
    if action=="create" and target.suffix.lower() not in (".docx",".xlsx",".pptx"): raise ValueError("新建请选择docx/xlsx/pptx")
    ops=args.get("operations",[])
    if not isinstance(ops,list) or len(ops)>100 or any(not isinstance(o,dict) or not isinstance(o.get("op"),str) for o in ops):
        raise ValueError("operations需要最多100项带op的操作列表")
    if action=="edit" and not ops: raise ValueError("编辑需要operations")
    if action in ("inspect","preview","copy") and ops: raise ValueError("此动作不接受编辑operations")
    if len(json.dumps(args,ensure_ascii=False))>2*1024*1024: raise ValueError("单次Office输入过大")
    cfg=configuration(); timeout=max(10,min(300,int(cfg["timeout_seconds"])))
    from . import snapshots
    with OFFICE_LOCK,snapshots.file_lock(source):
        cleanup_previews()
        if action=="copy":
            with require_available(source):
                # Windows rename publishes without replacing a colliding destination.
                temp=target.with_name("localai_stage_"+uuid.uuid4().hex+target.suffix)
                try:
                    shutil.copy2(source,temp)
                    if fingerprint(source)!=fingerprint(temp): raise ValueError("副本校验失败")
                    os.rename(temp,target)
                    if source.suffix.lower()==".pptx" and macro_free(source):record_created(target)
                finally: temp.unlink(missing_ok=True)
            return {"ok":True,"path":str(target),"action":action,"bytes":target.stat().st_size}
        mutated=action in ("create","edit")
        isolated_read=not mutated and kind=="powerpoint"
        stage=target.with_name("localai_stage_"+uuid.uuid4().hex+target.suffix) if mutated or isolated_read else source
        if isolated_read:
            staged_reads=ROOT/"cache/office-staged"
            staged_reads.mkdir(parents=True,exist_ok=True)
            stage=staged_reads/(uuid.uuid4().hex+source.suffix)
        want_preview=action=="preview" or bool(args.get("preview",cfg["preview_after_edit"] if mutated else False))
        preview=PREVIEW_ROOT/uuid.uuid4().hex if want_preview else None
        if preview: preview.mkdir(parents=True)
        original_hash=None; snapshot=None
        try:
            if isolated_read:
                shutil.copy2(source,stage)
            if action=="edit":
                with require_available(source):
                    original_hash=fingerprint(source)
                    if target==source: snapshot=snapshots.create(source,action="office_edit",call_id=str((scope or {}).get("call_id","")))
                    shutil.copy2(source,stage)
            payload={**{k:v for k,v in args.items() if k in ("sheet","range","table","offset","limit","page","page_count","preview_mode")},
                     "action":action,"application":kind,"path":str(stage),"operations":image_arguments(ops,scope)}
            if kind=="powerpoint":payload["macro_free"]=macro_free(source,action=="create")
            payload.setdefault("page_count",int(cfg["preview_pages"]))
            if preview: payload["preview_dir"]=str(preview)
            result=run_worker(payload,timeout)
            if mutated:
                if action=="edit" and original_hash!=fingerprint(source):
                    raise ValueError("文件在执行期间被其他程序修改，未覆盖外部修改")
                if target==source and action=="edit":
                    with require_available(target):
                        if original_hash!=fingerprint(target):
                            raise ValueError("提交前文件已被其他程序修改，未覆盖")
                    # Office/encryption filters may reject replacement while even
                    # a share-delete handle is open. Close after the last hash
                    # check, then atomically publish under our per-file lock.
                    os.replace(stage,target)
                else:
                    os.rename(stage,target)
                result.update(path=str(target),bytes=target.stat().st_size,snapshot_id=snapshot["id"] if snapshot else None)
                if kind=="powerpoint" and payload.get("macro_free"):record_created(target)
            else: result["path"]=str(source)
            result["action"]=action
            if preview:
                finish_preview(preview,result,target)
                if args.get("return_images") and result.get("preview"):
                    images=[]
                    for item in result["preview"]["items"][:3]:
                        blob=preview_file(result["preview_id"],Path(item["url"]).name).read_bytes()
                        if len(blob)>4*1024*1024: continue
                        images.append({"page":item["page"],"mime_type":"image/png","data_base64":base64.b64encode(blob).decode("ascii")})
                    result["visual_images"]=images
            return result
        finally:
            if mutated or isolated_read: stage.unlink(missing_ok=True)
            if preview and not (preview/"metadata.json").exists() and preview.resolve().parent==PREVIEW_ROOT.resolve():
                shutil.rmtree(preview)
