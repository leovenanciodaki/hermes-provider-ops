"""Generate only against the verified read-only reference; never modifies it."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REF=ROOT.parent/'frota-hermes-upstream'
def changes():
    p='agent/fallback_cooldown.py';a=(REF/p).read_text();b=a.replace('    agent._rate_limited_until = time.monotonic() + backoff_seconds','    agent._provider_ops_cooldown_armed_at = time.time()\n    agent._rate_limited_until = time.monotonic() + backoff_seconds')
    yield p,a,b
    p='agent/agent_runtime_helpers.py';a=(REF/p).read_text()
    needle='    if getattr(agent, "_rate_limited_until", 0) > time.monotonic():\n        return False  # primary still in rate-limit cooldown, stay on fallback'
    replacement='''    if getattr(agent, "_rate_limited_until", 0) > time.monotonic():
        # Explicit native reset after this session's arm, for this primary route only.
        try:
            from hermes_cli.auth import read_credential_pool
            armed = getattr(agent, "_provider_ops_cooldown_armed_at", 0)
            fresh = _load_primary_pool() if armed else None
            rows = read_credential_pool(primary_provider) if fresh is not None else []
            reset_ids = {row.get("id") for row in rows
                         if isinstance(row, dict) and isinstance(row.get("status_cleared_at"), (int, float))
                         and row["status_cleared_at"] > armed
                         and row["status_cleared_at"] > (row.get("last_status_at") or 0)}
            if fresh is not None and reset_ids:
                with fresh._lock:
                    available, _ = fresh._available_entries(model=primary_model or None)
                if any(entry.id in reset_ids for entry in available):
                    agent._credential_pool = fresh
                    agent._rate_limited_until = 0
        except Exception:
            pass
    if getattr(agent, "_rate_limited_until", 0) > time.monotonic():
        return False  # primary still in rate-limit cooldown, stay on fallback'''
    assert a.count(needle)==1
    yield p,a,a.replace(needle,replacement)
if __name__=='__main__':
    rows=[]
    for p,a,b in changes():
        rows.append({'path':p,'before':hashlib.sha256(a.encode()).hexdigest(),'after':hashlib.sha256(b.encode()).hexdigest()})
        # Store minimal replacement, not vendored core source.
        import difflib
        (ROOT/'patches'/ (Path(p).stem+'.patch')).write_text(''.join(difflib.unified_diff(a.splitlines(True),b.splitlines(True),fromfile=p,tofile=p)))
    (ROOT/'patches/manifest.json').write_text(json.dumps({'core_sha':'3b421578588243f920886e6fe79a6faa87ea026e','files':rows},indent=2)+'\n')
