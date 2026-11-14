"""Authenticated host bridge client; no SSH or persistent token."""
import json
import urllib.request
LEO=7414371088
URL='http://10.10.0.10:9900/_ext/kanban-dispatcher'
def valid(update):
    m=update.get('message',{})
    return (isinstance(update.get('update_id'),int) and not isinstance(update.get('update_id'),bool)
        and m.get('from',{}).get('id')==LEO and m.get('chat',{}).get('id')==LEO
        and m.get('chat',{}).get('type')=='private')
def provider(update):
    if not valid(update):return None
    return {'provider refresh anthropic':'anthropic','provider refresh openai':'openai'}.get(update['message'].get('text','').strip())
def forward(update,token):
    name=provider(update)
    if not name:raise ValueError('Unauthorized refresh')
    q={'op':'provider-refresh','provider':name,'update':update,'_token':token}
    request=urllib.request.Request(URL,data=json.dumps(q).encode(),headers={'Content-Type':'application/json','X-Aquario-Dispatcher-Token':token})
    with urllib.request.urlopen(request,timeout=50) as r:return json.load(r)

NAMES=('manu','mafe','luna','aquario','tanque')
def reply_text(out,test=False):
    # Bridge output is restricted to one literal result per installation.
    rows={}
    for line in str(out.get('result','')).splitlines():
        name,sep,value=line.partition(':')
        value=value.strip()
        if sep and name in NAMES:
            rows[name]=value if value in ('liberado','sem pool') else 'falhou:unavailable'
    return ('TESTE\n' if test else '')+'\n'.join(name+': '+rows.get(name,'falhou:unavailable') for name in NAMES)
