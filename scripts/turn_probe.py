"""One local API turn; stdout is private output consumed by the host worker."""
import json, os, urllib.request
from pathlib import Path
from gateway.config import load_gateway_config, Platform
from hermes_constants import get_hermes_home
from agent.secret_scope import get_secret
home=Path(get_hermes_home())
from dotenv import load_dotenv
load_dotenv(home/'.env',override=False)
# A model acceptance must not generate quota Telegram notifications.
import time
for candidate in [home]+list((home/'profiles').glob('*')):
 folder=candidate/'provider-ops';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
 state=folder/'state.json';prior=json.loads(state.read_text()) if state.exists() else {'sent':{}}
 prior['checked']=time.time();state.write_text(json.dumps(prior));state.chmod(0o600)
config=load_gateway_config()
platform=config.platforms.get(Platform.API_SERVER)
if not platform or not platform.enabled:raise RuntimeError('Local API server unavailable')
extra=platform.extra
key=extra.get('key') or get_secret('API_SERVER_KEY','') or os.environ.get('API_SERVER_KEY','')
port=int(extra.get('port') or os.environ.get('API_SERVER_PORT','8642'))
if not key:raise RuntimeError('Local API authentication unavailable')
request=urllib.request.Request('http://127.0.0.1:'+str(port)+'/v1/chat/completions',
    data=json.dumps({'model':'hermes-agent','messages':[{'role':'user','content':'Reply exactly FROTA_OK'}],'stream':False}).encode(),
    headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
with urllib.request.urlopen(request,timeout=40) as response:result=json.load(response)
text=result['choices'][0]['message']['content']
if 'FROTA_OK' not in text:raise RuntimeError('API acceptance response mismatch')
print(json.dumps({'output':text}))
