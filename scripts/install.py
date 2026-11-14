#!/usr/bin/env python3
"""Install into one verified native runtime. Caller must create backup first."""
import argparse,hashlib,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def apply_patch(core):
    manifest=json.loads((ROOT/'patches/manifest.json').read_text())
    states=[]
    for row in manifest['files']:
        actual=hashlib.sha256((core/row['path']).read_bytes()).hexdigest()
        if actual not in (row['before'],row['after']):raise RuntimeError('Core preimage mismatch: '+row['path'])
        states.append((row,actual))
    for row,actual in states:
        if actual==row['after']:continue
        subprocess.run(['patch','--batch','--forward','-p0','-i',str(ROOT/'patches'/(Path(row['path']).stem+'.patch'))],cwd=core,check=True,capture_output=True)
        if hashlib.sha256((core/row['path']).read_bytes()).hexdigest()!=row['after']:raise RuntimeError('Postimage mismatch')
    return manifest

def install(core,cli,ref):
    if len(ref)!=40 or any(c not in '0123456789abcdef' for c in ref):raise ValueError('Exact repo SHA required')
    head=subprocess.check_output(['git','-C',str(core),'rev-parse','HEAD'],text=True).strip()
    if head!='3b421578588243f920886e6fe79a6faa87ea026e':raise RuntimeError('Clean runtime SHA required')
    apply_patch(core)
    subprocess.run([cli,'plugins','install','https://github.com/leovenanciodaki/hermes-provider-ops','--ref',ref,'--enable','--yes-deps','--force'],check=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--core',type=Path,required=True);p.add_argument('--cli',required=True);p.add_argument('--repo-ref',required=True);a=p.parse_args();install(a.core,a.cli,a.repo_ref)
