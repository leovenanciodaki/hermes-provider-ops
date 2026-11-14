"""One authenticated route added to the existing Aquarium home-office proxy."""
import json, pathlib, hashlib, hmac, subprocess
CONTAINER='hermes-agent-l0fn2jx4qutmcpf774xsi6no'
BACKEND='/home/hermes/.hermes/kanban/telegram-dispatcher/dispatcher.py'
def handle(client):
    def reply(status,obj):
        body=json.dumps(obj,ensure_ascii=False).encode()
        client.sendall(('HTTP/1.1 %d Result\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (status,len(body))).encode()+body)
    try:
        client.settimeout(180)
        f=client.makefile('rb')
        if f.readline(4097).strip()!=b'POST /_ext/kanban-dispatcher HTTP/1.1':reply(404,{'ok':False});return
        headers={}
        for _ in range(64):
            line=f.readline(8193)
            if line==b'\r\n':break
            k,v=line.decode().split(':',1);headers[k.lower()]=v.strip()
        else:raise ValueError('Headers')
        n=int(headers.get('content-length','0'))
        if 'transfer-encoding' in headers or not 0<n<=262144:raise ValueError('Body')
        cfg=json.loads(pathlib.Path('/etc/aquario-dispatcher.json').read_text())
        token=headers.get('x-aquario-dispatcher-token','')
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),cfg['token_sha256']):reply(401,{'ok':False,'error':'Unauthorized'});return
        q=json.loads(f.read(n))
        if q.get('op') not in ('sync','update','provider-refresh'):raise ValueError('Operation')
        if q.get('op')=='provider-refresh':
            from provider_refresh import provider
            if provider(q.get('update',{})) != q.get('provider') or q.get('provider') not in ('anthropic','openai'):
                reply(403,{'ok':False,'error':'Unauthorized refresh'});return
            # Host-level replay guard, including direct authenticated bridge calls.
            from provider_host import execute
            reply(200,execute(q));return
        q['_token']=token
        p=subprocess.run(['docker','exec','-i','-u','hermes',CONTAINER,'/opt/hermes/.venv/bin/python',BACKEND],input=json.dumps(q),capture_output=True,text=True,timeout=160)
        try:out=json.loads(p.stdout)
        except Exception:out={'ok':False,'error':'Dispatcher backend unavailable'}
        reply(200 if p.returncode==0 else 502,out)
    except Exception as e:
        try:reply(502,{'ok':False,'error':'Dispatcher transport '+type(e).__name__})
        except Exception:pass
    finally:client.close()
