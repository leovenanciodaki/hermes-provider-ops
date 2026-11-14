#!/usr/bin/env python3
"""Aquario approval dispatcher. Token arrives only on stdin from n8n's credential.
Kanban mutations use the installed kernel. No publication/completion operation.
"""
import sys, os, json, time, re, sqlite3, pathlib, fcntl, hashlib, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, '/opt/hermes')
from hermes_cli import kanban_db as kb
LEO = 7414371088
BOT = 9226715934
HOME = pathlib.Path('/home/hermes/.hermes')
ROOT = HOME/'kanban/telegram-dispatcher'
BOARDS = {'d': HOME/'kanban.db', 'm': HOME/'kanban/boards/operacao-max/kanban.db'}
BRANDS = {'festas':'FESTAS', 'maxm':'MAX M', 'guia':'Guia do Áudio', 'maxa':'MAX A', 'transversal':'Transversal'}
TZ = ZoneInfo('America/Sao_Paulo')

def now(): return datetime.now(TZ).strftime('%d/%m/%Y %H:%M:%S BRT')
def stamp(ts): return datetime.fromtimestamp(ts, TZ).strftime('%d/%m/%Y %H:%M BRT')
def audit(event, **data):
    with (ROOT/'audit.jsonl').open('a') as f:
        f.write(json.dumps({'at':now(),'event':event,**data},ensure_ascii=False)+'\n')
def connect(board):
    c=sqlite3.connect(str(BOARDS[board]),timeout=30);c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def detail(c,t,board):
    t=dict(t)
    ev=c.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN ('blocked','triaged','triage') ORDER BY id DESC LIMIT 1",(t['id'],)).fetchone()
    payload=json.loads(ev['payload'] or '{}') if ev else {}
    comments=[dict(r) for r in c.execute('SELECT id,author,body,created_at FROM task_comments WHERE task_id=? ORDER BY id',(t['id'],))]
    reason=payload.get('reason') or ''
    if reason=='initial_status': reason=''
    last=comments[-1]['body'] if comments else ''
    t.update(board=board,reason=reason or last or 'Aguardando a sua decisão.',since=ev['created_at'] if ev else t['created_at'],episode=ev['id'] if ev else 0,comments=comments)
    t['ref']=board+':'+t['id']+':'+str(t['episode'])
    t['links']=list(dict.fromkeys(u.rstrip('.,;:!?)]}>') for u in re.findall(r'https?://[^\s<>"\x27`]+','\n'.join([t.get('body') or '']+[r['body'] for r in comments]))))
    return t

def eligible(t):
    if t['status'] not in ('blocked','triage'):return False
    if t.get('block_kind') in ('capability','dependency','transient'):return False
    if t.get('block_kind')=='needs_input':return True
    text=t.get('reason','')+'\n'+(t['comments'][-1]['body'] if t.get('comments') else '')
    if re.search(r'(?i)(?:sem|não precisa|nao precisa|dispensa).{0,12}(?:ok|aprova).{0,8}\bLeo\b',text):return False
    return bool(re.search(r'(?i)\bLeo\b',text) and re.search(r'(?i)aguard|decid|decis|aprov|autoriz|\bok\b|respost|valid|confirm|escolh|precisa',text))

def pending():
    out=[]
    for b in BOARDS:
        c=connect(b)
        for r in c.execute("SELECT * FROM tasks WHERE status IN ('blocked','triage') ORDER BY created_at"):
            t=detail(c,r,b)
            if eligible(t):out.append(t)
        c.close()
    return out

def get_card(board,tid):
    c=connect(board);r=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
    t=detail(c,r,board) if r else None;c.close();return t

def brand(t):return BRANDS.get(t.get('tenant'), 'Transversal')
def card_text(t):
    text=f"{brand(t)} · {t['title'][:170]}\nAgente: {t.get('assignee') or 'sem agente'} · desde {stamp(t['since'])}\nCard: {t['id']} ({'default' if t['board']=='d' else 'operacao-max'})\n\n{t['reason'][:1500]}"
    links='\n'.join(t['links'])
    if links:text+='\n\nEntrega / referências:\n'+links[:1600]
    return text[:3700]
def keyboard(t):
    p=t['board']+':'+t['id']
    rows=[[{'text':'✅ Aprovar','callback_data':'a:'+p},{'text':'✏️ Pedir ajuste','callback_data':'e:'+p},{'text':'💬 Comentar','callback_data':'c:'+p}]]
    if t['links']:rows.append([{'text':'Abrir entrega','url':t['links'][0]}])
    return {'inline_keyboard':rows}

class Dispatcher:
    def __init__(self,token,state):self.token=token;self.s=state
    def tg(self,method,data):
        for attempt in range(2):
            req=urllib.request.Request('https://api.telegram.org/bot'+self.token+'/'+method,data=json.dumps(data,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=25) as r:out=json.load(r)
            except urllib.error.HTTPError as e:
                try:out=json.loads(e.read())
                except Exception:out={'ok':False,'description':'Telegram HTTP '+str(e.code)}
            except Exception as e:
                # Never render exception URL (contains bot credential).
                audit('telegram_transport_error',method=method,error=type(e).__name__)
                raise RuntimeError('Telegram transport '+type(e).__name__) from None
            if not out.get('ok'):
                err=out.get('description','Telegram error')
                if out.get('error_code')==429 and attempt==0:
                    time.sleep(min(35,out.get('parameters',{}).get('retry_after',1)));continue
                if method=='editMessageText' and 'message is not modified' in err:return {'message_id':data['message_id'],'unchanged':True}
                audit('telegram_error',method=method,description=err)
                raise RuntimeError(err)
            result=out['result']
            audit('telegram_ok',method=method,message_id=result.get('message_id') if isinstance(result,dict) else data.get('message_id'),text=result.get('text') if isinstance(result,dict) and method in ('sendMessage','editMessageText') else None,reply_markup=result.get('reply_markup') if isinstance(result,dict) else None,reply_markup_requested=data.get('reply_markup'))
            return result
    def save(self):
        p=ROOT/'state.json';tmp=ROOT/'state.tmp';tmp.write_text(json.dumps(self.s,ensure_ascii=False));os.chmod(tmp,0o600);tmp.replace(p)
    def send(self,text,**kw):
        r=self.tg('sendMessage',{'chat_id':LEO,'text':text,'disable_web_page_preview':True,**kw})
        self.s['outgoing'].append(r['message_id']);self.save();return r['message_id']
    def edit(self,mid,text):
        return self.tg('editMessageText',{'chat_id':LEO,'message_id':int(mid),'text':text[:4096],'disable_web_page_preview':True,'reply_markup':{'inline_keyboard':[]}})
    def panel(self,cards):
        lines=['🐠 Aquário · esperando o Leo',f'Leitura: {now()}',f'{len(cards)} pendência(s) · /status para atualizar']
        for tenant,label in BRANDS.items():
            rows=[t for t in cards if (t.get('tenant') if t.get('tenant') in BRANDS else 'transversal')==tenant]
            if not rows:continue
            lines+=['',label]
            for t in rows:
                age=max(0,(int(time.time())-t['since'])//86400)
                lines.append(f"• {t['title'][:77]} · {t.get('assignee') or 'sem agente'} · {age}d")
        if not cards:lines+=['','Nada esperando você agora.']
        lines+=['','Aprovar libera a agente; não conclui nem publica.','Responda à mensagem de uma pendência para comentar no card.']
        text='\n'.join(lines)[:4096]
        mid=self.s.get('panel')
        if mid:
            try:self.edit(mid,text)
            except RuntimeError as e:
                if 'message to edit not found' not in str(e):raise
                mid=None
        if not mid:
            mid=self.send(text,disable_notification=True);self.s['panel']=mid;self.save()
        if not self.s.get('pinned'):
            self.tg('pinChatMessage',{'chat_id':LEO,'message_id':mid,'disable_notification':True});self.s['pinned']=True;self.save()
        audit('panel_updated',message_id=mid,count=len(cards),read_at=now())
        return mid
    def sync(self,announce=True):
        cards=pending();refs={t['ref'] for t in cards};sent=0
        for ref,entry in list(self.s['sent'].items()):
            if ref not in refs and not entry.get('closed'):
                self.edit(entry['message_id'],entry['text']+'\n\n↪️ Esta pendência saiu da fila do Leo.');entry['closed']=True;self.save()
        for t in cards:
            if t['ref'] in self.s['sent']:continue
            if not announce:continue
            text=card_text(t);mid=self.send(text,reply_markup=keyboard(t))
            entry={'board':t['board'],'id':t['id'],'ref':t['ref'],'message_id':mid,'text':text,'closed':False}
            self.s['messages'][str(mid)]={**entry,'mode':'reply'}
            self.s['sent'][t['ref']]=entry;self.save();sent+=1
        panel=self.panel(cards)
        return {'ok':True,'pending':len(cards),'new_sent':sent,'panel_id':panel}
    def answer(self,q,text):
        try:self.tg('answerCallbackQuery',{'callback_query_id':q['id'],'text':text[:180]})
        except RuntimeError as e:
            # Synthetic callbacks in QA have no Telegram query id; real callbacks use the same API.
            if 'query is too old' not in str(e) and 'query ID is invalid' not in str(e):raise
    def mark(self,entry,result):
        self.edit(entry['message_id'],entry['text']+'\n\n'+result)
        if entry['ref'] in self.s['sent']:self.s['sent'][entry['ref']]['closed']=True
        self.save()
    def comment(self,entry,text,uid):
        marker=f'[Telegram update {uid}]'
        c=connect(entry['board'])
        existing=c.execute('SELECT id FROM task_comments WHERE task_id=? AND author=? AND instr(body,?)>0',(entry['id'],'Leo',marker)).fetchone()
        cid=existing[0] if existing else kb.add_comment(c,entry['id'],'Leo',text+'\n'+marker)
        c.close();audit('card_comment',board=entry['board'],card=entry['id'],comment_id=cid,body=text,duplicate=bool(existing));return cid
    def decide(self,entry,action,text,uid):
        if entry['ref'] in self.s['decisions']:
            return {'ok':True,'duplicate_decision':True,'card':entry['id']}
        t=get_card(entry['board'],entry['id'])
        if not t or not eligible(t) or t['ref']!=entry['ref']:
            self.mark(entry,'↪️ Pendência já mudou no Kanban; nenhuma decisão reaplicada.')
            return {'ok':True,'stale':True}
        cid=self.comment(entry,text,uid)
        c=connect(entry['board'])
        changed=kb.unblock_task(c,entry['id']) if t['status']=='blocked' else kb.specify_triage_task(c,entry['id'],author='Leo')
        status=c.execute('SELECT status FROM tasks WHERE id=?',(entry['id'],)).fetchone()[0];c.close()
        if not changed:raise RuntimeError('Card changed during decision; comment preserved, refresh /status')
        self.s['decisions'][entry['ref']]={'action':action,'update':uid,'at':now(),'comment_id':cid};self.save()
        result='✅ Leo aprovou · agente liberada (sem concluir/publicar).' if action=='approve' else '✏️ Ajuste enviado · agente liberada para refazer.'
        self.mark(entry,result);audit('decision_applied',action=action,board=entry['board'],card=entry['id'],comment_id=cid,status=status)
        self.sync(announce=False)
        return {'ok':True,'action':action,'card':entry['id'],'status':status,'comment_id':cid}
    def update(self,u):
        cb=u.get('callback_query');msg=u.get('message')
        origin=cb.get('from',{}) if cb else (msg or {}).get('from',{})
        chat=(cb.get('message',{}) if cb else (msg or {})).get('chat',{})
        if origin.get('id')!=LEO or chat.get('id')!=LEO or chat.get('type')!='private':
            audit('ignored_sender',update_id=u.get('update_id'));return {'ok':True,'ignored':True}
        uid=u.get('update_id')
        if not isinstance(uid,int):return {'ok':True,'ignored':True}
        if str(uid) in self.s['updates']:
            if cb:self.answer(cb,'Já registrado.')
            audit('duplicate_update',update_id=uid);return {'ok':True,'duplicate':True}
        if cb:
            mid=str(cb['message'].get('message_id'));entry=self.s['messages'].get(mid)
            match=re.fullmatch(r'([aec]):([dm]):(t_[a-zA-Z0-9]+)',cb.get('data',''))
            if not entry or not match or entry['board']!=match[2] or entry['id']!=match[3]:
                self.answer(cb,'Este botão não corresponde a uma pendência ativa.');out={'ok':True,'ignored':True}
            elif match[1]=='a':
                self.answer(cb,'Registrando a aprovação…');out=self.decide(entry,'approve','Leo aprovou via Telegram',uid)
            elif entry['ref'] in self.s['decisions']:
                self.answer(cb,'Essa decisão já foi registrada.');out={'ok':True,'duplicate_decision':True}
            else:
                self.answer(cb,'Responda à mensagem abaixo.')
                mode='adjust' if match[1]=='e' else 'comment'
                prompt=('✏️ Que ajuste você quer?' if mode=='adjust' else '💬 Qual comentário você quer deixar?')+f"\n{entry['id']} · Responda a esta mensagem."
                p=self.send(prompt,reply_markup={'force_reply':True,'input_field_placeholder':'Escreva aqui…','selective':True})
                self.s['messages'][str(p)]={**entry,'mode':mode,'prompt_id':p};self.save()
                out={'ok':True,'prompt_id':p,'mode':mode,'card':entry['id']}
        else:
            text=(msg.get('text') or msg.get('caption') or '').strip()
            cmd=text.split()[0].split('@')[0] if text else ''
            from provider_refresh import provider, forward
            if provider(u):
                try:out=forward(u,self.token)
                except Exception:out={'ok':False,'result':'manu: falhou:bridge unavailable\nmafe: falhou:bridge unavailable\nluna: falhou:bridge unavailable\naquario: falhou:bridge unavailable\ntanque: falhou:bridge unavailable'}
                from provider_refresh import reply_text
                text=reply_text(out,test=u.get('provider_ops_test') is True)
                edit=u.get('provider_ops_test_edit_message_id')
                if u.get('provider_ops_test') is True and isinstance(edit,int):self.tg('editMessageText',{'chat_id':LEO,'message_id':edit,'text':text})
                else:self.send(text)
            elif cmd in ('/status','/start'):out=self.sync()
            elif msg.get('reply_to_message'):
                reply=msg['reply_to_message'];entry=self.s['messages'].get(str(reply.get('message_id')))
                if not entry or reply.get('from',{}).get('id')!=BOT:out={'ok':True,'ignored':True}
                elif not text:out={'ok':True,'ignored':True,'reason':'text_required'}
                elif entry.get('mode')=='adjust':out=self.decide(entry,'adjust','Leo pediu ajuste via Telegram: '+text,uid)
                else:
                    cid=self.comment(entry,'Leo comentou via Telegram: '+text,uid)
                    self.mark(entry,'💬 Comentário do Leo registrado no card.')
                    self.sync(announce=False);out={'ok':True,'action':'comment','card':entry['id'],'comment_id':cid}
            else:out={'ok':True,'ignored':True}
        self.s['updates'][str(uid)]={'at':now(),'result':out};self.save()
        return out

def main(q):
    ROOT.mkdir(parents=True,exist_ok=True)
    os.chmod(ROOT,0o700)
    with (ROOT/'state.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        p=ROOT/'state.json'
        state=json.loads(p.read_text()) if p.exists() else {'updates':{},'sent':{},'messages':{},'decisions':{},'outgoing':[]}
        app=Dispatcher(q.pop('_token'),state)
        if q.get('op')=='sync':return app.sync()
        if q.get('op')=='update':return app.update(q.get('update',{}))
        raise ValueError('Unsupported operation')
if __name__=='__main__':
    try:print(json.dumps(main(json.load(sys.stdin)),ensure_ascii=False))
    except Exception as e:
        # Exception messages are code-controlled or Telegram descriptions, never URLs.
        print(json.dumps({'ok':False,'error':type(e).__name__+': '+str(e)}));sys.exit(1)
