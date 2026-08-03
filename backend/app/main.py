import asyncio, json, os, re, shutil, sqlite3, subprocess, time, uuid, hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer, BadSignature

load_dotenv()
DATA=Path(os.getenv('DATA_DIR','/data')); TMP=Path(os.getenv('TEMP_DIR','/downloads')); LOG=Path(os.getenv('LOG_DIR','/logs'))
for p in (DATA,TMP,LOG): p.mkdir(parents=True,exist_ok=True)
DB=DATA/'app.db'; pwd=CryptContext(schemes=['bcrypt'], deprecated='auto'); signer=URLSafeTimedSerializer(os.getenv('SESSION_SECRET','change-me'))
REMOTE={'gd':'gd','dropbox':'dropbox'}; security=HTTPBasic(); clients:set[WebSocket]=set(); runner_task=None

def now(): return datetime.now(timezone.utc).isoformat()
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init(recover=True):
 c=conn(); c.executescript('''CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, user TEXT, url TEXT, link_type TEXT, remote TEXT, target_dir TEXT, status TEXT, progress REAL DEFAULT 0, speed TEXT DEFAULT '', size TEXT DEFAULT '', temp_path TEXT DEFAULT '', created_at TEXT, completed_at TEXT, error TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);''')
 defaults={'default_remote':os.getenv('DEFAULT_REMOTE','gd'),'default_dir':os.getenv('DEFAULT_DIR',''),'max_concurrency':os.getenv('MAX_CONCURRENCY','2'),'download_limit':os.getenv('DOWNLOAD_LIMIT','0'),'upload_limit':os.getenv('UPLOAD_LIMIT','0'),'telegram_allowed_ids':os.getenv('TELEGRAM_ALLOWED_IDS','')}
 for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,v))
 if recover: c.execute("UPDATE tasks SET status='retryable', error='服务重启后任务可重试' WHERE status IN ('preparing','queued','downloading','uploading')")
 c.commit(); c.close()
def settings():
 c=conn(); d={x['key']:x['value'] for x in c.execute('SELECT * FROM settings')}; c.close(); return d
def taskrow(row): return dict(row) if row else None
def gettask(tid):
 c=conn(); x=taskrow(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()); c.close(); return x
def update(tid, **kw):
 c=conn(); c.execute('UPDATE tasks SET '+','.join(f'{k}=?' for k in kw)+' WHERE id=?',(*kw.values(),tid)); c.commit(); c.close()
def safe_dir(value:str)->str:
 value=value.strip().strip('/')
 if len(value)>400 or '..' in value.split('/') or '\\' in value or any(ord(x)<32 for x in value) or re.search(r'[:*?"<>|]',value): raise HTTPException(400,'目标目录不合法')
 return value
def root(remote,directory): return f'{REMOTE[remote]}:inbox' + (f'/{directory}' if directory else '')
def kind(url):
 if url.startswith('magnet:'): return 'magnet'
 if url.lower().endswith('.torrent'): return 'torrent'
 if re.match(r'https?://',url):
  if re.search(r'(youtube\.com|youtu\.be|vimeo\.com|bilibili\.com)',url,re.I): return 'video'
  if re.search(r'(drive\.google\.com|dropbox\.com|pan\.baidu\.com|mega\.nz)',url,re.I): return 'unsupported'
  return 'http'
 return 'unsupported'
def auth(request:Request):
 token=request.cookies.get('session')
 try: return signer.loads(token,max_age=86400) if token else None
 except BadSignature: raise HTTPException(401,'登录已失效')
def csrf(request:Request, user=Depends(auth)):
 if request.headers.get('X-CSRF-Token') != request.cookies.get('csrf'): raise HTTPException(403,'CSRF 校验失败')
 return user
async def broadcast():
 data=json.dumps({'type':'tasks'})
 for ws in list(clients):
  try: await ws.send_text(data)
  except: clients.discard(ws)
def sync_broadcast():
 try: asyncio.get_running_loop().create_task(broadcast())
 except RuntimeError: pass
def run(cmd, tid, phase):
 update(tid,status=phase,speed='处理中'); sync_broadcast()
 p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,shell=False)
 lines=[]
 for line in iter(p.stdout.readline,''):
  lines.append(line[-500:]); update(tid,speed=phase); sync_broadcast()
  if gettask(tid)['status']=='cancelled': p.terminate(); return False,'已取消'
 rc=p.wait(); return rc==0, ''.join(lines[-10:])[-2000:]
def worker(tid):
 t=gettask(tid); target=root(t['remote'],t['target_dir']); url=t['url']; limit=settings().get('download_limit','0')
 try:
  if t['link_type']=='http':
   ok,msg=run(['rclone','copyurl',url,target,'--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf'),'--progress'],tid,'uploading')
  elif t['link_type'] in ('magnet','torrent'):
   dest=TMP/tid; dest.mkdir(exist_ok=True); update(tid,temp_path=str(dest))
   ok,msg=run(['aria2c','--seed-time=0','--dir',str(dest),'--max-overall-download-limit='+limit,url],tid,'downloading')
   if ok: ok,msg=run(['rclone','move',str(dest),target,'--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf'),'--progress'],tid,'uploading')
  else:
   dest=TMP/tid; dest.mkdir(exist_ok=True); update(tid,temp_path=str(dest))
   ok,msg=run(['yt-dlp','--no-playlist','-P',str(dest),url],tid,'downloading')
   if ok: ok,msg=run(['rclone','move',str(dest),target,'--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf'),'--progress'],tid,'uploading')
  update(tid,status='success' if ok else 'failed',progress=100 if ok else 0,speed='',completed_at=now(),error='' if ok else msg)
 except Exception as e: update(tid,status='failed',error=str(e),completed_at=now())
 sync_broadcast()
async def queue_loop():
 while True:
  active=[]; c=conn(); rows=c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at").fetchall(); c.close()
  # asyncio tasks are tracked by DB state; launch until configured capacity
  c=conn(); count=c.execute("SELECT count(*) FROM tasks WHERE status IN ('preparing','downloading','uploading')").fetchone()[0]; c.close()
  for r in rows[:max(0,int(settings().get('max_concurrency','2'))-count)]:
   update(r['id'],status='preparing'); asyncio.create_task(asyncio.to_thread(worker,r['id']))
  await asyncio.sleep(2)
@asynccontextmanager
async def lifespan(app): init(); q=asyncio.create_task(queue_loop()); yield; q.cancel()
app=FastAPI(title='rclonex',lifespan=lifespan); app.add_middleware(CORSMiddleware,allow_origins=['http://localhost'],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])
class Create(BaseModel): url:str; remote:Literal['gd','dropbox']; target_dir:str=''
class Patch(BaseModel): values:dict[str,str]
class FileAction(BaseModel): path:str
@app.post('/api/login')
def login(creds:HTTPBasicCredentials=Depends(security)):
 if not (hmac.compare_digest(creds.username,os.getenv('ADMIN_USERNAME','admin')) and hmac.compare_digest(creds.password,os.getenv('ADMIN_PASSWORD',''))): raise HTTPException(401,'账号或密码错误')
 import secrets
 csrf_token=secrets.token_urlsafe(24); res=Response(status_code=204); res.set_cookie('session',signer.dumps(creds.username),httponly=True,samesite='strict'); res.set_cookie('csrf',csrf_token,httponly=False,samesite='strict'); return res
@app.post('/api/logout')
def logout(user=Depends(csrf)): r=Response(status_code=204); r.delete_cookie('session'); r.delete_cookie('csrf'); return r
@app.get('/api/me')
def me(user=Depends(auth)): return {'user':user,'csrf_required':True}
@app.get('/api/tasks')
def tasks(user=Depends(auth)):
 c=conn(); rows=[taskrow(x) for x in c.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 300')]; c.close(); return rows
@app.post('/api/tasks')
def create(body:Create,user=Depends(csrf)):
 d=safe_dir(body.target_dir); k=kind(body.url)
 if k=='unsupported': raise HTTPException(400,'暂不支持该平台或链接类型')
 tid=uuid.uuid4().hex[:10]; c=conn(); c.execute('INSERT INTO tasks(id,user,url,link_type,remote,target_dir,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(tid,user,body.url,k,body.remote,d,'queued',now())); c.commit(); c.close(); sync_broadcast(); return gettask(tid)
@app.post('/api/tasks/{tid}/cancel')
def cancel(tid:str,user=Depends(csrf)): update(tid,status='cancelled',completed_at=now()); return gettask(tid)
@app.post('/api/tasks/{tid}/retry')
def retry(tid:str,user=Depends(csrf)):
 t=gettask(tid);
 if not t: raise HTTPException(404,'任务不存在')
 update(tid,status='queued',error='',progress=0,completed_at=''); return gettask(tid)
@app.delete('/api/tasks/{tid}')
def delete(tid:str,user=Depends(csrf)): c=conn(); c.execute('DELETE FROM tasks WHERE id=?',(tid,)); c.commit(); c.close(); return {'ok':True}
@app.get('/api/settings')
def getsettings(user=Depends(auth)): return settings()
@app.put('/api/settings')
def putsettings(body:Patch,user=Depends(csrf)):
 allowed={'default_remote','default_dir','max_concurrency','download_limit','upload_limit','telegram_allowed_ids'}; c=conn()
 for k,v in body.values.items():
  if k in allowed: c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,str(v)))
 c.commit(); c.close(); return settings()
@app.get('/api/dashboard')
def dashboard(user=Depends(auth)):
 c=conn(); counts={x['status']:x['n'] for x in c.execute('SELECT status,count(*) n FROM tasks GROUP BY status')}; failed=[taskrow(x) for x in c.execute("SELECT * FROM tasks WHERE status='failed' ORDER BY created_at DESC LIMIT 5")]; c.close(); usage=shutil.disk_usage(TMP); return {'counts':counts,'failed':failed,'disk':{'used':usage.used,'total':usage.total}}
@app.get('/api/files/{remote}')
def files(remote:str,path:str='',user=Depends(auth)):
 if remote not in REMOTE: raise HTTPException(404)
 path=safe_dir(path); p=subprocess.run(['rclone','lsjson',root(remote,path),'--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf')],capture_output=True,text=True,shell=False)
 if p.returncode: raise HTTPException(502,'读取云盘失败')
 return json.loads(p.stdout)
@app.get('/api/remotes')
def remotes(user=Depends(auth)):
 result=[]
 for key in REMOTE:
  p=subprocess.run(['rclone','lsd',f'{REMOTE[key]}:','--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf')],capture_output=True,text=True,shell=False)
  result.append({'id':key,'name':'Google Drive' if key=='gd' else 'Dropbox','connected':p.returncode==0,'root':f'{REMOTE[key]}:inbox/'})
 return result
@app.post('/api/files/{remote}/mkdir')
def mkdir(remote:str,body:FileAction,user=Depends(csrf)):
 if remote not in REMOTE: raise HTTPException(404)
 path=safe_dir(body.path); p=subprocess.run(['rclone','mkdir',root(remote,path),'--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf')],capture_output=True,text=True,shell=False)
 if p.returncode: raise HTTPException(502,'创建目录失败')
 return {'ok':True}
@app.delete('/api/files/{remote}')
def remove_file(remote:str,path:str,user=Depends(csrf)):
 if remote not in REMOTE: raise HTTPException(404)
 path=safe_dir(path)
 if not path: raise HTTPException(400,'不可删除 inbox 根目录')
 p=subprocess.run(['rclone','deletefile',root(remote,path),'--config',os.getenv('RCLONE_CONFIG','/config/rclone.conf')],capture_output=True,text=True,shell=False)
 if p.returncode: raise HTTPException(502,'删除失败（目录请先清空）')
 return {'ok':True}
@app.websocket('/api/ws')
async def ws(socket:WebSocket):
 await socket.accept(); clients.add(socket)
 try:
  while True: await socket.receive_text()
 except WebSocketDisconnect: clients.discard(socket)
