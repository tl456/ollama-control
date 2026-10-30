"""Native file operations. Text creation never overwrites; deletion is recycle only."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import shutil
import uuid
from . import snapshots
from .toolkit.common import checked

def destination(directory,name):
    base=checked(directory)
    if not base.is_dir(): raise ValueError("directory 必须为现有目录完整路径")
    if not name or name in (".","..") or any(c in name for c in '/\\:*?"<>|') or name[-1:] in (" ","."):
        raise ValueError("文件名不可包含路径、保留字符或结尾空格/点")
    if name.split(".")[0].upper() in {"CON","PRN","AUX","NUL",*[f"COM{i}" for i in range(1,10)],*[f"LPT{i}" for i in range(1,10)]}:
        raise ValueError("系统保留文件名")
    return base/name

def reserve(directory,name):
    desired=destination(directory,name)
    for i in range(1,10001):
        candidate=desired if i==1 else desired.with_name(desired.stem+f" ({i})"+desired.suffix)
        try: return candidate,candidate.open("xb")
        except FileExistsError: continue
    raise ValueError("同名文件过多")

def text_create(args,scope=None):
    name=args["name"]; fmt=args.get("format","md").lower()
    if not name.lower().endswith("."+fmt): name+="."+fmt
    text=args["text"]
    if len(text.encode("utf-8"))>10*1024*1024: raise ValueError("单次文本最多10MiB")
    path,stream=reserve(args["directory"],name)
    try:
        with stream: stream.write(text.encode("utf-8")); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True); raise
    return {"ok":True,"path":str(path),"bytes":path.stat().st_size,"encoding":"utf-8","created":True}

def recycle(path):
    if os.name!="nt": raise ValueError("当前系统未提供Windows回收站，未删除文件")
    p=str(path)
    if p.startswith("\\\\"): raise ValueError("网络路径不能保证进入Windows回收站，未删除")
    # Require a fixed local drive: removable/network volumes may permanently delete.
    if ctypes.windll.kernel32.GetDriveTypeW(str(Path(p).anchor))!=3:
        raise ValueError("仅本机固定磁盘支持此回收站操作，未删除")
    import pythoncom
    from win32com.shell import shell, shellcon
    from win32com.server.exception import COMException
    from win32com.server.policy import DesignatedWrapPolicy
    class RecycleOnly(DesignatedWrapPolicy):
        _com_interfaces_=[shell.IID_IFileOperationProgressSink]
        _public_methods_=["StartOperations","FinishOperations","PreRenameItem","PostRenameItem","PreMoveItem","PostMoveItem",
                          "PreCopyItem","PostCopyItem","PreDeleteItem","PostDeleteItem","PreNewItem","PostNewItem",
                          "UpdateProgress","ResetTimer","PauseTimer","ResumeTimer"]
        recycled=False
        def __init__(self): self._wrap_(self)
        def __getattr__(self,name):
            if name in self._public_methods_: return lambda *args: None
            raise AttributeError(name)
        def PreDeleteItem(self,flags,item):
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                raise COMException(desc="Recycle-only operation refused",scode=-2147467259)
        def PostDeleteItem(self,flags,item,result,new_item):
            self.recycled=bool(new_item) and result>=0
    pythoncom.CoInitialize()
    try:
        operation=pythoncom.CoCreateInstance(shell.CLSID_FileOperation,None,pythoncom.CLSCTX_ALL,shell.IID_IFileOperation)
        operation.SetOperationFlags(shellcon.FOF_SILENT|shellcon.FOF_NOERRORUI|shellcon.FOF_NOCONFIRMATION|
                                    shellcon.FOFX_EARLYFAILURE|0x20000000|0x00080000)
        sink=RecycleOnly()
        wrapped=pythoncom.WrapObject(sink,shell.IID_IFileOperationProgressSink)
        operation.DeleteItem(shell.SHCreateItemFromParsingName(p,None,shell.IID_IShellItem),wrapped)
        result=operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted() or Path(p).exists() or not sink.recycled:
            raise ValueError("未确认回收站操作完成；未尝试永久删除")
    finally: pythoncom.CoUninitialize()
    return {"ok":True,"path":p,"recycled":True}

def file_manage(args,scope=None):
    action=args["action"]; scope=scope or {}
    if action=="mkdir":
        p=destination(args["directory"],args["name"]); p.mkdir(exist_ok=False)
        return {"ok":True,"path":str(p),"created":True}
    candidate=Path(args["path"]).absolute()
    if action in ("recycle","move","rename") and (candidate.is_symlink() or (hasattr(candidate,"is_junction") and candidate.is_junction())):
        raise ValueError("此路径是链接或联接点；未操作其指向的文件。请明确指定实际文件路径，或在资源管理器中处理链接本身。")
    p=checked(args["path"])
    with snapshots.file_lock(p):
        if action=="info":
            s=p.stat();return {"path":str(p),"directory":p.is_dir(),"bytes":s.st_size,"modified":s.st_mtime}
        if action=="list":
            if not p.is_dir(): raise ValueError("目标不是目录")
            limit=min(200,max(1,int(args.get("limit",50))));page=max(1,int(args.get("page",1)))
            from itertools import islice
            rows=[{"name":q.name,"directory":q.is_dir()} for q in islice(p.iterdir(),(page-1)*limit,page*limit+1)]
            return {"path":str(p),"items":rows[:limit],"page":page,"has_more":len(rows)>limit}
        if action=="recycle": return recycle(p)
        if action in ("copy","move","rename"):
            dest=destination(args.get("directory",str(p.parent)),args.get("name",p.name))
            if dest.exists(): raise ValueError("目标已存在，未覆盖；请使用新名称")
            if dest.is_relative_to(p): raise ValueError("不能复制或移动到自身子目录")
            if action=="copy":
                if p.is_dir(): shutil.copytree(p,dest)
                else:
                    with dest.open("xb") as output,p.open("rb") as source: shutil.copyfileobj(source,output)
            else:
                if dest.is_relative_to(p): raise ValueError("不能移动到自身子目录")
                if os.name=="nt": os.rename(p,dest)
                else:
                    # Avoid overwriting even on POSIX (development tests only).
                    if dest.exists(): raise FileExistsError(dest)
                    shutil.move(str(p),str(dest))
            return {"ok":True,"path":str(dest),"action":action}
        if action=="edit_text":
            if not p.is_file() or p.stat().st_size>10*1024*1024: raise ValueError("文本编辑最大10MiB")
            encoding=args.get("encoding","utf-8")
            try: old=p.read_text(encoding=encoding)
            except (UnicodeError,LookupError) as error: raise ValueError("无法按指定编码读取；未修改，请确认文本编码") from error
            if "\x00" in old or old.startswith("%TSD-Header"): raise ValueError("目标不是可直接编辑的普通文本")
            text=args.get("text")
            if text is None:
                find=args.get("find","")
                if not find: raise ValueError("请提供text全文，或非空find与replace")
                count=old.count(find)
                if count==0: raise ValueError("未找到需要替换的文字，未修改")
                text=old.replace(find,args.get("replace",""),int(args.get("count",-1)))
            if text==old: return {"ok":True,"path":str(p),"changed":False}
            blob=text.encode(encoding)
            if len(blob)>10*1024*1024: raise ValueError("编辑后文本超过10MiB")
            snap=snapshots.create(p,"edit_text",scope.get("_call_id",""))
            temp=p.with_name("."+p.name+"."+uuid.uuid4().hex+".tmp")
            try:
                with temp.open("xb") as f: f.write(blob); f.flush();os.fsync(f.fileno())
                os.replace(temp,p)
                if p.read_bytes()!=blob: raise ValueError("保存后的文本校验失败")
            finally: temp.unlink(missing_ok=True)
            return {"ok":True,"path":str(p),"changed":True,"bytes":len(blob),"snapshot_id":snap["id"]}
    raise ValueError("未知文件操作")
