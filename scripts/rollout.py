#!/usr/bin/env python3
"""Deterministic closed-scope rollout entry point. Fail closed on unverified adapters."""
import argparse,json,os,re,shlex,sys
from pathlib import Path
DEFAULT=('manu','mafe','luna','aquario')
CONFIG='/home/hermes/.hermes/profiles/carla/home/.ssh/config_carla_infra'
SHA='3b421578588243f920886e6fe79a6faa87ea026e'

def ssh_argv(target,remote):
    # Each hop quotes exactly one fixed remote command; user text is never interpolated.
    if target in ('mafe','tanque','aquario'):return ['ssh','-F',CONFIG,target,shlex.join(remote)]
    mac=['ssh','-i','/srv/hermes-opt-data/carla-scratch/ssh/macmini_ed25519','leonardovenancio@100.87.54.99']
    if target=='manu':mac += [shlex.join(['ssh','hostinger-vps',shlex.join(['sudo','-u','manuella',*remote])])]
    elif target=='luna':mac += [shlex.join(remote)]
    else:raise ValueError('Unknown target')
    return ['ssh','-F',CONFIG,'aquario',shlex.join(mac)]

def main():
    p=argparse.ArgumentParser();p.add_argument('--repo-ref',required=True);p.add_argument('--target',choices=[*DEFAULT,'tanque','all'],default='all');p.add_argument('--accept-turns',action='store_true');p.add_argument('--test-alert',action='store_true');p.add_argument('--artifacts',type=Path,required=True)
    a=p.parse_args()
    if not re.fullmatch('[0-9a-f]{40}',a.repo_ref):p.error('--repo-ref must be an exact SHA')
    repo=Path(__file__).resolve().parents[1];dest=a.artifacts.resolve()
    if dest==repo or repo in dest.parents:p.error('Private artifacts must be outside repository')
    dest.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(dest,0o700)
    targets=DEFAULT if a.target=='all' else (a.target,)
    for target in targets:
        # There is no verified service transaction adapter in this release. Never perform a
        # partial backup/install or fabricate readiness/acceptance to cover that missing work.
        result=dict(target=target,before=None,after=None,plugin='provider-ops',version='1.0.0',service='not checked',platforms={},excluded=['tanque'] if target!='tanque' else [],backup=None,rollback=False,error='UNVERIFIED: complete backup/recreate/readiness/rollback adapter unavailable; no remote operation attempted',turn='missing acceptance',test_alert='not sent',core_sha=SHA,repo_ref=a.repo_ref)
        fd=os.open(dest/(target+'.json'),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as out:json.dump(result,out,indent=2)
        print(target+': falhou:'+result['error'])
    return 1
if __name__=='__main__':sys.exit(main())
