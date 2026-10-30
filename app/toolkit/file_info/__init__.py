from datetime import datetime
import stat
from ..common import resolve_args, allowed_path

def file_info(args):
    args=resolve_args(args)
    base,target=allowed_path(args['root_id'],args['relative_path'])
    info=target.stat()
    return {'path':str(target),'parent':str(target.parent),'root_id':args['root_id'],'relative_path':str(target.relative_to(base)),
            'name':target.name,'type':'directory' if target.is_dir() else 'file','extension':target.suffix.lower(),
            'size':info.st_size if target.is_file() else None,'created':datetime.fromtimestamp(info.st_ctime).astimezone().isoformat(),
            'modified':datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(),'read_only':bool(getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_READONLY)}
