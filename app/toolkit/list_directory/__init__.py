from datetime import datetime
from pathlib import Path
import os,time
from ..common import resolve_args, allowed_path

def directory_list(args):
    args=resolve_args(args);_,target=allowed_path(args['root_id'],args['relative_path'])
    if not target.is_dir():raise ValueError('目标不是目录')
    entries=[];started=time.monotonic();truncated=False
    with os.scandir(target) as scan:
        for item in scan:
            if len(entries)>=5000 or time.monotonic()-started>3:truncated=True;break
            try:
                info=item.stat();is_dir=item.is_dir()
                entries.append({'name':item.name,'path':str(Path(item.path)),'type':'directory' if is_dir else 'file','size':None if is_dir else info.st_size,'modified':datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(),'is_link':item.is_symlink()})
            except OSError:continue
    entries.sort(key=lambda v:(v['type']!='directory',v['name'].casefold()))
    offset=args.get('offset',0)
    return {'path':str(target),'parent':str(target.parent),'root_id':args['root_id'],'relative_path':str(target),'items':entries[offset:offset+200],
            'count':len(entries),'next_offset':offset+200 if offset+200<len(entries) else None,'truncated':truncated}
