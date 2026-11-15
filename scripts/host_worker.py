#!/usr/bin/env python3
"""Private detached host transaction. Invoked by rollout, never on import."""
import argparse, hashlib, json, os, shutil, subprocess, sys, tarfile, time
from pathlib import Path
from urllib.parse import quote
from remote_docker import api, recipe
from install import apply_patch
SHA='3b421578588243f920886e6fe79a6faa87ea026e'
STAGED={'aquario':'sha256:909aab930cdc786c0a65624ab183916d49b9b3137a9962ea9141b15769d926a5','mafe':'sha256:c1c56809ca97db6d2ce4862dabda6c8cda270cb54f6291bab4c42e4f9231bc3f'}
NAMES={'aquario':['hermes-agent-l0fn2jx4qutmcpf774xsi6no'],'mafe':['hermes-mafe'],'tanque':['hermes-carla','hermes-ana']}
EXCLUDE={'cache','sessions','logs','backups','.git','skills','node_modules','.venv','__pycache__','provider-ops-backups','frota-backups','backup','old-backups'}
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
        if p.exists():
            s=json.loads(p.read_text())
            if s.get('gateway_state') in ('running','starting','degraded'):result[str(h)]=s
    return result
def connected(state):
    return {k for k,v in state.get('platforms',{}).items() if v.get('state')=='connected' and (v.get('writer_pid') is None or (v.get('writer_pid'),v.get('writer_start_time',v.get('start_time')))==(state.get('pid'),state.get('start_time')))}
# Known pre-existing platform faults that also fail on the current version; tracked separately.
TOLERATED={'jessy':{'discord'}}
def required(home,state):
    return connected(state)-TOLERATED.get(Path(home).name,set())
def ready(before, after, since):
    from datetime import datetime
    for h,b in before.items():
        s=after.get(h,{})
        try: fresh=datetime.fromisoformat(s.get('started_at',s.get('updated_at','')).replace('Z','+00:00')).timestamp()>=since
        except (ValueError,TypeError):fresh=False
        if not fresh or (s.get('pid'),s.get('start_time'))==(b.get('pid'),b.get('start_time')) or s.get('code_version')!='0.23.1' or s.get('gateway_state')!='running' or not required(h,b)<=connected(s):return False
    return bool(before)
def wait_ready(before, root, since, deadline):
    while time.monotonic()<deadline:
        after=states(root)
        if ready(before,after,since):return after
        time.sleep(2)
    raise RuntimeError('Gateway readiness deadline: '+json.dumps({h:{'missing_platforms':sorted(required(h,b)-connected(after.get(h,{}))), 'state':after.get(h,{}), 'required_new_process': [b.get('pid'),b.get('start_time')]} for h,b in before.items()}))
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
 dirs[:]=sorted(d for d in dirs if d not in {'.venv','.hermes','bin','node_modules','uv-cache','__pycache__'})
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
    before=states(root)
    code="import json,pathlib; out={};\nfor p in pathlib.Path('/proc').iterdir():\n try: out[p.name]=int((p/'stat').read_text().rsplit(')',1)[1].split()[19])\n except (OSError,ValueError,IndexError): pass\nprint(json.dumps(out))"
    processes=json.loads(run(['docker','exec',name,'python3','-c',code]))
    before={h:s for h,s in before.items() if processes.get(str(s.get('pid')))==s.get('start_time')}
    if not before:raise RuntimeError('No active gateway baseline')
    boot=[Path(m['Source']) for m in d['Mounts'] if m['Destination']=='/opt/eveo-migration/boot-baseline.py']
    obsolete=legacy([root,Path('/srv/hermes-opt-data'),Path('/usr/local/sbin')])
    backup=work/'backup.tar.gz'; old=name+'-provider-ops-old-'+work.parent.name
    paths=[*obsolete,*boot]
    for h in homes(root):paths.extend([h/'config.yaml',h/'plugins/provider-ops',h/'plugins/aquario-pool-fastrotate'])
    snapshot(paths,backup)
    image=image_for(d,a.target,a.image,work)
    private(work/'rollback.json',{'name':name,'old':old,'backup':str(backup),'previous_image_id':d['Image'],'previous_image_name':d['Config'].get('Image')})
    stopped=False;renamed=False;made=False
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
        import rollout_support
        since=time.time();cutover_deadline=time.monotonic()+(900 if a.target=='aquario' else 240);rollout_support.DEADLINE=cutover_deadline
        run(['docker','stop','-t','20',name],timeout=30);stopped=True
        run(['docker','rename',name,old]);renamed=True
        api('POST','/containers/create?name='+quote(name),recipe(d,image));made=True
        run(['docker','start',name],timeout=20)
        after=wait_ready(before,root,since,min(cutover_deadline-40,time.monotonic()+(600 if a.target=='aquario' else 180)))
        if a.accept_turns:accept_turn(root,name,work,deadline=cutover_deadline)
        run(['docker','exec','-u','0',name,'rm','-rf',release])
        plugin_readback=readback(root,a.repo_ref)
        backup_result=finish_backup(work,backup)
        import rollout_support
        rollout_support.DEADLINE=None
        return {'ok':True,**backup_result,'plugins':plugin_readback,'before':{h:{'platforms':sorted(connected(s)),'code_sha':s.get('code_sha'),'code_version':s.get('code_version')} for h,s in before.items()},'after':{h:{'platforms':sorted(connected(s)),'code_sha':s.get('code_sha'),'code_version':s.get('code_version')} for h,s in after.items()},'rollback':str(work),'image':image,'turn':'accepted' if a.accept_turns else 'not requested'}
    except BaseException:
        import rollout_support
        rollout_support.DEADLINE=None
        log(exception())
        actions=[]
        if made:actions.append(lambda:run(['docker','rm','-f',name],timeout=20))
        actions.append(lambda:restore(backup))
        if renamed:actions.append(lambda:run(['docker','rename',old,name],timeout=15))
        if stopped:actions.append(lambda:run(['docker','start',name],timeout=20))
        for action in actions:
            try:action()
            except BaseException:log(exception())
        raise

def accept_turn(root, container, work, deadline=None):
    code=Path(__file__).with_name('turn_probe.py').read_text()
    candidates=[h for h,s in states(root).items() if 'api_server' in connected(s)]
    if not candidates:raise RuntimeError('No connected local API server')
    home=Path(candidates[0])
    inside=Path('/home/hermes/.hermes')/home.relative_to(root) if container else home
    argv=['docker','exec','-i','-u','1000' if container=='hermes-agent-l0fn2jx4qutmcpf774xsi6no' else (api('GET','/containers/'+quote(container)+'/json')['Config'].get('User') or '0'),'-e','HERMES_HOME='+str(inside),container,'/opt/hermes/.venv/bin/python','-'] if container else ['env','HERMES_HOME='+str(home),str(CORE_PY(root)),'-']
    output=run(argv,input=code,timeout=min(225,max(1,deadline-time.monotonic())) if deadline else 225)
    private(work/'turn.json',json.loads(output.splitlines()[-1]))

def CORE_PY(root):
    return Path('/opt/manuella/runtime/.venv/bin/python') if str(root).startswith('/opt/manuella') else Path('/Volumes/LeoNvme/hermes-local/versions/v0.23.0/hermes-agent/.venv/bin/python')
def service_transaction(a,work):
    manu=a.target=='manu';root=Path('/opt/manuella' if manu else '/Users/leonardovenancio/.hermes')
    core=Path('/opt/manuella/runtime' if manu else '/Volumes/LeoNvme/hermes-local/versions/v0.23.0/hermes-agent')
    cli=core/'bin/hermes'; before=states(root)
    if not before:raise RuntimeError('No active gateway baseline')
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
    cutover_deadline=time.monotonic()+240
    try:
        old_plugin=root/'plugins/provider-ops'
        same=old_plugin.exists() and run(['git','-C',old_plugin,'rev-parse','HEAD'])==a.repo_ref
        for h in homes(root):native_install(cli,Path(__file__).resolve().parents[1]/'release',a.repo_ref,h)
        apply_patch(core);clean(obsolete,[])
        if changed or not same:
            import rollout_support
            since=time.time();cutover_deadline=time.monotonic()+240;rollout_support.DEADLINE=cutover_deadline
            run(restart,timeout=20);wait_ready(before,root,since,min(cutover_deadline-40,time.monotonic()+180))
        if a.accept_turns:accept_turn(root,None,work,deadline=cutover_deadline)
        plugin_readback=readback(root,a.repo_ref)
        backup_result=finish_backup(work,backup)
        import rollout_support
        rollout_support.DEADLINE=None
        return {'ok':True,**backup_result,'plugins':plugin_readback,'before':before,'after':states(root),'changed':changed or not same,'rollback':str(work),'turn':'accepted' if a.accept_turns else 'not requested'}
    except BaseException:
        import rollout_support
        rollout_support.DEADLINE=None
        log(exception());restore(backup);run(restart,timeout=45);raise

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
    p=argparse.ArgumentParser();p.add_argument('--target',required=True);p.add_argument('--repo-ref',required=True);p.add_argument('--image');p.add_argument('--accept-turns',action='store_true');p.add_argument('--rollback',action='store_true');a=p.parse_args()
    os.umask(0o077);work=Path.cwd()
    (work/'result.json').unlink(missing_ok=True)
    (work/'exit-code').unlink(missing_ok=True)
    try:
        disk_check(work)
        if a.rollback:rollback(work);result={'ok':True,'rolled_back':True}
        elif a.target in NAMES:
            result={'ok':True,'installations':[]}
            for name in NAMES[a.target]:
                sub=work/name;sub.mkdir(exist_ok=True);result['installations'].append(docker_transaction(a,name,sub))
        else:result=service_transaction(a,work)
        code=0
    except BaseException as e:
        detail=exception();log(detail);result={'ok':False,'error':redact(str(e)),'traceback':detail};code=1
    private(work/'result.json',result);(work/'exit-code').write_text(str(code));return code
if __name__=='__main__':sys.exit(main())
