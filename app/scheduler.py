from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from app.config import get_settings
from app.services import settings as st
from app.services.state import ensure_status_message, vote_count
from app.services.session_ops import set_group_open, security_close_if_manual
from app.services.vip import send_vip_ad, expire_pass_soiree, send_due_pass_soiree_links
from app.services.crowdfunding import send_crowd_ad
from app.services.ads import send_random_ad
from app.services.invites import validate_invites, top_text, send_invite_ad
from app.services.freepass import send_due_free_pass_links
from app.utils.time import in_slot, mid_time, now_tz

async def tick(bot:Bot):
    s=get_settings(); chat=s.main_group_id
    await ensure_status_message(bot,chat,recreate_on_change=True)
    if not await st.auto_enabled():
        return
    ins=in_slot(await st.time_slot(),s.timezone)
    open_=await st.is_open()
    goal=await st.vote_goal(); votes=await vote_count(chat)
    if ins and not open_ and votes>=goal:
        await set_group_open(bot,True,'auto')
    if not ins and open_:
        await set_group_open(bot,False,'auto')
async def run_justice_now(bot:Bot):
    if not await st.is_open(): return
    from app.services.justice import execute_justice
    await execute_justice(bot, manual=False)

async def justice_tick(bot:Bot):
    if not await st.is_open(): return
    s=get_settings(); mt=mid_time(await st.time_slot(),s.timezone); n=now_tz(s.timezone)
    done=await st.get_value('justice_done_'+n.strftime('%Y%m%d'),'false')
    if done=='true': return
    if abs((n-mt).total_seconds())<70:
        await st.set_value('justice_done_'+n.strftime('%Y%m%d'),'true')
        await run_justice_now(bot)
async def rules_tick(bot:Bot, force:bool=False):
    if not force and not await st.is_open(): return
    s=get_settings(); old=await st.get_value('rules_message_id','')
    try:
        if old: await bot.delete_message(s.main_group_id,int(old))
    except Exception: pass
    m=await bot.send_message(s.main_group_id, await st.get_value('rules_text','Règles'))
    await st.set_value('rules_message_id',str(m.message_id))
    from datetime import datetime
    await st.set_value('last_rules_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
async def top_tick(bot:Bot):
    if not await st.is_open(): return
    s=get_settings(); txt=await top_text()
    if 'Aucune statistique' in txt: return
    await bot.send_message(s.main_group_id, txt)
    from datetime import datetime
    await st.set_value('last_top_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
def start_scheduler(bot:Bot):
    sch=AsyncIOScheduler(timezone=get_settings().timezone)
    sch.add_job(tick,'interval',minutes=1,args=[bot], id='tick')
    sch.add_job(justice_tick,'interval',minutes=1,args=[bot], id='justice')
    sch.add_job(validate_invites,'interval',minutes=1,args=[bot], id='invite_validate')
    sch.add_job(rules_tick,'interval',minutes=30,args=[bot], id='rules')
    sch.add_job(send_vip_ad,'cron',hour='22,0',minute='50,10',args=[bot], id='vip_ads')
    sch.add_job(send_crowd_ad,'cron',hour='22,0',minute='55,15',args=[bot], id='crowd_ads')
    sch.add_job(send_random_ad,'cron',hour='22,0',minute='45,5',args=[bot], id='random_ads')
    sch.add_job(top_tick,'cron',hour='0',minute='40',args=[bot], id='top')
    sch.add_job(send_invite_ad,'cron',hour='23',minute='25',args=[bot], id='invite_ad')
    sch.add_job(security_close_if_manual,'interval',minutes=5,args=[bot], id='security_close')
    sch.add_job(send_due_pass_soiree_links,'cron',hour='23',minute='0',args=[bot], id='pass_soiree_release')
    sch.add_job(send_due_free_pass_links,'cron',hour='23',minute='0',args=[bot], id='free_pass_release')
    sch.add_job(expire_pass_soiree,'cron',hour='5',minute='0',args=[bot], id='expire_pass')
    sch.start(); return sch
