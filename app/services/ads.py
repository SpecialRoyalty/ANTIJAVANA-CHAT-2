import random
from sqlalchemy import select
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import Advertisement
from app.services import settings as st
from app.services.state import track

async def add_ad(text:str='', image_file_id:str|None=None):
    async with SessionLocal() as db:
        from sqlalchemy import func
        count=int((await db.execute(select(func.count(Advertisement.id)))).scalar() or 0)
        if count>=2:
            return -1
        ad=Advertisement(title=f'Pub', text=text, image_file_id=image_file_id, active=True)
        db.add(ad); await db.commit(); return ad.id

async def list_ads_text():
    async with SessionLocal() as db:
        res=await db.execute(select(Advertisement).order_by(Advertisement.id.desc()).limit(20))
        ads=list(res.scalars().all())
    if not ads: return '📢 Aucune publicité configurée.'
    return '📢 Publicités configurées\n\nClique sur une pub pour la gérer.'

async def ads_list_kb():
    async with SessionLocal() as db:
        res=await db.execute(select(Advertisement).order_by(Advertisement.id.desc()).limit(20))
        ads=list(res.scalars().all())
    rows=[]
    for a in ads:
        label=f'#{a.id} {"🟢" if a.active else "🔴"} '+((a.text or '[image seule]')[:28])
        rows.append([InlineKeyboardButton(text=label, callback_data=f'ad_manage:{a.id}')])
    rows.append([InlineKeyboardButton(text='⬅️ Retour pubs', callback_data='adm_ads')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def ad_detail(ad_id:int):
    async with SessionLocal() as db:
        ad=await db.get(Advertisement, ad_id)
    if not ad: return 'Pub introuvable.', None
    text=(ad.text or '[sans texte]')
    msg=f'📢 Pub #{ad.id}\n\nStatut : {"active" if ad.active else "off"}\nImage : {"oui" if ad.image_file_id else "non"}\n\nTexte :\n{text}'
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📤 Publier maintenant', callback_data=f'ad_send_one:{ad.id}')],
        [InlineKeyboardButton(text='📝 Modifier texte', callback_data=f'await:ad_edit_text:{ad.id}'), InlineKeyboardButton(text='🖼 Modifier image', callback_data=f'await:ad_edit_image:{ad.id}')],
        [InlineKeyboardButton(text='🟢/🔴 Activer/Désactiver', callback_data=f'ad_toggle:{ad.id}')],
        [InlineKeyboardButton(text='🗑 Supprimer cette pub', callback_data=f'ad_delete:{ad.id}')],
        [InlineKeyboardButton(text='📋 Retour liste pubs', callback_data='ad_list')],
    ])
    return msg,kb

async def toggle_ad(ad_id:int):
    async with SessionLocal() as db:
        ad=await db.get(Advertisement, ad_id)
        if not ad: return False
        ad.active=not ad.active
        await db.commit()
        return True

async def delete_ad(ad_id:int):
    async with SessionLocal() as db:
        ad=await db.get(Advertisement, ad_id)
        if not ad: return False
        await db.delete(ad)
        await db.commit()
        return True

async def set_ad_text(ad_id:int, text:str):
    async with SessionLocal() as db:
        ad=await db.get(Advertisement, ad_id)
        if not ad: return False
        ad.text=text
        await db.commit(); return True

async def set_ad_image(ad_id:int, image_file_id:str):
    async with SessionLocal() as db:
        ad=await db.get(Advertisement, ad_id)
        if not ad: return False
        ad.image_file_id=image_file_id
        await db.commit(); return True

async def send_ad_by_id(bot:Bot, ad_id:int, force:bool=True):
    if not force and not await st.is_open(): return None
    async with SessionLocal() as db:
        ad=await db.get(Advertisement, ad_id)
    if not ad: return None
    s=get_settings(); kb=None
    if ad.button_text and ad.button_url:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ad.button_text,url=ad.button_url)]])
    if ad.image_file_id:
        m=await bot.send_photo(s.main_group_id,ad.image_file_id,caption=ad.text or None,reply_markup=kb)
    else:
        m=await bot.send_message(s.main_group_id,ad.text or '📢 Publicité',reply_markup=kb)
    await track(s.main_group_id,m.message_id,None,'ad',bool(ad.image_file_id))
    from datetime import datetime
    await st.set_value('last_ad_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    await st.set_value('last_ad_message_id', str(m.message_id))
    await st.set_value('last_ad_id', str(ad.id))
    return m.message_id

async def send_random_ad(bot:Bot, force:bool=False):
    if not force and not await st.is_open(): return None
    async with SessionLocal() as db:
        res=await db.execute(select(Advertisement).where(Advertisement.active==True))
        ads=list(res.scalars().all())
    if not ads: return None
    ad=random.choice(ads); s=get_settings()
    kb=None
    if ad.button_text and ad.button_url:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ad.button_text,url=ad.button_url)]])
    if ad.image_file_id:
        m=await bot.send_photo(s.main_group_id,ad.image_file_id,caption=ad.text or None,reply_markup=kb)
    else:
        m=await bot.send_message(s.main_group_id,ad.text or '📢 Publicité',reply_markup=kb)
    await track(s.main_group_id,m.message_id,None,'ad',bool(ad.image_file_id))
    from datetime import datetime
    await st.set_value('last_ad_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    await st.set_value('last_ad_message_id', str(m.message_id))
    return m.message_id

async def ads_health_text():
    last=await st.get_value('last_ad_sent_at','jamais')
    mid=await st.get_value('last_ad_message_id','-')
    state='ouvert' if await st.is_open() else 'fermé'
    return f'📢 Publicités\n\nGroupe : {state}\nDernier envoi : {last}\nDernier message ID : {mid}\nProchain envoi automatique : pendant ouverture selon planning.\nVérification fermeture : le rapport de nettoyage confirme si la pub suivie a été supprimée.'
