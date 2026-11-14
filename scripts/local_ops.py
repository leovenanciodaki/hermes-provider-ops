#!/usr/bin/env python3
"""Stdlib launcher; native CLI owns bootstrap and auth store writes."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PROVIDERS = {'anthropic':'anthropic','openai':'openai-codex'}
def diagnostic(text):
    text = re.sub(r'(?i)(bearer\s+\S+|sk-[\w-]+|eyJ[\w.-]+|(?:token|key|secret|password)\s*[=:]\s*\S+)', '[REDACTED]', str(text))
    return ' '.join(text.split())[:280]

def refresh(provider, cli='hermes', run=subprocess.run):
    if provider not in PROVIDERS: raise ValueError('Unsupported provider')
    # Native root semantics, without importing optional YAML into system Python.
    # Native root helper is stdlib-only and honors HERMES_HOME profile nesting.
    for native in (Path(cli).parent.parent,Path(cli).resolve().parent,Path(cli).resolve().parent.parent):
        if (native/'hermes_constants.py').exists():
            sys.path.insert(0,str(native));break
    from hermes_constants import get_default_hermes_root
    root = get_default_hermes_root()
    names = ['default'] + sorted(p.name for p in (root/'profiles').iterdir() if p.is_dir() and (p/'config.yaml').exists()) if (root/'profiles').is_dir() else ['default']
    results = []
    for name in dict.fromkeys(names):
        home=root if name=='default' else root/'profiles'/name
        store=home/'auth.json'
        local=json.loads(store.read_text()).get('credential_pool',{}).get(PROVIDERS[provider],[]) if store.exists() else []
        if not local:
            results.append({'profile':name,'status':'sem pool','error':''});continue
        argv = [cli,'--profile',name,'auth','reset',PROVIDERS[provider]]
        p = run(argv,capture_output=True,text=True,timeout=20)
        output = diagnostic(p.stderr or p.stdout)
        empty = bool(re.search(r'(?i)no .*pool|no .*credentials|nenhum|Reset status on 0 ',output))
        results.append({'profile':name,'status':'sem pool' if empty else ('liberado' if p.returncode==0 else 'falhou'),'error':output if p.returncode and not empty else ''})
    return results

def main():
    parser=argparse.ArgumentParser();parser.add_argument('op',choices=['refresh']);parser.add_argument('provider',choices=PROVIDERS);parser.add_argument('--cli',default='hermes')
    a=parser.parse_args()
    try:
        rows=refresh(a.provider,a.cli)
        for row in rows: print(row['profile']+': '+row['status']+(':'+row['error'] if row['error'] else ''))
        return int(any(r['status']=='falhou' for r in rows))
    except Exception as e: print('falhou:'+diagnostic(e));return 1
if __name__=='__main__':sys.exit(main())
