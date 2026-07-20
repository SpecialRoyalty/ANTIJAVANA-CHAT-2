from __future__ import annotations
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from aiogram import Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import VipOrder, TrackedMessage, VipAccess
from app.keyboards.common import admin_validate_kb
from app.services import settings as st
from app.services.session_ops import notify_admins
from app.services.state import track, log_error

OFFER_NAMES={'soiree':'🎟 Pass soirée','total':'📦 Pass total','javana':'💎 COPIE 1:1 VIP JAVANA -50%'}

async def offer_price(offer:str)->int:
    return int(await st.get_value(f'vip_price_{offer}', {'soiree':'10','total':'30','javana':'50'}.get(offer,'0')) or '0')

async def vip_group_kb():
    username=get_settings().public_bot_username.strip().lstrip('@')
    if username:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🎟 Pass soirée', url=f'https://t.me/{username}?start=vip_soiree')],
            [InlineKeyboardButton(text='📦 Pass total', url=f'https://t.me/{username}?start=vip_total')],
            [InlineKeyboardButton(text='💎 COPIE 1:1 VIP JAVANA -50%', url=f'https://t.me/{username}?start=vip_javana')],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🎟 Pass soirée', callback_data='vip_offer:soiree')],
        [InlineKeyboardButton(text='📦 Pass total', callback_data='vip_offer:total')],
        [InlineKeyboardButton(text='💎 COPIE 1:1 VIP JAVANA -50%', callback_data='vip_offer:javana')],
    ])

async def user_cart(user_id:int)->set[str]:
    raw=await st.get_value(f'vip_cart:{user_id}','')
    return {x for x in raw.split(',') if x}
async def set_cart(user_id:int, items:set[str]):
    await st.set_value(f'vip_cart:{user_id}', ','.join(sorted(items)))
async def toggle_cart(user_id:int, offer:str):
    items=await user_cart(user_id)
    if offer in items: items.remove(offer)
    else: items.add(offer)
    await set_cart(user_id, items)
    return items
async def cart_total(items:set[str])->int:
    return sum([await offer_price(x) for x in items])

async def vip_cart_block_reason(user_id:int, items:set[str]) -> str|None:
    # Cohérence commerciale : quelqu'un qui possède déjà un accès supérieur
    # ne peut plus acheter le Pass soirée seul. On bloque aussi les doublons
    # d'accès déjà actifs/en attente.
    if not items:
        return 'Choisis au moins une offre.'
    async with SessionLocal() as db:
        existing = await db.execute(
            select(VipAccess).where(
                VipAccess.user_id == user_id,
                VipAccess.status.in_(['pending','active'])
            )
        )
        accesses=list(existing.scalars().all())
    have={a.offer for a in accesses}
    if 'soiree' in items and not ({'total','javana'} & items) and ({'total','javana'} & have):
        return 'Tu disposes déjà d’un accès supérieur. Le Pass Soirée n’est plus nécessaire.'
    if 'total' in items and 'total' in have:
        return 'Tu disposes déjà du Pass Total.'
    if 'javana' in items and 'javana' in have:
        return 'Tu disposes déjà du VIP JAVANA.'
    if 'soiree' in items and 'soiree' in have and not ({'total','javana'} & items):
        return 'Tu disposes déjà d’un Pass Soirée pour la session.'
    return None


def vip_private_kb(items:set[str]):
    def mark(o): return '☑️' if o in items else '☐'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f'{mark("soiree")} 🎟 Pass soirée', callback_data='vip_toggle:soiree')],
        [InlineKeyboardButton(text=f'{mark("total")} 📦 Pass total', callback_data='vip_toggle:total')],
        [InlineKeyboardButton(text=f'{mark("javana")} 💎 COPIE 1:1 VIP JAVANA -50%', callback_data='vip_toggle:javana')],
        [InlineKeyboardButton(text='💳 Continuer au paiement', callback_data='vip_checkout')],
    ])

def payment_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='PayPal', callback_data='vip_pay:paypal'), InlineKeyboardButton(text='Revolut', callback_data='vip_pay:revolut'), InlineKeyboardButton(text='Crypto', callback_data='vip_pay:crypto')],
        [InlineKeyboardButton(text='⬅️ Modifier offres', callback_data='vip_menu')]
    ])

async def vip_menu_text(user_id:int)->str:
    items=await user_cart(user_id)
    lines=['💎 MENU VIP\n']
    for o in ['soiree','total','javana']:
        desc=await st.get_value(f'vip_offer_{o}_text', OFFER_NAMES[o])
        lines.append(f'{OFFER_NAMES[o]} — {await offer_price(o)}€')
        if desc and desc != OFFER_NAMES[o]: lines.append(desc[:180])
        lines.append('')
    lines.append('Sélectionne une ou plusieurs offres.')
    if items:
        lines.append('\nVotre sélection : ' + ', '.join(OFFER_NAMES[x] for x in sorted(items)))
        lines.append(f'Total : {await cart_total(items)}€')
    return '\n'.join(lines)

async def send_vip_private(bot:Bot, user_id:int, preselect:str|None=None):
    if preselect:
        items=await user_cart(user_id); items.add(preselect); await set_cart(user_id,items)
    items=await user_cart(user_id)
    await bot.send_message(user_id, await vip_menu_text(user_id), reply_markup=vip_private_kb(items))

async def send_vip_ad(bot:Bot, force:bool=False):
    s=get_settings()
    if not force and not await st.is_open(): return None
    text=await st.get_value('vip_text','💎 ACCÈS VIP\n\nChoisissez une offre.')
    image=await st.get_value('vip_image_file_id','')
    kb=await vip_group_kb()
    if image:
        m=await bot.send_photo(s.main_group_id,image,caption=text,reply_markup=kb)
    else:
        m=await bot.send_message(s.main_group_id,text,reply_markup=kb)
    await track(s.main_group_id,m.message_id,None,'vip_ad',bool(image))
    await st.set_value('last_vip_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    await st.set_value('last_vip_message_id', str(m.message_id))
    return m.message_id

async def create_order_from_cart(user_id:int, username:str):
    items=await user_cart(user_id)
    if not items: return None
    offers=','.join(sorted(items)); amount=str(await cart_total(items))
    async with SessionLocal() as db:
        order=VipOrder(user_id=user_id,username=username,offers=offers,amount=amount,status='selecting')
        db.add(order); await db.commit(); return order.id

async def payment_text_for_cart(user_id:int):
    s=get_settings(); items=await user_cart(user_id); total=await cart_total(items)
    selected='\n'.join(f'- {OFFER_NAMES.get(x,x)}' for x in sorted(items)) or 'Aucune'
    return f'💳 Paiement VIP\n\nSélection :\n{selected}\n\nTotal : {total}€\n\nPayPal : {s.paypal_text or "à configurer"}\nRevolut : {s.revolut_text or "à configurer"}\nCrypto : {s.crypto_text or "à configurer"}\n\nAprès paiement, envoie une capture ici.'

def _proof_file_id(msg:Message):
    if msg.photo: return msg.photo[-1].file_id
    if msg.document: return msg.document.file_id
    return None

async def handle_vip_proof(bot:Bot,msg:Message):
    if not msg.from_user: return False
    fid=_proof_file_id(msg)
    if not fid: return False
    async with SessionLocal() as db:
        res=await db.execute(select(VipOrder).where(VipOrder.user_id==msg.from_user.id,VipOrder.status.in_(['selecting','pending'])).order_by(VipOrder.id.desc()).limit(1))
        order=res.scalar_one_or_none()
        if not order: return False
        order.screenshot_file_id=fid; order.status='pending'; await db.commit()
        await msg.answer('✅ Capture reçue. Validation admin en attente.')
        await notify_admins(bot,f'💰 Nouvelle demande VIP\n\nUtilisateur : @{msg.from_user.username or msg.from_user.full_name}\nOffres : {order.offers}\nMontant : {order.amount}€', admin_validate_kb('vip',order.id))
        return True

def _group_for_offer(offer:str)->int|None:
    s=get_settings()
    return {'soiree':s.pass_soiree_group_id,'total':s.pass_total_group_id,'javana':s.vip_javana_group_id}.get(offer)

def _now_local():
    return datetime.now(ZoneInfo(get_settings().timezone))

def _today_at(hour:int, minute:int=0):
    n=_now_local()
    return datetime.combine(n.date(), time(hour,minute), tzinfo=n.tzinfo)

def _soiree_send_now()->bool:
    n=_now_local()
    # de 23:00 à 04:59:59, le lien est envoyé immédiatement.
    return n.hour >= 23 or n.hour < 5

def _soiree_next_release_utc()->datetime:
    n=_now_local()
    release=_today_at(23,0)
    if n >= release:
        release = release + timedelta(days=1)
    return release.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)

def _soiree_expire_utc_for_current_or_next()->datetime:
    n=_now_local()
    exp=_today_at(5,0)
    if n.hour >= 5:
        exp = exp + timedelta(days=1)
    return exp.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)

async def _create_one_time_link(bot:Bot, group_id:int, offer:str, expires_at:datetime|None=None):
    kwargs={'member_limit':1}
    if expires_at:
        # Telegram accepte un datetime ou timestamp selon aiogram ; on passe datetime UTC naive/aware compatible.
        kwargs['expire_date']=expires_at
    link=await bot.create_chat_invite_link(group_id, **kwargs)
    return link.invite_link

async def _send_access_link(bot:Bot, access_id:int):
    async with SessionLocal() as db:
        a=await db.get(VipAccess, access_id)
        if not a or a.status not in ['pending','failed']: return False
        if not a.group_id:
            a.status='failed'; await db.commit(); await log_error('vip_access','groupe non configuré'); return False
        try:
            link=await _create_one_time_link(bot, a.group_id, a.offer, a.expires_at)
            a.invite_link=link; a.invite_sent_at=datetime.utcnow(); a.status='active'; await db.commit()
        except Exception as e:
            a.status='failed'; await db.commit(); await log_error('vip_invite',e); return False
    msg = {
        'soiree': '✅ Paiement validé\n\nAccès accordé au Pass soirée.\n\nTu as accès à la rediffusion de cette session jusqu’à 5 heures.\n\nLien unique :',
        'total': '✅ Paiement validé\n\nAccès permanent accordé.\n\nLien unique utilisable une seule fois :',
        'javana': '✅ Paiement validé\n\nBienvenue dans COPIE 1:1 VIP JAVANA -50%.\n\nLien unique utilisable une seule fois :',
    }.get(a.offer,'✅ Paiement validé\n\nLien unique :')
    try:
        await bot.send_message(a.user_id, f'{msg}\n{a.invite_link}')
    except Exception as e:
        await log_error('vip_send_link',e)
    return True

async def validate_vip(bot:Bot, order_id:int, ok:bool):
    async with SessionLocal() as db:
        order=await db.get(VipOrder,order_id)
        if not order: return 'Commande introuvable.'
        order.status='accepted' if ok else 'rejected'
        accesses=[]
        if ok:
            for offer in [x for x in order.offers.split(',') if x]:
                group=_group_for_offer(offer)
                access=VipAccess(order_id=order.id,user_id=order.user_id,username=order.username,offer=offer,group_id=group,status='pending')
                if offer=='soiree':
                    access.expires_at=_soiree_expire_utc_for_current_or_next()
                db.add(access); await db.flush(); accesses.append(access.id)
        await db.commit()
    if not ok:
        await bot.send_message(order.user_id,'❌ Paiement refusé.')
        return 'OK'
    # Total et JAVANA : lien immédiat. Soirée : immédiat seulement entre 23h et 5h, sinon file d’attente 23h.
    immediate=[]; waiting=[]
    async with SessionLocal() as db:
        for aid in accesses:
            a=await db.get(VipAccess, aid)
            if not a: continue
            if a.offer=='soiree' and not _soiree_send_now():
                waiting.append(aid)
            else:
                immediate.append(aid)
    for aid in immediate:
        await _send_access_link(bot, aid)
    if waiting:
        release=_soiree_next_release_utc()
        await bot.send_message(order.user_id, '✅ Paiement validé\n\nTon Pass soirée est enregistré.\n\nLe lien unique sera envoyé automatiquement à 23h00.')
        await notify_admins(bot, f'🎟 Pass soirée mis en attente 23h00\nUtilisateur : @{order.username or order.user_id}\nRelease UTC : {release.isoformat()}')
    await set_cart(order.user_id,set())
    return 'OK'

async def send_due_pass_soiree_links(bot:Bot, force:bool=False):
    # À 23h, tous les Pass soirée acceptés en attente reçoivent leur lien unique.
    if not force and _now_local().hour != 23: return 0
    async with SessionLocal() as db:
        res=await db.execute(select(VipAccess).where(VipAccess.offer=='soiree', VipAccess.status=='pending'))
        ids=[a.id for a in res.scalars().all()]
    sent=0
    for aid in ids:
        if await _send_access_link(bot, aid): sent+=1
    if sent:
        await notify_admins(bot, f'🎟 Pass soirée : {sent} lien(s) envoyé(s).')
    await st.set_value('last_pass_soiree_release_at', datetime.utcnow().isoformat(timespec='seconds'))
    return sent

async def copy_media_to_vip(bot:Bot,msg:Message):
    s=get_settings()
    if not (msg.photo or msg.video or msg.document or msg.animation): return
    for gid,label in [(s.pass_soiree_group_id,'soiree'),(s.pass_total_group_id,'total')]:
        if not gid: continue
        try:
            copied=await bot.copy_message(gid,msg.chat.id,msg.message_id)
            await track(gid,copied.message_id,None,f'copy_{label}',True)
        except Exception as e: await log_error(f'copy_{label}',e)

async def expire_pass_soiree(bot:Bot):
    # Nouvelle règle V11 : NE SUPPRIME PLUS les médias du groupe Pass soirée.
    # À 05h : expulse les utilisateurs Pass soirée actifs, invalide/revoque les liens si possible, relance commerciale.
    s=get_settings()
    if not s.pass_soiree_group_id: return
    now=datetime.utcnow()
    async with SessionLocal() as db:
        res=await db.execute(select(VipAccess).where(VipAccess.offer=='soiree', VipAccess.status=='active'))
        accesses=list(res.scalars().all())
        ids=[a.id for a in accesses]
    kicked=0
    for aid in ids:
        async with SessionLocal() as db:
            a=await db.get(VipAccess, aid)
            if not a or a.status!='active': continue
            if a.expires_at and a.expires_at > now: continue
            try:
                await bot.ban_chat_member(s.pass_soiree_group_id, a.user_id)
                await bot.unban_chat_member(s.pass_soiree_group_id, a.user_id, only_if_banned=True)
                kicked += 1
            except Exception as e:
                await log_error('pass_soiree_kick',e)
            if a.invite_link:
                try: await bot.revoke_chat_invite_link(s.pass_soiree_group_id, a.invite_link)
                except Exception as e: await log_error('pass_soiree_revoke',e)
            a.status='expired'; await db.commit()
            try:
                await bot.send_message(
                    a.user_id,
                    '⏳ Ton Pass Soirée est terminé.\n\nMerci pour ta participation.\n\nTu peux déjà réserver :\n\n🎟 Pass Soirée — prochaine session\n📦 Pass Total\n💎 VIP JAVANA -50%',
                    reply_markup=vip_private_kb(set())
                )
            except Exception: pass
    await st.set_value('last_pass_soiree_expire_at', datetime.utcnow().isoformat(timespec='seconds'))
    await notify_admins(bot, f'🎟 Pass soirée expiré : {kicked} membre(s) retiré(s). Médias conservés.')

async def vip_health_text():
    last=await st.get_value('last_vip_sent_at','jamais')
    mid=await st.get_value('last_vip_message_id','-')
    image='oui' if await st.get_value('vip_image_file_id','') else 'non'
    release=await st.get_value('last_pass_soiree_release_at','jamais')
    expire=await st.get_value('last_pass_soiree_expire_at','jamais')
    state='ouvert' if await st.is_open() else 'fermé'
    async with SessionLocal() as db:
        pending=(await db.execute(select(VipAccess).where(VipAccess.offer=='soiree', VipAccess.status=='pending'))).scalars().all()
        active=(await db.execute(select(VipAccess).where(VipAccess.offer=='soiree', VipAccess.status=='active'))).scalars().all()
        pc=len(list(pending)); ac=len(list(active))
    return f'💎 VIP\n\nGroupe : {state}\nImage principale configurée : {image}\nDernier envoi pub VIP : {last}\nDernier message ID : {mid}\nPrix : soirée {await offer_price("soiree")}€ / total {await offer_price("total")}€ / JAVANA {await offer_price("javana")}€\n\nPass soirée :\nEn attente 23h : {pc}\nActifs à retirer à 05h : {ac}\nDernier envoi liens : {release}\nDernière expiration : {expire}\n\nProchain envoi automatique : pendant ouverture selon planning.'
