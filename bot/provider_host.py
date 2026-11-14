"""Host replay guard and fixed operator argv. No token reaches the subprocess."""
import fcntl,json,os,subprocess
from pathlib import Path
from provider_refresh import provider
CONFIG=Path('/etc/hermes-provider-ops-routes.json')
STATE=Path('/var/lib/hermes-provider-ops')
def execute(q):
    name=provider(q.get('update',{}))
    if not name or name!=q.get('provider'):return {'ok':False,'error':'Unauthorized refresh'}
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (STATE/'refresh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=STATE/'updates.json';seen=json.loads(path.read_text()) if path.exists() else {}
        uid=str(q['update']['update_id'])
        if uid in seen:return {'ok':True,'duplicate':True}
        cfg=json.loads(CONFIG.read_text());argv=cfg['operator_argv']
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) for x in argv) or not argv[0].startswith('/'):
            raise ValueError('Fixed operator argv required')
        seen[uid]={'ok':True,'started':True}
        tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(seen));os.chmod(tmp,0o600);tmp.replace(path)
        try:
            p=subprocess.run(argv+[name],capture_output=True,text=True,timeout=45)
            out={'ok':p.returncode==0,'result':p.stdout[:8192]}
        except Exception:out={'ok':False,'error':'operator unavailable'}
        seen[uid]=out;tmp.write_text(json.dumps(seen));os.chmod(tmp,0o600);tmp.replace(path)
        return out
