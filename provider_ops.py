"""Quota observation only. No inference, credential rotation, or configuration writes."""
import contextvars
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LEO = 7414371088
LOG = logging.getLogger(__name__)

def atomic(path, obj):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.quota-')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(obj, out); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def get_json(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return json.load(r)

def fetch_usage(provider, entry):
    key = entry.runtime_api_key
    if not key: return {}
    if provider == 'anthropic':
        from agent.anthropic_credentials import _is_oauth_token
        if not _is_oauth_token(key): return {}
        return get_json('https://api.anthropic.com/api/oauth/usage', {
            'Authorization': 'Bearer '+key, 'Accept': 'application/json',
            'Content-Type': 'application/json', 'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'claude-code/2.1.0'})
    from agent.account_usage import fetch_account_usage
    snap = fetch_account_usage(provider, api_key=key, base_url=entry.base_url, read_only=True)
    return (snap.raw or {}) if snap else {}

def windows(provider, payload):
    rows = payload if provider == 'anthropic' else payload.get('rate_limit', {})
    for name, row in rows.items():
        if not isinstance(row, dict): continue
        pct = row.get('utilization' if provider == 'anthropic' else 'used_percent')
        reset = row.get('resets_at' if provider == 'anthropic' else 'reset_at')
        if isinstance(pct, bool) or not isinstance(pct, (float, int)) or not math.isfinite(pct): continue
        if not 0 <= pct <= 100 or reset is None: continue
        try:
            dt = datetime.fromtimestamp(reset, timezone.utc) if isinstance(reset, (float, int)) else datetime.fromisoformat(str(reset).replace('Z', '+00:00'))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError): continue
        yield name, pct, dt

def telegram(token, text):
    req = urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
        data=json.dumps({'chat_id': LEO, 'text': text}).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r: result = json.load(r)
    if not result.get('ok'): raise RuntimeError('Telegram rejected notification')
    return result['result']['message_id']

def check(home, token, *, fetch=fetch_usage, send=telegram, pools=None, thresholds=(85,95), test=False, interval=300, now=None):
    """Injectable test helper. Overrides require test=True; returns actual message IDs."""
    if not test and (thresholds != (85,95) or interval != 300):
        raise ValueError('Overrides require explicit test=True')
    if not token: return []
    if pools is None:
        from agent.credential_pool import load_pool
        pools = load_pool
    folder = Path(home)/'provider-ops'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder/('test-state.json' if test else 'state.json')
    ids = []
    with (folder/'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {'checked':0,'sent':{}}
        stamp = time.time() if now is None else now
        if stamp - state['checked'] < interval: return []
        state['checked'] = stamp
        atomic(path,state)
        for provider in ('anthropic','openai-codex'):
            for entry in pools(provider).entries():
                try:
                    payload = fetch(provider,entry)
                    for name,pct,reset in windows(provider,payload):
                        for threshold in thresholds:
                            identity = json.dumps([provider,entry.id,name,reset.isoformat(),threshold])
                            key = hashlib.sha256(identity.encode()).hexdigest()
                            if pct < threshold or key in state['sent']: continue
                            label = str(entry.label).replace('\n',' ')[:100]
                            text = f"{'TESTE ' if test else ''}{provider} · {label} · {name}: {pct:g}% (≥{threshold}%) · reset {reset.astimezone(ZoneInfo('America/Sao_Paulo')):%d/%m/%Y %H:%M %Z}"
                            mid = send(token,text)
                            if not isinstance(mid,int): raise ValueError('Missing Telegram message id')
                            ids.append(mid); state['sent'][key] = mid; atomic(path,state)
                            if test: return ids  # one explicit TESTE maximum
                except Exception:
                    # Do not log exceptions: HTTP exceptions may contain token-bearing URLs.
                    LOG.warning('Provider quota check/send unavailable')
        return ids

def post_llm_call(*args, **kwargs):
    try:
        from hermes_constants import get_hermes_home
        from gateway.platforms._shared import get_scoped_secret
        home = get_hermes_home()
        token = get_scoped_secret('TELEGRAM_BOT_TOKEN', '')
        context = contextvars.copy_context()
        def work():
            try: context.run(check,home,token)
            except Exception: LOG.warning('Provider quota worker unavailable')
        threading.Thread(target=work,daemon=True,name='provider-quota').start()
    except Exception: LOG.warning('Provider quota hook unavailable')
