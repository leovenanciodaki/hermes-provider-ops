"""One local API turn; stdout is private output consumed by the host worker."""
import json, os, time, urllib.request, urllib.error

def discover_port(state, fallback):
 from urllib.parse import urlparse
 api=state.get('platforms',{}).get('api_server',{})
 listener=api.get('listener_base') or api.get('metrics',{}).get('listener_base')
 return int(api.get('metrics',{}).get('port') or (urlparse(listener).port if listener else None) or fallback)

def process_start(pid):
 from pathlib import Path
 try:return int(Path('/proc/'+str(pid)+'/stat').read_bytes().rsplit(b')',1)[1].split()[19])
 except (OSError,ValueError,IndexError):
  try:
   from gateway.status import get_process_start_time
   return get_process_start_time(pid)
  except Exception:return None

def status_port(path, fallback, since):
 # A stale/dead platform writer is not even a reliable port hint.
 from datetime import datetime
 try:
  state=json.loads(path.read_text())
  updated=datetime.fromisoformat(state['updated_at'].replace('Z','+00:00')).timestamp()
  if updated<since:return fallback
  pid=state['pid'];os.kill(pid,0)
  if state.get('start_time') is None or process_start(pid)!=state['start_time']:return fallback
  api=state.get('platforms',{}).get('api_server',{})
  if api.get('writer_pid',pid)!=pid:return fallback
  if api.get('writer_start_time',state.get('start_time'))!=state.get('start_time'):return fallback
  if datetime.fromisoformat(api.get('updated_at',state['updated_at']).replace('Z','+00:00')).timestamp()<since:return fallback
  return discover_port(state,fallback)
 except (OSError,ValueError,KeyError,TypeError,AttributeError):return fallback

def validate_health(health):
 if health.get('version')!='0.23.1':raise RuntimeError('Live gateway version mismatch')
 pid=health.get('pid')
 if not isinstance(pid,int) or pid<=0:raise RuntimeError('Missing live gateway PID')
 os.kill(pid,0)
 return pid

def live_turn(port, key, wait=0.0):
 base='http://127.0.0.1:'+str(port)
 headers={'Content-Type':'application/json','Authorization':'Bearer '+key}
 def health():
  with urllib.request.urlopen(urllib.request.Request(base+'/health/detailed',headers=headers),timeout=10) as response:
   result=json.load(response)
  validate_health(result);return result
 until=time.monotonic()+wait
 while True:
  try:before=health();break
  except OSError as error:
   if isinstance(error,urllib.error.HTTPError) and error.code in (401,403):raise
   if time.monotonic()>=until:raise
   time.sleep(0.5)
 request=urllib.request.Request(base+'/v1/chat/completions',
     data=json.dumps({'model':'hermes-agent','messages':[{'role':'user','content':'Reply exactly FROTA_OK'}],'stream':False}).encode(),headers=headers)
 with urllib.request.urlopen(request,timeout=40) as response:result=json.load(response)
 text=result['choices'][0]['message']['content']
 if not isinstance(text,str) or not text.strip():raise RuntimeError('Empty API acceptance response')
 # The response itself is liveness evidence; an extra health/readiness request
 # must not turn a completed inference into a false no-turn rollback.
 return {'output':text,'pid':before['pid'],'version':before['version']}

def main():
 from pathlib import Path
 from gateway.config import load_gateway_config, Platform
 from hermes_constants import get_hermes_home
 from agent.secret_scope import get_secret
 home=Path(get_hermes_home())
 from dotenv import load_dotenv
 load_dotenv(home/'.env',override=False)
 # A model acceptance must not generate quota Telegram notifications.
 import time
 warnings=[]
 try:
  for candidate in [home]+list((home/'profiles').glob('*')):
   folder=candidate/'provider-ops';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
   state=folder/'state.json';prior=json.loads(state.read_text()) if state.exists() else {'sent':{}}
   prior['checked']=time.time();state.write_text(json.dumps(prior));state.chmod(0o600)
 except Exception as error:warnings.append('Notification bookkeeping: '+type(error).__name__)

 config=load_gateway_config()
 platform=config.platforms.get(Platform.API_SERVER)
 if not platform or not platform.enabled:raise RuntimeError('Local API server unavailable')
 extra=platform.extra
 key=extra.get('key') or get_secret('API_SERVER_KEY','') or os.environ.get('API_SERVER_KEY','')
 port=int(extra.get('port') or os.environ.get('API_SERVER_PORT','8642'));configured_port=port
 status_path=home/'gateway_state.json'
 since=float(os.environ.get('PROVIDER_OPS_SINCE','0'))
 import socket
 until=time.monotonic()+float(os.environ.get('PROVIDER_OPS_PROBE_WAIT','180'))
 if not key:raise RuntimeError('Local API authentication unavailable')
 try:
  while True:
   try:
    port=status_port(status_path,configured_port,since)
    with socket.create_connection(('127.0.0.1',port),timeout=1):break
   except OSError:
    if time.monotonic()>until:raise
    time.sleep(0.5)
  result=live_turn(port,key,wait=max(0,until-time.monotonic()))
 except Exception as error:
  print(json.dumps({'failure':'api_unavailable','error':type(error).__name__}));return
 if warnings:result['warnings']=warnings
 print(json.dumps(result))

if __name__=='__main__':
 try:main()
 except Exception as error:
  print(json.dumps({'failure':'probe_infrastructure','error':type(error).__name__}))
