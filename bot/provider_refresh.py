"""Authenticated host bridge client; no SSH or persistent token."""
import json
import urllib.request
LEO=7414371088
URL='http://100.116.56.44:9900/_ext/kanban-dispatcher'
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
