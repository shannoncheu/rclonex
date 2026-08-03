import os, asyncio, httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
load_dotenv()
API=os.getenv('API_INTERNAL_URL','http://api:8000/api')
def check(update):
 import app.main as core
 allowed={int(x) for x in core.settings().get('telegram_allowed_ids',os.getenv('TELEGRAM_ALLOWED_IDS','')).split(',') if x.strip()}
 return update.effective_user and update.effective_user.id in allowed
async def submit(update,ctx,remote):
 if not check(update): return
 try:
  directory,url=ctx.args[0],ctx.args[1]; payload={'url':url,'remote':remote,'target_dir':directory}
  # Internal token is not exposed externally; bot creates task through the same queue API endpoint bypass-free.
  import app.main as core
  from types import SimpleNamespace
  task=core.create(core.Create(**payload),user=f'tg:{update.effective_user.id}')
  await update.message.reply_text(f"已收到任务 #{task['id']}，识别类型：{task['link_type']}。")
  asyncio.create_task(notify_lifecycle(update.effective_chat.id, task['id'], ctx.bot))
 except (IndexError,Exception) as e: await update.message.reply_text('用法：/'+remote+' <目标目录> <链接>\n错误：'+str(e))
async def gd(u,c): await submit(u,c,'gd')
async def dropbox(u,c): await submit(u,c,'dropbox')
async def status(u,c):
 if check(u):
  import app.main as core
  ts=core.tasks(user='bot')[:10]; await u.message.reply_text('\n'.join(f"#{x['id']} {x['status']} {x['target_dir']}" for x in ts) or '暂无任务')
async def cancel(u,c):
 if check(u) and c.args:
  import app.main as core; core.update(c.args[0],status='cancelled'); await u.message.reply_text('已请求取消')
async def retry(u,c):
 if check(u) and c.args:
  import app.main as core; core.update(c.args[0],status='queued',error=''); await u.message.reply_text('已加入队列')
async def help_(u,c):
 if check(u): await u.message.reply_text('/gd <目录> <链接>\n/dropbox <目录> <链接>\n/status\n/cancel <ID>\n/retry <ID>')
async def notify_lifecycle(chat_id,tid,bot):
 import app.main as core
 last=''
 labels={'preparing':'准备开始','downloading':'开始下载','uploading':'开始上传','success':'任务成功','failed':'任务失败','cancelled':'任务已取消'}
 for _ in range(720):
  t=core.gettask(tid); state=t['status'] if t else 'cancelled'
  if state!=last and state in labels:
   text=f"#{tid} {labels[state]}" + (f"：{t['error']}" if state=='failed' and t['error'] else '')
   await bot.send_message(chat_id,text); last=state
  if state in ('success','failed','cancelled'): return
  await asyncio.sleep(5)
def main():
 import app.main as core; core.init(recover=False)
 app=Application.builder().token(os.environ['TELEGRAM_BOT_TOKEN']).build()
 for name,fn in [('gd',gd),('dropbox',dropbox),('status',status),('cancel',cancel),('retry',retry),('help',help_)]: app.add_handler(CommandHandler(name,fn))
 app.run_polling()
if __name__=='__main__': main()
