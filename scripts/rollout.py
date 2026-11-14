#!/usr/bin/env python3
"""Stage a pinned local release and launch a detached host transaction."""
import argparse, io, json, os, re, shlex, subprocess, sys, tarfile, time, uuid
from pathlib import Path
from rollout_support import redact, exception, log, STAGING_LIMIT
DEFAULT=('manu','mafe','luna','aquario')
CONFIG=str(Path.home()/'.ssh/config_carla_infra')
SHA='3b421578588243f920886e6fe79a6faa87ea026e'
ROOT=Path(__file__).resolve().parents[1]
def ssh_argv(target,remote):
    if target in ('mafe','tanque','aquario'):return ['ssh','-F',CONFIG,target,shlex.join(remote)]
    mac=['sudo','-n','ssh','-i','/srv/hermes-opt-data/carla-scratch/ssh/macmini_ed25519','leonardovenancio@100.87.54.99']
    if target=='manu':mac += [shlex.join(['ssh','hostinger-vps',shlex.join(['sudo','-n','-u','manuella','env','HERMES_HOME=/opt/manuella',*remote])])]
    elif target=='luna':mac += [shlex.join(remote)]
    else:raise ValueError('Unknown target')
    return ['ssh','-F',CONFIG,'aquario',shlex.join(mac)]
def remote(target,argv,**kw):
    command=ssh_argv(target,argv)
    try:p=subprocess.run(command,capture_output=True,timeout=45,env=dict(os.environ,DOCKER_API_VERSION='1.45'),**kw)
    except BaseException as e:
        log(f'step=remote command={shlex.join(command)} error={e} stdout={getattr(e,"stdout",None)} stderr={getattr(e,"stderr",None)}');raise
    if p.returncode:
        message=redact(f'step=remote command={shlex.join(command)} returncode={p.returncode} stdout={p.stdout!r} stderr={p.stderr!r}');log(message);raise RuntimeError(message)
    return p.stdout

def bundle(ref):
    # Preserve the actual Git commit, so native --ref verifies it without network.
    command=['git','-C',str(ROOT),'bundle','create','-','--all']
    try:data=subprocess.check_output(command,stderr=subprocess.PIPE,timeout=45)
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
        log(f'step=bundle command={shlex.join(command)} returncode={getattr(e,"returncode",None)} error={e} stdout={getattr(e,"stdout",None)} stderr={getattr(e,"stderr",None)}');raise
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as tar:
        info=tarfile.TarInfo('release.bundle');info.size=len(data);info.mode=0o600;tar.addfile(info,io.BytesIO(data))
        for folder in ('scripts','patches'):
            for path in (ROOT/folder).iterdir():
                if path.is_file():tar.add(path,arcname=str(path.relative_to(ROOT)))
    # Ref must exist in this repository; never silently substitute a working tree.
    from rollout_support import run
    actual=run(['git','-C',str(ROOT),'rev-parse',ref+'^{commit}'])
    if actual!=ref:raise ValueError('Repo ref is not an exact commit')
    if len(out.getvalue())>=STAGING_LIMIT:raise RuntimeError('Deployment payload exceeds 50MiB')
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as check:
        if sum(m.size for m in check)>=STAGING_LIMIT:raise RuntimeError('Uncompressed deployment payload exceeds 50MiB')
    return out.getvalue()

def launch(target,job,ref,image,turns,rollback=False):
    # Bootstrap image is independent of the upgrade image; tanque builds its own derivative.
    worker=['/usr/bin/python3',job+'/scripts/host_worker.py','--target',target,'--repo-ref',ref]
    if image:worker+=['--image',image]
    if turns:worker+=['--accept-turns']
    if rollback:worker+=['--rollback']
    if target in ('mafe','tanque'):
        # Only sudo docker is needed. Host Python is verified before detachment.
        bootstrap="import os,sys; os.chroot('/host'); os.chdir(sys.argv[1]); os.execv('/usr/bin/python3',['/usr/bin/python3']+sys.argv[2:])"
        worker=['sudo','-n','docker','run','--rm','--network','host','--pid','host','-v','/:/host','-v','/var/run/docker.sock:/var/run/docker.sock','--entrypoint','python3',image or ('sha256:c1c56809ca97db6d2ce4862dabda6c8cda270cb54f6291bab4c42e4f9231bc3f' if target=='mafe' else 'python:3.11'),'-c',bootstrap,job,*worker[1:]]
    elif target=='aquario':worker=['sudo','-n',*worker]
    code="import os,subprocess,sys; os.umask(0o077); os.chdir(sys.argv[1]); f=open('job.log','ab',buffering=0); p=subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True); print(p.pid)"
    return remote(target,['/usr/bin/python3','-c',code,job,*worker]).decode().strip()
def status(target,job):
    code="import pathlib,sys; p=pathlib.Path(sys.argv[1]); print((p/'result.json').read_text() if (p/'result.json').exists() else '{\"ok\":null,\"running\":true}')"
    argv=['/usr/bin/python3','-c',code,job]
    if target=='aquario':argv=['sudo','-n',*argv]
    # Result files belong to root for mafe; docker-only sudo reads them through chroot.
    if target in ('mafe','tanque'):
        code="import pathlib,sys; p=pathlib.Path('/host'+sys.argv[1])/'result.json'; print(p.read_text() if p.exists() else '{\"ok\":null,\"running\":true}')"
        argv=['sudo','-n','docker','run','--rm','--network','none','-v','/:/host:ro','--entrypoint','python3',remote(target,['sudo','-n','docker','inspect','--format','{{.Image}}','hermes-carla' if target=='tanque' else 'hermes-mafe']).decode().strip(),'-c',code,job]
    return json.loads(remote(target,argv))
def main():
    p=argparse.ArgumentParser();p.add_argument('--repo-ref',required=True);p.add_argument('--target',choices=[*DEFAULT,'tanque','all'],default='all');p.add_argument('--accept-turns',action='store_true');p.add_argument('--artifacts',type=Path,required=True);p.add_argument('--image');p.add_argument('--detach',action='store_true');p.add_argument('--status',metavar='JOB');p.add_argument('--rollback',metavar='JOB')
    a=p.parse_args()
    if not re.fullmatch('[0-9a-f]{40}',a.repo_ref):p.error('--repo-ref must be exact 40 lowercase hex')
    dest=a.artifacts.resolve()
    if dest==ROOT or ROOT in dest.parents:p.error('Artifacts must be outside repository')
    dest.mkdir(mode=0o700,parents=True,exist_ok=True);dest.chmod(0o700);os.umask(0o077)
    if (a.status or a.rollback) and a.target=='all':p.error('Job commands require one --target')
    for value in (a.status,a.rollback):
        if value and not re.fullmatch(r'(?:/tmp/hermes-provider-ops-|/var/lib/hermes-provider-ops/jobs/)[a-z0-9-]+',value):p.error('Invalid job path')
    failed=False
    payload=None if a.status or a.rollback else bundle(a.repo_ref)
    for target in DEFAULT if a.target=='all' else (a.target,):
        try:
            job=a.status or a.rollback or ('/tmp/hermes-provider-ops-' if target=='luna' else '/var/lib/hermes-provider-ops/jobs/')+target+'-'+uuid.uuid4().hex
            if a.status:result=status(target,job)
            elif a.rollback:
                # Reuse the original private worker/artifacts and detach the restore too.
                launch(target,job,a.repo_ref,a.image,False,rollback=True)
                result={'ok':None,'job':job,'rollback':'started'}
            else:
                remote(target,['test','-x','/usr/bin/python3'])
                if target in ('mafe','tanque'):
                    helper="import os,sys,tarfile; os.umask(0o077); p='/host'+sys.argv[1]; os.makedirs(p); os.chown(p,int(sys.argv[2]),int(sys.argv[3])); tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz').extractall(p,filter='data'); [os.chown(os.path.join(b,n),int(sys.argv[2]),int(sys.argv[3])) for b,ds,fs in os.walk(p) for n in ds+fs]"
                    uid=remote(target,['id','-u']).decode().strip();gid=remote(target,['id','-g']).decode().strip()
                    remote(target,['sudo','-n','docker','run','--rm','-i','--network','none','-v','/:/host','--entrypoint','python3',a.image or 'sha256:c1c56809ca97db6d2ce4862dabda6c8cda270cb54f6291bab4c42e4f9231bc3f','-c',helper,job,uid,gid],input=payload)
                else:
                    if target=='aquario':
                        uid=remote(target,['id','-u']).decode().strip();gid=remote(target,['id','-g']).decode().strip()
                        remote(target,['sudo','-n','/usr/bin/python3','-c',"import os,sys; os.makedirs(sys.argv[1]); os.chown(sys.argv[1],int(sys.argv[2]),int(sys.argv[3]))",job,uid,gid])
                    remote(target,['sh','-c','umask 077; mkdir -p '+shlex.quote(job)+' && tar -xzf - -C '+shlex.quote(job)],input=payload)
                remote(target,['git','clone',job+'/release.bundle',job+'/release'])
                remote(target,['git','-C',job+'/release','checkout','--detach',a.repo_ref])
                pid=launch(target,job,a.repo_ref,a.image,a.accept_turns)
                result={'ok':None,'job':job,'pid':pid}
                if not a.detach:
                    end=time.monotonic()+270
                    while time.monotonic()<end:
                        result=status(target,job)
                        if result.get('ok') is not None:break
                        time.sleep(3)
                    result['job']=job
            (dest/(target+'.json')).write_text(json.dumps(result,indent=2))
            failed |= result.get('ok') is False
            print(target+': '+('falhou' if result.get('ok') is False else 'ok' if result.get('ok') else 'job '+job))
        except Exception as e:
            failed=True;(dest/(target+'.json')).write_text(json.dumps({'ok':False,'error':redact(str(e)),'traceback':exception()}))
            log(target+': falhou:'+exception())
    return int(failed)
if __name__=='__main__':sys.exit(main())
