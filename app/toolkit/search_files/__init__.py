from pathlib import Path
import os,time,fnmatch
from ..common import resolve_args, allowed_path, tool_values

MAX_WARNINGS = 20

def search_files(args):
    args=resolve_args(args);_,target=allowed_path(args['root_id'],args['relative_path'])
    if not target.is_dir():raise ValueError('搜索起点必须是目录')
    depth_limit=tool_values('search_files')['search_depth'];query=args['query'].casefold()
    matches=[];stack=[(target,0)];seen=set();visited=0;started=time.monotonic()
    warnings=[];warning_count=0;denied_count=0
    def note_error(path,error):
        nonlocal warning_count,denied_count
        warning_count+=1
        denied=isinstance(error,PermissionError)
        denied_count+=int(denied)
        if len(warnings)<MAX_WARNINGS:
            warnings.append({'path':str(path)[:500],'error':type(error).__name__,
                             'message':'当前Windows账户没有读取权限，已跳过' if denied else '目录或条目读取失败，已跳过'})
    while stack and visited<5000 and len(matches)<1000 and time.monotonic()-started<3:
        folder,depth=stack.pop()
        try:
            canonical=str(folder.resolve()).casefold()
            if canonical in seen:continue
            seen.add(canonical)
            with os.scandir(folder) as scan:
                for item in scan:
                    if visited>=5000 or time.monotonic()-started>=3:break
                    visited+=1
                    try:
                        directory=item.is_dir();info=item.stat()
                        if directory and depth<depth_limit:stack.append((Path(item.path),depth+1))
                        match=fnmatch.fnmatchcase(item.name.casefold(),query) if any(c in query for c in '*?[') else query in item.name.casefold()
                        if match:matches.append({'name':item.name,'path':str(Path(item.path)),'relative_path':str(Path(item.path).relative_to(target)),
                                                 'type':'directory' if directory else 'file','size':None if directory else info.st_size})
                        if len(matches)>=1000:break
                    except OSError as error:
                        note_error(item.path,error)
                        continue
        except OSError as error:
            if depth==0:
                message='当前Windows账户没有目录读取权限' if isinstance(error,PermissionError) else '目录不存在、不可访问或读取失败'
                raise ValueError('搜索起点无法读取：'+message+'；没有将读取失败当作无匹配结果') from error
            note_error(folder,error)
            continue
    matches.sort(key=lambda v:v['relative_path'].casefold());offset=args.get('offset',0);limit=args.get('limit',100)
    return {'path':str(target),'root_id':args['root_id'],'relative_path':str(target),'query':args['query'],'search_depth':depth_limit,
            'items':matches[offset:offset+limit],'count':len(matches),'next_offset':offset+limit if offset+limit<len(matches) else None,
            'visited':visited,'truncated':bool(stack) or visited>=5000 or len(matches)>=1000 or time.monotonic()-started>=3 or warning_count>0,
            'warnings':warnings,'warning_count':warning_count,'denied_count':denied_count,
            'warnings_truncated':warning_count>len(warnings),'partial':warning_count>0}
