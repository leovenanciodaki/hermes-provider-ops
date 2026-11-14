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
from datetime import datetime, timezone, timedelta
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

def installation_root(home):
    home = Path(home)
    if home.parent.name == 'profiles':
        return home.parent.parent
    return home


def _merge_state(root, path):
    state = {'checked': 0, 'sent': {}}
    sources = [root/'provider-ops'/'state.json']
    sources.extend(sorted((root/'profiles').glob('*/provider-ops/state.json')))
    for source in sources:
        if source.exists():
            old = json.loads(source.read_text())
            state['checked'] = max(state['checked'], old.get('checked', 0))
            for key, mid in old.get('sent', {}).items():
                state['sent'].setdefault(key, mid)
    atomic(path, state)
    return state


def migrate_state(root):
    """Merge installation and profile history without sending notifications."""
    root = installation_root(root)
    folder = root/'provider-ops'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder/'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _merge_state(root, folder/'state.json')


def _hash(parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def check(home, token, *, fetch=fetch_usage, send=telegram, pools=None, thresholds=(85,95), test=False, interval=300, now=None):
    """Injectable helper; explicit test overrides use isolated state."""
    if not test and (thresholds != (85,95) or interval != 300):
        raise ValueError('Overrides require explicit test=True')
    if not token: return []
    if pools is None:
        from agent.credential_pool import load_pool
        pools = load_pool
    root = installation_root(home)
    folder = root/'provider-ops'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder/('test-state.json' if test else 'state.json')
    ids = []
    with (folder/'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = (json.loads(path.read_text()) if path.exists() else {'checked':0,'sent':{}}) if test else _merge_state(root, path)
        stamp = time.time() if now is None else now
        if stamp - state['checked'] < interval: return []
        state['checked'] = stamp
        atomic(path,state)
        for provider in ('anthropic','openai-codex'):
            for entry in pools(provider).entries():
                try:
                    payload = fetch(provider,entry)
                    for name,pct,reset in windows(provider,payload):
                        label = str(getattr(entry, 'label', '') or entry.id).replace('\n',' ')[:100]
                        bucket = int((reset.timestamp() + 1800) // 3600)
                        base = [provider, label, name, bucket]
                        notified = []
                        for threshold in thresholds:
                            key = _hash(base + [threshold])
                            if key in state['sent']:
                                notified.append(threshold)
                                continue
                            for offset in range(-3600, 3601) if state['sent'] else ():
                                old = _hash([provider,entry.id,name,(reset + timedelta(seconds=offset)).isoformat(),threshold])
                                if old in state['sent']:
                                    state['sent'][key] = state['sent'][old]
                                    notified.append(threshold)
                                    break
                        atomic(path, state)
                        crossed = [t for t in thresholds if pct >= t]
                        if not crossed: continue
                        threshold = max(crossed)
                        if notified and threshold <= max(notified): continue
                        brand = 'Codex' if provider == 'openai-codex' else 'Anthropic'
                        window = {'primary_window':'de 5 horas', 'five_hour':'de 5 horas',
                                  'secondary_window':'semanal', 'seven_day':'semanal'}.get(name, name)
                        when = reset.astimezone(ZoneInfo('America/Sao_Paulo'))
                        text = f"{'TESTE ' if test else ''}Conta {label} do {brand} em {pct:g}% da janela {window}. Zera {when:%d/%m} às {when:%H:%M}."
                        mid = send(token,text)
                        if type(mid) is not int: raise ValueError('Missing Telegram message id')
                        ids.append(mid)
                        state['sent'][_hash(base + [threshold])] = mid
                        atomic(path,state)
                        if test: return ids
                except Exception:
                    LOG.warning('Provider quota check/send unavailable')
        return ids


def post_llm_call(*args, **kwargs):
    try:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
        owner = root/'provider-ops'/'alerts-owner'
        if root != Path('/opt/manuella') or not owner.is_file() or owner.read_text() != 'manu':
            return
        from gateway.platforms._shared import get_scoped_secret
        token = get_scoped_secret('TELEGRAM_BOT_TOKEN', '')
        context = contextvars.copy_context()
        def work():
            try: context.run(check,root,token)
            except Exception: LOG.warning('Provider quota worker unavailable')
        threading.Thread(target=work,daemon=True,name='provider-quota').start()
    except Exception: LOG.warning('Provider quota hook unavailable')
