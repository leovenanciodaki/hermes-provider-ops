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
EXCLUDE={'cache','sessions','logs','backups','.git','skills','node_modules','.venv','__pycache__','provider-ops-backups','frota-backups'}
LEGACY={'aquario-pool-fastrotate','anthropic_pool_overlay_20261114','anthropic-rescue-20261110'}
def run(argv, timeout=120, **kw):
    p=subprocess.run([str(x) for x in argv],capture_output=True,text=True,timeout=timeout,**kw)
    if p.returncode:
        private(Path.cwd()/'last-command-error.private.json',{'program':Path(str(argv[0])).name,'returncode':p.returncode,'stderr':p.stderr[-5000:]})
        raise RuntimeError('Command failed: '+Path(str(argv[0])).name)
    return p.stdout.strip()
def private(path, obj):
    path=Path(path); temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj,indent=2));temp.chmod(0o600);temp.replace(path)
def legacy(roots):
    found=[]
    for root in roots:
        for base, dirs, files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if d not in EXCLUDE]
            for leaf in list(dirs)+files:
                if leaf in LEGACY or leaf.startswith('anthropic_pool_unlatch'):
                    found.append(Path(base)/leaf)
                    if leaf in dirs: dirs.remove(leaf)
    return list(dict.fromkeys(found))
def homes(root):
    return [root]+sorted(p for p in (root/'profiles').glob('*') if (p/'config.yaml').exists())
def snapshot(paths, backup):
    # Record absent paths too: rollback must remove newly created plugin/config files.
    paths=list(dict.fromkeys(Path(p) for p in paths))
    private(str(backup)+'.paths', [str(p) for p in paths])
    with tarfile.open(backup,'w:gz') as tar:
        for p in paths:
            if p.exists() or p.is_symlink(): tar.add(p,arcname=str(p).lstrip('/'))
    backup.chmod(0o600)
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
    return {k for k,v in state.get('platforms',{}).items() if v.get('state')=='connected'}
def ready(before, after, since):
    from datetime import datetime
    for h,b in before.items():
        s=after.get(h,{})
        try: fresh=datetime.fromisoformat(s.get('started_at',s.get('updated_at','')).replace('Z','+00:00')).timestamp()>=since
        except (ValueError,TypeError):fresh=False
        if not fresh or (s.get('pid'),s.get('start_time'))==(b.get('pid'),b.get('start_time')) or s.get('code_version')!='0.23.1' or s.get('gateway_state')!='running' or not connected(b)<=connected(s):return False
    return bool(before)
def wait_ready(before, root, since, deadline):
    while time.monotonic()<deadline:
        after=states(root)
        if ready(before,after,since):return after
        time.sleep(2)
    raise RuntimeError('Gateway readiness deadline')
def has_legacy(home):
    config=home/'config.yaml'
    return (home/'plugins/aquario-pool-fastrotate').exists() or (config.exists() and 'aquario-pool-fastrotate' in config.read_text())

def native_install(cli, release, ref, home, prefix=(), legacy_present=None):
    env=['env','HERMES_HOME='+str(home)]
    # Native Git installer clones ONLY the local pinned release, while gateways remain up.
    if legacy_present if legacy_present is not None else has_legacy(home):
        run([*prefix,*env,cli,'plugins','disable','aquario-pool-fastrotate'])
    run([*prefix,*env,cli,'plugins','install','file://'+str(release),'--ref',ref,'--enable','--yes-deps','--force'],timeout=240)

def clean(paths, boot):
    for p in boot:
        lines=p.read_text().splitlines(keepends=True)
        p.write_text(''.join(line for line in lines if not any(x in line for x in (*LEGACY,'anthropic_pool_unlatch','_pool_overlay_subprocess','# Anthropic pool rescue:'))))
    for p in paths:
        if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
        elif p.exists() or p.is_symlink():p.unlink()
def verify_image(image):
    code="import hashlib,json,pathlib; m=json.loads(pathlib.Path('/release/patches/manifest.json').read_text()); assert all(hashlib.sha256((pathlib.Path('/opt/hermes')/r['path']).read_bytes()).hexdigest()==r['after'] for r in m['files']); import hermes_cli; from hermes_cli.version_info import get_code_identity; assert get_code_identity()['version']=='0.23.1' and get_code_identity()['sha']=='3b421578588243f920886e6fe79a6faa87ea026e'"
    run(['docker','run','--rm','--network','none','--user','0','-w','/opt/hermes','-v',str(Path(__file__).resolve().parents[1])+':/release:ro','--entrypoint','/opt/hermes/.venv/bin/python',image,'-c',code])
def image_for(d, target, image, work):
    candidate=image or STAGED.get(target)
    if candidate:
        verify_image(candidate);return candidate
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
        run(['docker','cp',str(core)+'/.',stage+':/opt/hermes'])
        result=run(['docker','commit',stage]);verify_image(result);return result
    finally:run(['docker','rm','-f',stage])
def docker_transaction(a,name,work):
    d=api('GET','/containers/'+quote(name)+'/json');root=Path(next(m['Source'] for m in d['Mounts'] if m['Destination']=='/home/hermes/.hermes'))
    before=states(root)
    if not before:raise RuntimeError('No active gateway baseline')
    boot=[Path(m['Source']) for m in d['Mounts'] if m['Destination']=='/opt/eveo-migration/boot-baseline.py']
    obsolete=legacy([root,Path('/srv/hermes-opt-data'),Path('/usr/local/sbin')])
    backup=work/'backup.tar.gz'; old=name+'-provider-ops-old-'+work.name
    image=image_for(d,a.target,a.image,work)
    paths=[*obsolete,*boot]
    for h in homes(root):paths.extend([h/'config.yaml',h/'.env',h/'plugins/provider-ops',h/'plugins/aquario-pool-fastrotate'])
    snapshot(paths,backup);private(work/'inspect.json',d)
    private(work/'rollback.json',{'name':name,'old':old,'backup':str(backup)})
    stopped=False;renamed=False;made=False
    try:
        # Copy one local Git release into live runtime. No network/dependencies at cut.
        release='/tmp/provider-ops-release-'+work.name
        run(['docker','cp',str(Path(__file__).resolve().parents[1]/'release'),name+':'+release])
        runtime_user='1000:1000' if a.target=='aquario' else (d['Config'].get('User') or '0')
        run(['docker','exec','-u','0',name,'chown','-R',runtime_user,release])
        run(['docker','exec','-u','0',name,'chmod','-R','a+rX',release])
        for h in homes(root):
            inside=Path('/home/hermes/.hermes')/h.relative_to(root)
            native_install('/opt/hermes/bin/hermes',Path(release),a.repo_ref,inside,
                ['docker','exec','-u',runtime_user,name],legacy_present=has_legacy(h))
        clean(obsolete,boot)
        since=time.time();deadline=time.monotonic()+200
        run(['docker','stop','-t','20',name],timeout=30);stopped=True
        run(['docker','rename',name,old]);renamed=True
        api('POST','/containers/create?name='+quote(name),recipe(d,image));made=True
        run(['docker','start',name],timeout=20)
        after=wait_ready(before,root,since,deadline)
        if a.accept_turns:accept_turn(root,name,work)
        return {'ok':True,'before':{h:sorted(connected(s)) for h,s in before.items()},'after':{h:sorted(connected(s)) for h,s in after.items()},'rollback':str(work),'image':image,'turn':'accepted' if a.accept_turns else 'not requested'}
    except BaseException:
        if made:run(['docker','rm','-f',name],timeout=20)
        restore(backup)
        if renamed:run(['docker','rename',old,name],timeout=15)
        if stopped:run(['docker','start',name],timeout=20)
        raise

def accept_turn(root, container, work):
    code=Path(__file__).with_name('turn_probe.py').read_text()
    candidates=[h for h,s in states(root).items() if 'api_server' in connected(s)]
    if not candidates:raise RuntimeError('No connected local API server')
    home=Path(candidates[0])
    inside=Path('/home/hermes/.hermes')/home.relative_to(root) if container else home
    argv=['docker','exec','-i','-u','1000' if container=='hermes-agent-l0fn2jx4qutmcpf774xsi6no' else (api('GET','/containers/'+quote(container)+'/json')['Config'].get('User') or '0'),'-e','HERMES_HOME='+str(inside),container,'/opt/hermes/.venv/bin/python','-'] if container else ['env','HERMES_HOME='+str(home),str(CORE_PY(root)),'-']
    output=run(argv,input=code,timeout=45)
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
    for h in homes(root):paths.extend([h/'config.yaml',h/'.env',h/'plugins/provider-ops',h/'plugins/aquario-pool-fastrotate'])
    obsolete=legacy([root]);paths+=obsolete
    backup=work/'backup.tar.gz';snapshot(paths,backup)
    restart=['sudo','-n','systemctl','restart','hermes-manuella'] if manu else ['launchctl','kickstart','-k','gui/'+str(os.getuid())+'/ai.hermes.gateway']
    private(work/'rollback.json',{'backup':str(backup),'restart':restart})
    try:
        old_plugin=root/'plugins/provider-ops'
        same=old_plugin.exists() and run(['git','-C',old_plugin,'rev-parse','HEAD'])==a.repo_ref
        for h in homes(root):native_install(cli,Path(__file__).resolve().parents[1]/'release',a.repo_ref,h)
        apply_patch(core);clean(obsolete,[])
        if changed or not same:
            since=time.time();run(restart);wait_ready(before,root,since,time.monotonic()+240)
        if a.accept_turns:accept_turn(root,None,work)
        return {'ok':True,'changed':changed or not same,'rollback':str(work),'turn':'accepted' if a.accept_turns else 'not requested'}
    except BaseException:
        restore(backup);run(restart);raise

def rollback(work):
    if not (work/'rollback.json').exists():
        for child in sorted(work.iterdir()):
            if child.is_dir() and (child/'rollback.json').exists():rollback(child)
        return
    r=json.loads((work/'rollback.json').read_text())
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
        if a.rollback:rollback(work);result={'ok':True,'rolled_back':True}
        elif a.target in NAMES:
            result={'ok':True,'installations':[]}
            for name in NAMES[a.target]:
                sub=work/name;sub.mkdir(exist_ok=True);result['installations'].append(docker_transaction(a,name,sub))
        else:result=service_transaction(a,work)
        code=0
    except BaseException as e:result={'ok':False,'error':type(e).__name__};code=1
    private(work/'result.json',result);(work/'exit-code').write_text(str(code));return code
if __name__=='__main__':sys.exit(main())
