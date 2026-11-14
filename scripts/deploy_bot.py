#!/usr/bin/env python3
"""Local host helper: atomically replace an explicitly named bot file. Never auto-runs."""
import argparse,hashlib,os,shutil,tempfile
from pathlib import Path
def deploy(source,destination,preimage=None):
    source=Path(source);destination=Path(destination)
    if not destination.is_file():raise ValueError('Existing bot file required')
    before=hashlib.sha256(destination.read_bytes()).hexdigest()
    if preimage and before!=preimage:raise ValueError('Preimage mismatch')
    backup=destination.with_name(destination.name+'.provider-ops-'+before+'.bak')
    if not backup.exists():shutil.copy2(destination,backup)
    fd,tmp=tempfile.mkstemp(prefix='.provider-ops-',dir=destination.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(source.read_bytes());out.flush();os.fsync(out.fileno())
        st=destination.stat();os.chmod(tmp,st.st_mode);os.chown(tmp,st.st_uid,st.st_gid)
        os.replace(tmp,destination)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return before
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('destination');p.add_argument('--preimage-sha256');a=p.parse_args()
    deploy(a.source,a.destination,a.preimage_sha256)
