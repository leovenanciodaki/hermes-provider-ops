#!/usr/bin/env python3
"""Private detached host transaction. Invoked by rollout, never on import."""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tarfile, time
from pathlib import Path
from urllib.parse import quote
from remote_docker import api, recipe
from install import apply_patch
SHA='3b421578588243f920886e6fe79a6faa87ea026e'
STAGED={'aquario':'sha256:909aab930cdc786c0a65624ab183916d49b9b3137a9962ea9141b15769d926a5','mafe':'sha256:c1c56809ca97db6d2ce4862dabda6c8cda270cb54f6291bab4c42e4f9231bc3f'}
NAMES={'aquario':['hermes-agent-l0fn2jx4qutmcpf774xsi6no'],'mafe':['hermes-mafe'],'tanque':['hermes-carla','hermes-ana']}
CACHE_DIRS={'cache','.cache','uv-cache','pip-cache','.uv-cache','.pytest_cache','.mypy_cache','.ruff_cache'}
EXCLUDE=CACHE_DIRS|{'sessions','logs','backups','.git','skills','node_modules','.venv','__pycache__','provider-ops-backups','frota-backups','backup','old-backups'}
COOLDOWN=6*60*60
class RetainedUpdate(RuntimeError):
    """Incomplete update: keep the current version, never retry or restart."""
class GatewayUnavailable(RuntimeError):pass
class NoRealTurn(RuntimeError):pass
class RolloutRefused(RuntimeError):
    def __init__(self,message,active_job=None):
        super().__init__(message);self.active_job=active_job

def acquire_lock(work, force=False):
    # One lock per host, not per target/job; hold the fd until result publication.
    import fcntl
    path=(work.parent if work.name.startswith('hermes-provider-ops-') else work.parent.parent)/'hermes-provider-ops.rollout.lock'
    stream=path.open('a+')
    try:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stream.seek(0)
        try:previous=json.loads(stream.read() or '{}')
        except (ValueError,TypeError):
            if not force:raise RolloutRefused('Rollout refused: invalid cooldown marker; inspect or use --force')
            previous={}
        now=time.time()
        if not force and now-previous.get('started_at',0)<COOLDOWN:
            raise RolloutRefused('Rollout refused: previous execution within 6h; use --force')
        stream.seek(0);stream.truncate();json.dump({'started_at':now,'job':str(work),'pid':os.getpid()},stream)
        stream.flush();os.fsync(stream.fileno());return stream
    except BlockingIOError:
        stream.seek(0)
        try:active_job=json.loads(stream.read() or '{}').get('job')
        except ValueError:active_job=None
        stream.close();raise RolloutRefused('Rollout refused: another execution is active (even with --force)',active_job=active_job)
    except BaseException:
        stream.close();raise

LEGACY={'aquario-pool-fastrotate','anthropic_pool_overlay_20261114','anthropic-rescue-20261110'}
from rollout_support import run, log, redact, exception, LIMIT, STAGING_LIMIT
def private(path, obj):
    path=Path(path); temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj,indent=2));temp.chmod(0o600);temp.replace(path)
def legacy(roots):
    found=[]
    for root in roots:
        for base, dirs, files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if d not in EXCLUDE and 'backup' not in d.lower()]
            for leaf in list(dirs)+files:
                if leaf in LEGACY or leaf.startswith('anthropic_pool_unlatch'):
                    candidate=Path(base)/leaf
                    # Historical snapshots are data, not executable overlay code.
                    # Preserve them in place; never copy or delete old databases.
                    protected=[p for p in candidate.iterdir() if 'backup' in p.name.lower()] if candidate.is_dir() and not candidate.is_symlink() else []
                    if protected:
                        log('Preserving historical legacy backup data in place: '+', '.join(str(p) for p in protected))
                        found.extend(p for p in candidate.iterdir() if p not in protected)
                    else:found.append(candidate)
                    if leaf in dirs: dirs.remove(leaf)
    return list(dict.fromkeys(found))
def homes(root):
    return [root]+sorted(p for p in (root/'profiles').glob('*') if (p/'config.yaml').exists())
def selected_size(paths):
    entries={}
    for p in paths:
        p=Path(p)
        candidates=[p] if not p.is_dir() or p.is_symlink() else p.rglob('*')
        for child in candidates:
            if child.is_file() and not child.is_symlink():entries[str(child)]=child.stat().st_size
    return sum(entries.values()), sorted(entries.items(),key=lambda x:x[1],reverse=True)[:10]
def disk_check(work, required=0):
    free=shutil.disk_usage(work).free
    log(f'disk_free={free} selected_backup={required}')
    if free < required*2+128*1024*1024:raise RuntimeError('Insufficient disk space for backup and reserve')
    return free
def snapshot(paths, backup):
    paths=list(dict.fromkeys(Path(p) for p in paths))
    size,largest=selected_size(paths)
    disk_check(backup.parent,size)
    if size>LIMIT:raise RuntimeError(f'Backup selected files exceed 100MiB: {largest}')
    private(str(backup)+'.paths', [str(p) for p in paths])
    with tarfile.open(backup,'w:gz',compresslevel=1,dereference=False) as tar:
        for p in paths:
            if p.exists() or p.is_symlink():tar.add(p,arcname=str(p).lstrip('/'))
    backup.chmod(0o600)
    if backup.stat().st_size>LIMIT:
        backup.unlink();raise RuntimeError(f'Compressed backup exceeds 100MiB: {largest}')
def readback(root, ref):
    result={}
    for h in homes(root):
        plugin=h/'plugins/provider-ops'
        version=(plugin/'VERSION').read_text().strip()
        installed=run(['git','-c','safe.directory='+str(plugin),'-C',plugin,'rev-parse','HEAD'])
        if version!='1.1.1' or installed!=ref:raise RuntimeError(f'Plugin readback mismatch: {h} {version} {installed}')
        remaining=legacy([h])
        if remaining:raise RuntimeError(f'Legacy paths remain: {remaining}')
        result[str(h)]={'version':version,'ref':installed,'legacy_verified':True}
    return result
def finish_backup(work, backup):
    size=backup.stat().st_size
    backup.unlink();Path(str(backup)+'.paths').unlink()
    r=json.loads((work/'rollback.json').read_text());r.update(backup_deleted=True,backup_bytes=size)
    private(work/'rollback.json',r)
    return {'backup_bytes':size,'backup_deleted':True,'free_final':disk_check(work)}
def restore(backup):
    for p in json.loads(Path(str(backup)+'.paths').read_text()):
        p=Path(p)
        if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
        elif p.exists() or p.is_symlink():p.unlink()
    with tarfile.open(backup) as tar:tar.extractall('/',filter='fully_trusted')
def states(root):
    result={}
    for h in homes(root):
        p=h/'gateway_state.json'
        try:
            s=json.loads(p.read_text())
            if isinstance(s,dict) and s.get('gateway_state') in ('running','starting','degraded'):result[str(h)]=s
        except (OSError,ValueError,TypeError):pass
    return result

def timestamp(value):
    from datetime import datetime
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError):return 0

def connected(state, since=0.0, processes=None):
    if timestamp(state.get('updated_at'))<since:return set()
    def current(v):
        pid=v.get('writer_pid',state.get('pid'))
        start=v.get('writer_start_time',v.get('start_time',state.get('start_time')))
        if processes is not None and processes.get(str(pid))!=start:return False
        if v.get('writer_pid') is not None and (pid,start)!=(state.get('pid'),state.get('start_time')):return False
        return not since or timestamp(v.get('updated_at',state.get('updated_at')))>=since
    return {k for k,v in state.get('platforms',{}).items() if v.get('state')=='connected' and current(v)}

def required(home,state):return connected(state)

def ready(before, after, since):
    # Advisory only. No requirement for a new PID or for messaging recovery.
    return bool(after) and all(s.get('code_version')=='0.23.1' and s.get('gateway_state')=='running' and timestamp(s.get('updated_at'))>=since for s in after.values())

def wait_ready(before, root, since, deadline):
    after={}
    while time.monotonic()<deadline:
        after=states(root)
        if ready(before,after,since):return after
        time.sleep(2)
    raise RuntimeError('Gateway readiness deadline (advisory): '+json.dumps(after))

PROCESS_CODE="""import json,pathlib
out={}
for p in pathlib.Path('/proc').iterdir():
 try: out[p.name]=int((p/'stat').read_text().rsplit(')',1)[1].split()[19])
 except (OSError,ValueError,IndexError): pass
print(json.dumps(out))
"""
def process_table(container):
    argv=['docker','exec',container,'python3','-c',PROCESS_CODE] if container else [sys.executable,'-c',PROCESS_CODE]
    if not container and not Path('/proc').exists():
        argv=[str(CORE_PY(Path('/Users/leonardovenancio/.hermes'))),'-c',"import json,psutil; from gateway.status import get_process_start_time; print(json.dumps({str(p.pid): get_process_start_time(p.pid) for p in psutil.process_iter()}))"]
    return json.loads(run(argv))

def baseline(root, container, since):
    # Diagnostics must never prevent an update on their own.
    try:
        processes=process_table(container)
        return {h:dict(s,platforms={k:v for k,v in s.get('platforms',{}).items() if k in connected(s,since=since,processes=processes)}) for h,s in states(root).items()}
    except Exception:
        log('Baseline unavailable (advisory): '+exception());return {}

def platform_regressions(before, after, since, processes):
    missing={}
    for h,b in before.items():
        s=after.get(h,{})
        if timestamp(s.get('updated_at'))<since:continue
        if processes is not None and processes.get(str(s.get('pid')))!=s.get('start_time'):continue
        fresh={k:v for k,v in s.get('platforms',{}).items()
               if timestamp(v.get('updated_at',s.get('updated_at')))>=since
               and (v.get('writer_pid',s.get('pid')),v.get('writer_start_time',s.get('start_time')))==(s.get('pid'),s.get('start_time'))}
        lost=(connected(b)-connected(dict(s,platforms=fresh),since,processes)) & fresh.keys()
        if lost:missing[h]=sorted(lost)
    return missing
def has_legacy(home):
    config=home/'config.yaml'
    return (home/'plugins/aquario-pool-fastrotate').exists() or (config.exists() and 'aquario-pool-fastrotate' in config.read_text())

def native_install(cli, release, ref, home, prefix=(), legacy_present=None):
    base=home.parent.parent if home.parent.name=='profiles' else home
    profile=home.name if home.parent.name=='profiles' else 'default'
    env=['env','HOME='+str(base.parent),'HERMES_HOME='+str(base)]
    # Native Git installer clones ONLY the local pinned release, while gateways remain up.
    if legacy_present if legacy_present is not None else has_legacy(home):
        try: run([*prefix,*env,cli,'-p',profile,'plugins','disable','aquario-pool-fastrotate'])
        except Exception: pass  # only named in config, never installed here: nothing to disable
    run([*prefix,*env,cli,'-p',profile,'plugins','install','file://'+str(release),'--ref',ref,'--enable','--yes-deps','--force'],timeout=240)

def clean(paths, boot):
    for p in boot:
        lines=p.read_text().splitlines(keepends=True)
        p.write_text(''.join(line for line in lines if not any(x in line for x in (*LEGACY,'anthropic_pool_unlatch','_pool_overlay_subprocess','# Anthropic pool rescue:'))))
    for p in paths:
        if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
        elif p.exists() or p.is_symlink():p.unlink()
# Never follow symlinks or traverse runtime directories when copying source modes.
EXECUTABLE_INVENTORY="""import json,os,stat
out={}
for base,dirs,files in os.walk('/opt/hermes',followlinks=False):
 dirs[:]=sorted(d for d in dirs if d not in {'.venv','.hermes','bin','node_modules','__pycache__', 'cache','.cache','uv-cache','pip-cache','.uv-cache','.pytest_cache','.mypy_cache','.ruff_cache'})
 for name in sorted(files):
  p=os.path.join(base,name); mode=os.lstat(p).st_mode
  if stat.S_ISREG(mode) and mode & 0o111: out[os.path.relpath(p,'/opt/hermes')]=mode & 0o111
print(json.dumps(out,sort_keys=True))
"""
REPAIR_EXECUTABLES="""import json,pathlib,stat,sys
root=pathlib.Path('/opt/hermes'); expected=json.loads(sys.argv[1])
for name,bits in expected.items():
 p=root/name
 if not p.exists() or any(parent.is_symlink() for parent in [p,*p.parents]): continue
 mode=p.lstat().st_mode
 if stat.S_ISREG(mode):
  p.chmod((stat.S_IMODE(mode) & ~0o111) | bits)
  assert p.stat().st_mode & 0o111 == bits, name
"""
def repair_executables(current, stage):
    inventory=run(['docker','run','--rm','--network','none','--entrypoint','python3',current,'-c',EXECUTABLE_INVENTORY])
    run(['docker','exec','-u','0',stage,'python3','-c',REPAIR_EXECUTABLES,inventory])

def smoke_image(image):
    # Pinned dispatcher -> /init -> main-wrapper.sh passes executable CMD through.
    # No host mounts: tmpfs and any image-declared anonymous volumes are disposable.
    container=run(['docker','create','--network','none','--tmpfs','/opt/data:rw',
        '-e','HERMES_HOME=/opt/data',image,'/bin/true'])
    try:
        run(['docker','start','-a',container],timeout=60)
        if run(['docker','inspect','--format','{{.State.ExitCode}}',container])!='0':
            raise RuntimeError('Default entrypoint smoke test failed')
    finally:run(['docker','rm','-f','-v',container],timeout=20)

def verify_core_image(image):
    code="import hashlib,json,pathlib; m=json.loads(pathlib.Path('/release/patches/manifest.json').read_text()); assert all(hashlib.sha256((pathlib.Path('/opt/hermes')/r['path']).read_bytes()).hexdigest()==r['after'] for r in m['files']); import hermes_cli; from hermes_cli.version_info import get_code_identity; assert get_code_identity()['version']=='0.23.1' and get_code_identity()['sha']=='3b421578588243f920886e6fe79a6faa87ea026e'"
    run(['docker','run','--rm','--network','none','--user','0','-w','/opt/hermes','-v',str(Path(__file__).resolve().parents[1])+':/release:ro','--entrypoint','/opt/hermes/.venv/bin/python',image,'-c',code])

def verify_image(image):
    verify_core_image(image)
    smoke_image(image)
def image_for(d, target, image, work):
    candidate=image or STAGED.get(target)
    if candidate:
        if target!='aquario':
            verify_image(candidate);return candidate
        # Verify pinned contents before repairing the staged source's known mode bug.
        verify_core_image(candidate)
        stage=run(['docker','create','--entrypoint','/bin/sleep',candidate,'infinity'])
        try:
            run(['docker','start',stage])
            repair_executables(d['Image'],stage)
            result=run(['docker','commit','--change','ENTRYPOINT '+json.dumps(d['Config']['Entrypoint']),
                '--change','CMD '+json.dumps(d['Config']['Cmd']),stage])
            verify_image(result);return result
        finally:run(['docker','rm','-f',stage])
    import urllib.request
    archive=work/'core.tar.gz'
    urllib.request.urlretrieve('https://github.com/NousResearch/hermes-agent/archive/'+SHA+'.tar.gz',archive)
    source=work/'clean-core';source.mkdir()
    with tarfile.open(archive) as tar:tar.extractall(source,filter='data')
    core=next(source.iterdir());apply_patch(core)
    for keep in ('.venv','.hermes','bin','node_modules'):
        p=core/keep
        if p.is_dir():shutil.rmtree(p)
        elif p.exists():p.unlink()
    private(core/'install-stamp.json',{'commit':SHA,'baseVersion':'0.23.1','displayVersion':'0.23.1','source':'docker','updateMechanism':'external'})
    private(core/'.provider-ops-install.json',{'core_sha':SHA,'version':'0.23.1','patches':json.loads((Path(__file__).resolve().parents[1]/'patches/manifest.json').read_text())})
    (core/'install-stamp.json').chmod(0o644)
    (core/'.provider-ops-install.json').chmod(0o644)
    stage=run(['docker','create','--entrypoint','/bin/sleep',d['Image'],'infinity'])
    try:
        # Keep runtime directories, replace every other core entry with pinned archive.
        run(['docker','start',stage])
        run(['docker','exec','-u','0',stage,'python3','-c',"import pathlib,shutil; r=pathlib.Path('/opt/hermes'); [(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) for p in r.iterdir() if p.name not in {'.venv','.hermes','bin','node_modules'}]"])
        size,largest=selected_size([core])
        if size>=STAGING_LIMIT:raise RuntimeError(f'Container staging exceeds 50MiB: {largest}')
        run(['chmod','-R','a+rX',str(core)])  # job umask is private; the image needs world-readable/executable core
        run(['docker','cp',str(core)+'/.',stage+':/opt/hermes'])
        repair_executables(d['Image'],stage)
        result=run(['docker','commit','--change','ENTRYPOINT '+json.dumps(d['Config']['Entrypoint']),
            '--change','CMD '+json.dumps(d['Config']['Cmd']),stage]);verify_image(result);return result
    finally:run(['docker','rm','-f',stage])
def docker_transaction(a,name,work):
    d=api('GET','/containers/'+quote(name)+'/json');root=Path(next(m['Source'] for m in d['Mounts'] if m['Destination']=='/home/hermes/.hermes'))
    since=getattr(a,'job_started_at',time.time())
    before=baseline(root,name,since)
    boot=[Path(m['Source']) for m in d['Mounts'] if m['Destination']=='/opt/eveo-migration/boot-baseline.py']
    obsolete=legacy([root,Path('/srv/hermes-opt-data'),Path('/usr/local/sbin')])
    backup=work/'backup.tar.gz'; old=name+'-provider-ops-old-'+work.parent.name
    paths=[*obsolete,*boot]
    for h in homes(root):paths.extend([h/'config.yaml',h/'plugins/provider-ops',h/'plugins/aquario-pool-fastrotate'])
    snapshot(paths,backup)
    image=image_for(d,a.target,a.image,work)
    private(work/'rollback.json',{'name':name,'old':old,'backup':str(backup),'previous_image_id':d['Image'],'previous_image_name':d['Config'].get('Image')})
    stopped=False;renamed=False;made=False;turn_attempted=False;accepted=False
    try:
        # Copy one local Git release into live runtime. No network/dependencies at cut.
        release='/tmp/provider-ops-release-'+work.parent.name
        release_size,_=selected_size([Path(__file__).resolve().parents[1]/'release'])
        if release_size>=STAGING_LIMIT:raise RuntimeError('Container staging exceeds 50MiB')
        run(['docker','cp',str(Path(__file__).resolve().parents[1]/'release'),name+':'+release])
        runtime_user='1000:1000' if a.target=='aquario' else (d['Config'].get('User') or '0')
        run(['docker','exec','-u','0',name,'chown','-R',runtime_user,release])
        run(['docker','exec','-u','0',name,'chmod','-R','a+rX',release])
        for h in homes(root):
            inside=Path('/home/hermes/.hermes')/h.relative_to(root)
            native_install('/opt/hermes/bin/hermes',Path(release),a.repo_ref,inside,
                ['docker','exec','-u',runtime_user,name],legacy_present=has_legacy(h))
        clean(obsolete,boot)
        stopped=True;run(['docker','stop','-t','60',name],timeout=120)
        run(['docker','rename',name,old]);renamed=True
        api('POST','/containers/create?name='+quote(name),recipe(d,image));made=True
        run(['docker','start',name],timeout=20)
        turn_attempted=True;accept_turn(root,name,work,since=since);accepted=True
        after=states(root)
        regressions=platform_regressions(before,after,since,process_table(name))
        if regressions:raise RetainedUpdate('Platform regressions (new version retained): '+json.dumps(regressions))
        run(['docker','exec','-u','0',name,'rm','-rf',release])
        plugin_readback=readback(root,a.repo_ref)
        backup_result=finish_backup(work,backup)
        import rollout_support
        rollout_support.DEADLINE=None
        return {'ok':True,**backup_result,'plugins':plugin_readback,'before':{h:{'platforms':sorted(connected(s)),'code_sha':s.get('code_sha'),'code_version':s.get('code_version')} for h,s in before.items()},'after':{h:{'platforms':sorted(connected(s)),'code_sha':s.get('code_sha'),'code_version':s.get('code_version')} for h,s in after.items()},'rollback':str(work),'image':image,'turn':'accepted'}
    except BaseException as error:
        import rollout_support
        rollout_support.DEADLINE=None
        log(exception())
        # A start command timeout is not proof of gateway failure. Probe once.
        if made and not turn_attempted:
            turn_attempted=True
            try:accept_turn(root,name,work,since=since);accepted=True
            except NoRealTurn as probe_error:error=probe_error;log(exception())
            except Exception as probe_error:raise RetainedUpdate(str(probe_error)) from error
        if accepted or not stopped or (made and not isinstance(error,NoRealTurn)):
            raise RetainedUpdate(str(error)) from error
        if stopped and not renamed and not made:
            # A timed-out stop may have left the original gateway healthy.
            try:running=api('GET','/containers/'+quote(name)+'/json').get('State',{}).get('Running')
            except Exception as observation_error:raise RetainedUpdate(str(observation_error)) from error
            if running is not False:raise RetainedUpdate('Stop did not establish an outage; original installation retained: '+str(error)) from error
        # Only total failure to start/respond reaches the one restore below.
        actions=[]
        if made:actions.append(lambda:run(['docker','rm','-f',name],timeout=20))
        actions.append(lambda:restore(backup))
        if renamed:actions.append(lambda:run(['docker','rename',old,name],timeout=15))
        if stopped:actions.append(lambda:run(['docker','start',name],timeout=20))
        for action in actions:
            try:action()
            except BaseException:log(exception())
        raise GatewayUnavailable(str(error)) from error

def accept_turn(root, container, work, deadline=None, since=None):
    code=Path(__file__).with_name('turn_probe.py').read_text()
    # Status files only prioritize candidates. Missing/stale status never vetoes API.
    candidates=list(dict.fromkeys([Path(h) for h,s in states(root).items() if 'api_server' in s.get('platforms',{})]+homes(root)))
    deadline=deadline or time.monotonic()+(900 if container=='hermes-agent-l0fn2jx4qutmcpf774xsi6no' else 300)
    since=since if since is not None else time.time()
    errors=[];indeterminate=not candidates
    user='1000' if container=='hermes-agent-l0fn2jx4qutmcpf774xsi6no' else ((api('GET','/containers/'+quote(container)+'/json')['Config'].get('User') or '0') if container else None)
    for index,home in enumerate(candidates):
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        inside=Path('/home/hermes/.hermes')/home.relative_to(root) if container else home
        budget=max(1,remaining/(len(candidates)-index))
        env=['HERMES_HOME='+str(inside),'PROVIDER_OPS_PROBE_WAIT='+str(max(0,min(600,budget-65))),'PROVIDER_OPS_SINCE='+str(since)]
        argv=['docker','exec','-i','-u',user,'-e',env[0],'-e',env[1],'-e',env[2],container,'/opt/hermes/.venv/bin/python','-'] if container else ['env',*env,str(CORE_PY(root)),'-']
        try:
            output=run(argv,input=code,timeout=int(min(remaining,max(65,budget))))
            result=json.loads(output.splitlines()[-1])
            if result.get('failure'):
                errors.append(redact(str(result)))
                if result['failure']!='api_unavailable':indeterminate=True
                continue
            if result.get('version')!='0.23.1' or not result.get('pid') or not result.get('output'):
                raise RuntimeError('Invalid live API acceptance evidence')
            try:private(work/'turn.json',dict(result,home=str(home)))
            except OSError as error:raise RetainedUpdate('Live turn accepted; evidence write failed: '+str(error)) from error
            if result.get('warnings'):raise RetainedUpdate('Live turn accepted; '+json.dumps(result['warnings']))
            return result
        except RetainedUpdate:raise
        except Exception as error:
            indeterminate=True;errors.append(redact(str(error)))
    if indeterminate:raise RetainedUpdate('API acceptance indeterminate; version retained: '+json.dumps(errors))
    raise NoRealTurn('No real local API turn responded: '+json.dumps(errors))

def CORE_PY(root):
    return Path('/opt/manuella/runtime/.venv/bin/python') if str(root).startswith('/opt/manuella') else Path('/Volumes/LeoNvme/hermes-local/versions/v0.23.0/hermes-agent/.venv/bin/python')
def service_transaction(a,work):
    manu=a.target=='manu';root=Path('/opt/manuella' if manu else '/Users/leonardovenancio/.hermes')
    core=Path('/opt/manuella/runtime' if manu else '/Volumes/LeoNvme/hermes-local/versions/v0.23.0/hermes-agent')
    cli=core/'bin/hermes'
    since=getattr(a,'job_started_at',time.time())
    before=baseline(root,None,since)
    identity=run([str(core/'.venv/bin/python'),'-c',"from hermes_cli.version_info import get_code_identity; import json; print(json.dumps(get_code_identity()))"],cwd=core)
    if json.loads(identity).get('sha')!=SHA:raise RuntimeError('Core SHA mismatch')
    manifest=json.loads((Path(__file__).resolve().parents[1]/'patches/manifest.json').read_text())
    changed=any(hashlib.sha256((core/r['path']).read_bytes()).hexdigest()!=r['after'] for r in manifest['files'])
    paths=[core/r['path'] for r in manifest['files']]
    for h in homes(root):paths.extend([h/'config.yaml',h/'plugins/provider-ops',h/'plugins/aquario-pool-fastrotate'])
    obsolete=legacy([root]);paths+=obsolete
    backup=work/'backup.tar.gz';snapshot(paths,backup)
    restart=['sudo','-n','systemctl','restart','hermes-manuella'] if manu else ['launchctl','kickstart','-k','gui/'+str(os.getuid())+'/ai.hermes.gateway']
    private(work/'rollback.json',{'backup':str(backup),'restart':restart})
    restarted=False;turn_attempted=False;accepted=False
    try:
        old_plugin=root/'plugins/provider-ops'
        same=old_plugin.exists() and run(['git','-C',old_plugin,'rev-parse','HEAD'])==a.repo_ref
        for h in homes(root):native_install(cli,Path(__file__).resolve().parents[1]/'release',a.repo_ref,h)
        apply_patch(core);clean(obsolete,[])
        if changed or not same:
            restarted=True;run(restart,timeout=60)
        turn_attempted=True;accept_turn(root,None,work,since=since);accepted=True
        regressions=platform_regressions(before,states(root),since,process_table(None))
        if regressions:raise RetainedUpdate('Platform regressions (new version retained): '+json.dumps(regressions))
        plugin_readback=readback(root,a.repo_ref)
        backup_result=finish_backup(work,backup)
        import rollout_support
        rollout_support.DEADLINE=None
        return {'ok':True,**backup_result,'plugins':plugin_readback,'before':before,'after':states(root),'changed':changed or not same,'rollback':str(work),'turn':'accepted'}
    except BaseException as error:
        import rollout_support
        rollout_support.DEADLINE=None
        log(exception())
        if restarted and not turn_attempted:
            turn_attempted=True
            try:accept_turn(root,None,work,since=since);accepted=True
            except NoRealTurn as probe_error:error=probe_error;log(exception())
            except Exception as probe_error:raise RetainedUpdate(str(probe_error)) from error
        if accepted or not restarted or not isinstance(error,NoRealTurn):raise RetainedUpdate(str(error)) from error
        restore(backup);run(restart,timeout=60);raise GatewayUnavailable(str(error)) from error

def rollback(work):
    if not (work/'rollback.json').exists():
        for child in sorted(work.iterdir()):
            if child.is_dir() and (child/'rollback.json').exists():rollback(child)
        return
    r=json.loads((work/'rollback.json').read_text())
    if r.get('backup_deleted'):raise RuntimeError('Installation backup deleted; automatic rollback unavailable; previous image/container retained')
    if 'name' in r:
        run(['docker','rm','-f',r['name']]);restore(Path(r['backup']))
        run(['docker','rename',r['old'],r['name']]);run(['docker','start',r['name']])
    else:restore(Path(r['backup']));run(r['restart'])
def main():
    p=argparse.ArgumentParser();p.add_argument('--target',required=True);p.add_argument('--repo-ref',required=True);p.add_argument('--image');p.add_argument('--accept-turns',action='store_true',help='Compatibility flag; API acceptance is always required');p.add_argument('--rollback',action='store_true');p.add_argument('--force',action='store_true');p.add_argument('--execution-id');a=p.parse_args()
    if a.execution_id and not re.fullmatch('[0-9a-f]{32}',a.execution_id):p.error('Invalid execution id')
    os.umask(0o077);work=Path.cwd();a.job_started_at=time.time()
    suffix='-'+a.execution_id if a.execution_id else ''
    result_path=work/('result'+suffix+'.json');exit_path=work/('exit-code'+suffix)
    try:lock=acquire_lock(work,a.force)
    except RolloutRefused as e:
        log(str(e))
        if (a.execution_id or e.active_job!=str(work)) and not result_path.exists():
            private(result_path,{'ok':False,'refused':True,'exit_code':3,'error':str(e)})
            exit_path.write_text('3')
        return 3
    try:
        result_path.unlink(missing_ok=True)
        exit_path.unlink(missing_ok=True)
        try:
            disk_check(work)
            if a.rollback:rollback(work);result={'ok':True,'rolled_back':True}
            elif a.target in NAMES:
                result={'ok':True,'installations':[]}  # type: dict
                for name in NAMES[a.target]:
                    sub=work/name;sub.mkdir(exist_ok=True);result['installations'].append(docker_transaction(a,name,sub))
            else:result=service_transaction(a,work)
            code=0
        except BaseException as e:
            detail=exception();log(detail)
            code=1 if isinstance(e,GatewayUnavailable) else 2
            result={'ok':False,'partial':code==2,'automatic_rollback':code!=2,'error':redact(str(e)),'traceback':detail}
        result['exit_code']=code
        private(result_path,result);exit_path.write_text(str(code));return code
    finally:lock.close()
if __name__=='__main__':sys.exit(main())
