"""Construct Docker recreate argv from inspect without exposing environment on stdout.

The caller must write the env file outside the repository with mode 0600.
No Docker daemon is contacted by these helpers.
"""
import os
from pathlib import Path
RETIRED={'/opt/hermes/tools/delegate_tool.py','/opt/hermes/tools/delegate_tool_dispatch.py','/opt/hermes/gateway/platforms/api_server_openai_routes.py'}
def recreate(inspect,image,envfile,cmd=None):
    cfg=inspect['Config'];host=inspect['HostConfig']
    argv=['docker','create','--name',inspect['Name'].lstrip('/'),'--env-file',str(envfile)]
    if cfg.get('User'):argv+=['--user',cfg['User']]
    if cfg.get('WorkingDir'):argv+=['--workdir',cfg['WorkingDir']]
    restart=host.get('RestartPolicy',{});policy=restart.get('Name')
    if policy:
        argv+=['--restart',policy+(':'+str(restart['MaximumRetryCount']) if restart.get('MaximumRetryCount') else '')]
    if host.get('NetworkMode'):argv+=['--network',host['NetworkMode']]
    for key,value in cfg.get('Labels',{}).items():argv+=['--label',key+'='+value]
    for mount in inspect.get('Mounts',[]):
        if mount['Destination'] in RETIRED:continue
        kind=mount['Type']
        if kind not in ('bind','volume'):raise ValueError('Unsupported mount type: '+kind)
        source=mount.get('Name') if kind=='volume' else mount['Source']
        spec=f"type={kind},src={source},dst={mount['Destination']}"
        if not mount.get('RW',True):spec+=',readonly'
        if ',' in source or ',' in mount['Destination']:raise ValueError('Unsupported mount comma')
        argv+=['--mount',spec]
    for port,bindings in (host.get('PortBindings') or {}).items():
        for binding in bindings or []:
            argv+=['--publish',binding.get('HostIp','')+':'+binding['HostPort']+':'+port]
    # Refuse unsupported security/resource/network contracts instead of dropping them.
    for key in ('Privileged','CapAdd','CapDrop','Devices','SecurityOpt','ExtraHosts','Dns','Tmpfs','Links','VolumesFrom','Memory','NanoCpus','ReadonlyRootfs','PidMode','IpcMode'):
        value=host.get(key)
        if value and not (key=='IpcMode' and value=='private'):raise ValueError('Unsupported Docker contract: '+key)
    entry=cfg.get('Entrypoint') or []
    if len(entry)>1:raise ValueError('Multiargument entrypoint requires explicit adapter')
    if entry:argv+=['--entrypoint',entry[0]]
    return argv+[image]+list(cmd if cmd is not None else cfg.get('Cmd') or [])

def private_env(path,inspect):
    path=Path(path)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as out:
        for value in inspect['Config'].get('Env',[]):
            if '\n' in value or '\r' in value:raise ValueError('Multiline Docker environment unsupported')
            out.write(value+'\n')
