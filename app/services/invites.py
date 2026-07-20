from datetime import datetime, timedelta
import json
from sqlalchemy import select
from aiogram import Bot
from aiogram.types import ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, Message
from app.db.session import SessionLocal
from app.db.models import User, InviteLink
from app.services.users import upsert_user
from app.config import get_settings
from app.services.moderation import text_has_word
from app.services.state import log_error, track
from app.services import settings as st

JOIN_CACHE: dict[int, tuple[int|None, datetime]] = {}

DEFAULT_TIERS=[
    {"count":1,"label":"1 vidéo","link":""},
    {"count":10,"label":"20 vidéos","link":""},
    {"count":50,"label":"100 vidéos","link":""},
    {"count":100,"label":"200 vidéos","link":""},
    {"count":300,"label":"500 vidéos","link":""},
    {"count":500,"label":"1 500 vidéos","link":""},
    {"count":1000,"label":"VIP gratuit à vie","link":""},
]

async def tiers():
    raw=await st.get_value('invite_tiers_json','')
    if not raw:
        return DEFAULT_TIERS
    try:
        data=json.loads(raw)
        return data if isinstance(data,list) else DEFAULT_TIERS
    except Exception:
        return DEFAULT_TIERS

async def set_tiers_from_text(text:str):
    # Format: 1|Label|https://gofile... une ligne par palier
    rows=[]
    for line in (text or '').splitlines():
        parts=[p.strip() for p in line.split('|')]
        if len(parts)>=3 and parts[0].isdigit():
            rows.append({'count':int(parts[0]),'label':parts[1],'link':parts[2]})
    if not rows:
        return False
    rows=sorted(rows,key=lambda x:x['count'])
    await st.set_value('invite_tiers_json', json.dumps(rows, ensure_ascii=False))
    return True

async def invite_text():
    return await st.get_value('invite_text','🎁 Programme de récompenses\n\nInvite des membres et débloque tes récompenses.')

async def invite_kb():
    username=get_settings().public_bot_username.strip().lstrip('@')
    url=f'https://t.me/{username}?start=invite' if username else None
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎁 Recevoir vidéos', url=url)]])
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎁 Recevoir vidéos', callback_data='invite_private')]])

async def send_invite_ad(bot:Bot, force:bool=False):
    if not force and not await st.is_open(): return None
    text=await invite_text(); img=await st.get_value('invite_image_file_id','')
    kb=await invite_kb(); s=get_settings()
    if img:
        m=await bot.send_photo(s.main_group_id,img,caption=text,reply_markup=kb)
        await track(s.main_group_id,m.message_id,None,'invite_ad',True)
    else:
        m=await bot.send_message(s.main_group_id,text,reply_markup=kb)
        await track(s.main_group_id,m.message_id,None,'invite_ad',False)
    await st.set_value('last_invite_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    await st.set_value('last_invite_message_id', str(m.message_id))
    return m.message_id

async def get_or_create_link(bot:Bot, owner_id:int):
    async with SessionLocal() as db:
        res=await db.execute(select(InviteLink).where(InviteLink.owner_id==owner_id,InviteLink.active==True).order_by(InviteLink.id.desc()).limit(1))
        row=res.scalar_one_or_none()
        if row and row.link:
            return row.link
    link_obj=await bot.create_chat_invite_link(get_settings().main_group_id, name=f'invite_{owner_id}', creates_join_request=False)
    link=link_obj.invite_link
    async with SessionLocal() as db:
        db.add(InviteLink(owner_id=owner_id,link=link,active=True))
        await db.commit()
    return link

async def send_invite_private(bot:Bot, user_id:int):
    link=await get_or_create_link(bot,user_id)
    t=await tiers()
    lines=['🎁 Ton lien unique','',link,'','Chaque invité validé augmente ton compteur.','', 'Paliers :']
    for r in t:
        lines.append(f"- {r['count']} invité(s) → {r['label']}")
    await bot.send_message(user_id,'\n'.join(lines))

async def on_join(event:ChatMemberUpdated, bot:Bot|None=None):
    if not event.new_chat_member or event.new_chat_member.status not in ('member','restricted'): return
    u=await upsert_user(event.from_user)
    name=((event.from_user.username or '')+' '+(event.from_user.full_name or '')).strip()
    if bot and await text_has_word('nameban', name):
        try:
            await bot.ban_chat_member(event.chat.id, event.from_user.id)
            u.is_banned=True
        except Exception as e: await log_error('nameban_join', e)
        return
    owner=None
    inv=getattr(event,'invite_link',None)
    link=getattr(inv,'invite_link',None) if inv else None
    if link:
        async with SessionLocal() as db:
            res=await db.execute(select(InviteLink).where(InviteLink.link==link,InviteLink.active==True).limit(1))
            row=res.scalar_one_or_none()
            if row: owner=row.owner_id
    if owner and owner==event.from_user.id:
        owner=None
    JOIN_CACHE[event.from_user.id]=(owner, datetime.utcnow())

async def _maybe_reward(bot:Bot, owner:int):
    async with SessionLocal() as db:
        user=await db.get(User,owner)
        if not user: return
        rc=user.reward_counter
    available=[r for r in await tiers() if rc>=int(r.get('count',0))]
    if not available: return
    reward=max(available,key=lambda r:int(r.get('count',0)))
    async with SessionLocal() as db:
        user=await db.get(User,owner)
        if user:
            user.reward_counter=0
            await db.commit()
    label=reward.get('label','Récompense')
    link=reward.get('link','')
    msg=f'🎁 PALIER ATTEINT\n\nRécompense débloquée :\n{label}\n\nTon compteur récompense repart à 0.'
    if link: msg += f'\n\nLien :\n{link}'
    try: await bot.send_message(owner,msg)
    except Exception: pass

async def validate_invites(bot:Bot):
    now=datetime.utcnow()
    for uid,(owner,t) in list(JOIN_CACHE.items()):
        if now-t >= timedelta(minutes=5):
            if owner:
                async with SessionLocal() as db:
                    user=await db.get(User,owner)
                    if user:
                        user.total_invites+=1; user.reward_counter+=1; user.weekly_invites+=1
                        rc=user.reward_counter; total=user.total_invites
                        await db.commit()
                        try: await bot.send_message(owner,f'✅ +1 invité validé\n\nProgression récompense : {rc}\nTotal invités : {total}')
                        except Exception: pass
                await _maybe_reward(bot,owner)
            JOIN_CACHE.pop(uid,None)

async def top_text():
    async with SessionLocal() as db:
        res=await db.execute(select(User).where(User.weekly_invites>=100).order_by(User.weekly_invites.desc()).limit(10))
        users=res.scalars().all()
    if not users: return '🏆 TOP INVITEURS\n\nAucune statistique pour le moment.'
    lines=['🏆 TOP INVITEURS — J-7','']
    for i,u in enumerate(users,1):
        name=('@'+u.username[:2]+'****') if u.username else (u.full_name[:2]+'****')
        lines.append(f'{i}. {name} — {u.weekly_invites} invités')
    lines.append('\nLe TOP 3 débloque tous les avantages.\nFin du classement dans : 7 jours')
    return '\n'.join(lines)

async def invite_health_text():
    return f"🎁 Invitations\n\nDernière publication : {await st.get_value('last_invite_sent_at','jamais')}\nImage configurée : {'oui' if await st.get_value('invite_image_file_id','') else 'non'}\nPaliers : {len(await tiers())}"

async def tiers_text():
    lines=['🎁 Paliers actuels', '', 'Format édition : 1|Label|Lien GoFile']
    for r in await tiers(): lines.append(f"{r['count']}|{r['label']}|{r.get('link','')}")
    return '\n'.join(lines)
