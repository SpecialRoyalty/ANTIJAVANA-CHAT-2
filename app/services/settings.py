from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import Setting
from app.config import get_settings

async def get_value(key:str, default:str=''):
    async with SessionLocal() as db:
        obj=await db.get(Setting,key)
        return obj.value if obj else default
async def set_value(key:str, value:str):
    async with SessionLocal() as db:
        obj=await db.get(Setting,key)
        if not obj: obj=Setting(key=key,value=value); db.add(obj)
        else: obj.value=value
        await db.commit()
async def init_defaults():
    s=get_settings()
    defaults={'auto_enabled':str(s.auto_schedule_enabled).lower(),'time_slot':s.default_time_slot,'vote_goal':str(s.default_vote_goal),'group_open':'false','status_message_id':'','active_session_id':'0','rules_text':'Respectez les règles. Pas de liens, pas de mentions, pas de commandes.','vip_text':'💎 ACCÈS VIP\n\nChoisissez une offre pour obtenir plus d’informations.','crowd_text':'🎯 FINANCEMENT COMMUNAUTAIRE','ads_text':'📢 Publicité','weekly_top_started':'false','weekly_top_start':'','manual_security_warned_at':'','manual_opened_at':'','free_pass_enabled':'false','free_pass_places':'20','free_pass_cooldown_days':'30','free_pass_min_media':'3','free_pass_min_invites':'0','free_pass_message_id':'','justice_limit':'20','hashban_reposts_detected':'0','hashban_reposts_blocked':'0','hashban_reposts_failed':'0','hashban_detected_file_unique_id':'0','hashban_detected_sha256':'0'}
    for k,v in defaults.items():
        if await get_value(k,'')=='': await set_value(k,v)
async def is_open(): return (await get_value('group_open','false'))=='true'
async def set_open(v:bool): await set_value('group_open','true' if v else 'false')
async def auto_enabled(): return (await get_value('auto_enabled','true'))=='true'
async def time_slot(): return await get_value('time_slot', get_settings().default_time_slot)
async def vote_goal(): return int(await get_value('vote_goal', str(get_settings().default_vote_goal)))

async def justice_limit():
    raw = await get_value('justice_limit','20')
    try:
        n = int(raw)
    except Exception:
        n = 20
    return max(1, min(n, 200))
