"""Shared bounded rollout diagnostics; never hide non-secret failure context."""
import inspect, os, re, time, shlex, subprocess, traceback
from pathlib import Path
LIMIT=100*1024*1024
STAGING_LIMIT=50*1024*1024
STEP='startup'
DEADLINE=None
def redact(value):
    if isinstance(value,bytes):value=value.decode(errors='replace')
    value=str(value)
    value=re.sub(r'(?i)(Bearer\s+)\S+',r'\1[REDACTED]',value)
    value=re.sub(r'\bsk-[A-Za-z0-9_-]+','[REDACTED]',value)
    return re.sub(r'''(?i)((?:[\w-]*(?:token|key|password)[\w-]*)["']?\s*[:=]\s*["']?)([^\s,"'\]}]+)''',r'\1[REDACTED]',value)
def log(message):
    message=redact(message)
    print(message,flush=True)
    with (Path.cwd()/'job.log').open('a') as f:f.write(message+'\n');f.flush()
def run(argv, timeout=120, **kw):
    global STEP
    STEP=inspect.currentframe().f_back.f_code.co_name
    argv=[str(x) for x in argv]
    if DEADLINE is not None:timeout=min(timeout,max(0.01,DEADLINE-time.monotonic()))
    env=dict(os.environ,DOCKER_API_VERSION='1.45');env.update(kw.pop('env',{}))
    try:
        p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,env=env,**kw)
    except BaseException as e:
        log(f'step={STEP} command={shlex.join(argv)} timeout/error={e} stdout={getattr(e,"stdout",None)} stderr={getattr(e,"stderr",None)}')
        raise
    if p.returncode:
        message=f'step={STEP} command={shlex.join(argv)} returncode={p.returncode} stdout={p.stdout} stderr={p.stderr}'
        log(message);raise RuntimeError(redact(message))
    return p.stdout.strip()
def exception():
    return redact(traceback.format_exc())
