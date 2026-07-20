from sqlalchemy import select, func
import random
from aiogram import Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import Crowdfunding, PaymentProof
from app.services import settings as st
from app.services.state import track
from app.services.session_ops import notify_admins
from app.keyboards.common import pay_kb, admin_validate_kb

def bar(cur:int,target:int):
    pct=0 if target<=0 else min(cur/target,1)
    full=int(pct*10)
    return '█'*full+'░'*(10-full)+f' {int(pct*100)}%'
async def get_campaign():
    active_id=int(await st.get_value('active_crowd_id','0') or '0')
    async with SessionLocal() as db:
        c=None
        if active_id:
            c=await db.get(Crowdfunding, active_id)
            if c and not c.active: c=None
        if not c:
            res=await db.execute(select(Crowdfunding).where(Crowdfunding.active==True).order_by(Crowdfunding.id.asc()).limit(1))
            c=res.scalar_one_or_none()
        if not c:
            c=Crowdfunding(text='🎯 FINANCEMENT COMMUNAUTAIRE', active=True)
            db.add(c); await db.flush(); await st.set_value('active_crowd_id', str(c.id)); await db.commit()
        return c

async def random_active_campaign():
    async with SessionLocal() as db:
        res=await db.execute(select(Crowdfunding).where(Crowdfunding.active==True))
        rows=list(res.scalars().all())
    if not rows: return await get_campaign()
    return random.choice(rows)

async def create_campaign():
    async with SessionLocal() as db:
        count=int((await db.execute(select(func.count(Crowdfunding.id)))).scalar() or 0)
        if count>=2: return False, 'Maximum 2 campagnes crowdfunding à la fois.'
        c=Crowdfunding(text='🎯 FINANCEMENT COMMUNAUTAIRE', target_amount=1000, active=True); db.add(c); await db.flush(); cid=c.id; await db.commit()
    await st.set_value('active_crowd_id', str(cid))
    return True, f'✅ Campagne créée et activée : #{cid}'

async def set_active_campaign(cid:int):
    await st.set_value('active_crowd_id', str(cid))

async def toggle_campaign(cid:int):
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid)
        if not c: return False
        c.active=not c.active
        await db.commit(); return True

async def delete_campaign(cid:int):
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid)
        if not c: return False
        await db.delete(c); await db.commit()
    if await st.get_value('active_crowd_id','0') == str(cid): await st.set_value('active_crowd_id','0')
    return True

async def campaigns_text():
    active_id=await st.get_value('active_crowd_id','0')
    async with SessionLocal() as db:
        res=await db.execute(select(Crowdfunding).order_by(Crowdfunding.id.asc()))
        rows=list(res.scalars().all())
    if not rows: return 'Aucune campagne.'
    lines=['💰 Campagnes crowdfunding', '', 'Si plusieurs campagnes sont actives, l’envoi automatique choisit une campagne active au hasard.']
    for c in rows:
        mark='✅ active principale' if str(c.id)==str(active_id) else ('🟢 active' if c.active else '🔴 inactive')
        lines.append(f'#{c.id} — {mark} — {c.current_amount}€/{c.target_amount}€ — image: {"OK" if c.image_file_id else "non"}')
    return '\n'.join(lines)

async def campaign_detail(cid:int):
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid)
    if not c: return 'Campagne introuvable.', None
    txt=f'💰 Campagne #{c.id}\n\nStatut : {"active" if c.active else "off"}\nObjectif : {c.current_amount}€ / {c.target_amount}€\nImage : {"oui" if c.image_file_id else "non"}\n\nTexte :\n{c.text or c.title}'
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Définir principale', callback_data=f'crowd_active:{c.id}'), InlineKeyboardButton(text='🟢/🔴 ON/OFF', callback_data=f'crowd_toggle:{c.id}')],
        [InlineKeyboardButton(text='📝 Modifier texte', callback_data=f'await:crowd_text:{c.id}'), InlineKeyboardButton(text='🎯 Modifier objectif', callback_data=f'await:crowd_target:{c.id}')],
        [InlineKeyboardButton(text='🖼 Modifier image', callback_data=f'await:crowd_image:{c.id}'), InlineKeyboardButton(text='📤 Publier cette campagne', callback_data=f'crowd_send_one:{c.id}')],
        [InlineKeyboardButton(text='🗑 Supprimer', callback_data=f'crowd_delete:{c.id}')],
        [InlineKeyboardButton(text='📋 Retour campagnes', callback_data='crowd_list')]
    ])
    return txt,kb

async def campaigns_kb():
    async with SessionLocal() as db:
        res=await db.execute(select(Crowdfunding).order_by(Crowdfunding.id.asc()))
        rows=list(res.scalars().all())
    kb=[]
    for c in rows:
        kb.append([InlineKeyboardButton(text=f'#{c.id} {"🟢" if c.active else "🔴"} {c.current_amount}/{c.target_amount}€', callback_data=f'crowd_manage:{c.id}')])
    kb.append([InlineKeyboardButton(text='➕ Nouvelle campagne',callback_data='crowd_new')])
    kb.append([InlineKeyboardButton(text='⬅️ Retour crowdfunding',callback_data='adm_crowd')])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def send_crowd_ad(bot:Bot, force:bool=False):
    if not force and not await st.is_open(): return None
    c=await random_active_campaign(); s=get_settings()
    text=f'{c.text or c.title}\n\nObjectif :\n{c.current_amount}€ / {c.target_amount}€\n\n{bar(c.current_amount,c.target_amount)}'
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='💰 Je participe',callback_data='crowd_join')]])
    
    if c.image_file_id:
        m=await bot.send_photo(s.main_group_id,c.image_file_id,caption=text,reply_markup=kb)
        await track(s.main_group_id,m.message_id,None,'crowdfunding',True)
    else:
        m=await bot.send_message(s.main_group_id,text,reply_markup=kb)
        await track(s.main_group_id,m.message_id,None,'crowdfunding',False)
    from datetime import datetime
    await st.set_value('last_crowd_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    await st.set_value('last_crowd_message_id', str(m.message_id))
    await st.set_value('last_crowd_campaign_id', str(c.id))
    return m.message_id

async def send_campaign_by_id(bot:Bot, cid:int, force:bool=True):
    if not force and not await st.is_open(): return None
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid)
    if not c: return None
    s=get_settings()
    text=f'{c.text or c.title}\n\nObjectif :\n{c.current_amount}€ / {c.target_amount}€\n\n{bar(c.current_amount,c.target_amount)}'
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='💰 Je participe',callback_data='crowd_join')]])
    if c.image_file_id:
        m=await bot.send_photo(s.main_group_id,c.image_file_id,caption=text,reply_markup=kb)
        await track(s.main_group_id,m.message_id,None,'crowdfunding',True)
    else:
        m=await bot.send_message(s.main_group_id,text,reply_markup=kb)
        await track(s.main_group_id,m.message_id,None,'crowdfunding',False)
    await st.set_value('last_crowd_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    await st.set_value('last_crowd_message_id', str(m.message_id))
    await st.set_value('last_crowd_campaign_id', str(c.id))
    return m.message_id

async def start_crowd_private(bot:Bot, user_id:int):
    c=await get_campaign()
    await bot.send_message(user_id, f'💰 Participation\n\nObjectif actuel : {c.current_amount}€ / {c.target_amount}€\n\nRéponds avec le montant que tu veux envoyer.')
    await st.set_value(f'crowd_state:{user_id}','amount')
    await st.set_value(f'crowd_campaign:{user_id}', str(c.id))
async def handle_crowd_text(msg:Message):
    if not msg.from_user:
        return False
    state=await st.get_value(f'crowd_state:{msg.from_user.id}','')
    if state!='amount': return False
    amount=int(''.join(x for x in (msg.text or '') if x.isdigit()) or '0')
    if amount <= 0:
        await msg.answer('Envoie un montant valide, par exemple 10.')
        return True
    await st.set_value(f'crowd_amount:{msg.from_user.id}',str(amount))
    await st.set_value(f'crowd_state:{msg.from_user.id}','proof')
    await msg.answer(f'Montant sélectionné : {amount}€\n\nChoisis un moyen de paiement :',reply_markup=pay_kb('crowd_pay'))
    return True

def _proof_file_id(msg:Message):
    if msg.photo: return msg.photo[-1].file_id
    if msg.document: return msg.document.file_id
    return None

async def handle_crowd_proof(bot:Bot,msg:Message):
    if not msg.from_user:
        return False
    state=await st.get_value(f'crowd_state:{msg.from_user.id}','')
    fid=_proof_file_id(msg)
    if state!='proof' or not fid: return False
    amount=int(await st.get_value(f'crowd_amount:{msg.from_user.id}','0') or '0')
    async with SessionLocal() as db:
        cid=await st.get_value(f'crowd_campaign:{msg.from_user.id}','0')
        p=PaymentProof(user_id=msg.from_user.id,kind=f'crowdfunding:{cid}',amount=amount,screenshot_file_id=fid,status='pending'); db.add(p); await db.commit(); pid=p.id
    await st.set_value(f'crowd_state:{msg.from_user.id}','pending')
    await msg.answer('✅ Capture reçue. Validation admin en attente.')
    await notify_admins(bot,f'💰 Crowdfunding à valider\n\nUtilisateur : @{msg.from_user.username or msg.from_user.full_name}\nMontant : {amount}€',admin_validate_kb('crowd',pid))
    return True
async def validate_crowd(bot:Bot,pid:int,ok:bool):
    async with SessionLocal() as db:
        p=await db.get(PaymentProof,pid)
        if not p: return 'Introuvable'
        p.status='accepted' if ok else 'rejected'
        updated_campaign_id=0
        if ok:
            cid=0
            if ':' in (p.kind or ''):
                try: cid=int(p.kind.split(':',1)[1])
                except Exception: cid=0
            c=await db.get(Crowdfunding,cid) if cid else None
            if not c:
                res=await db.execute(select(Crowdfunding).order_by(Crowdfunding.id.desc()).limit(1)); c=res.scalar_one_or_none()
            if c:
                c.current_amount+=p.amount
                updated_campaign_id=c.id
        await db.commit()
    await bot.send_message(p.user_id,('✅ Participation validée\n\nMontant ajouté : '+str(p.amount)+'€\n\nMerci pour ton soutien. Une fois les médias achetés, tous les participants recevront automatiquement une copie.') if ok else '❌ Participation refusée.')
    if ok and updated_campaign_id:
        await refresh_last_crowd_message(bot, updated_campaign_id)
    return 'OK'


async def set_campaign_text(text:str, cid:int|None=None):
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid) if cid else None
        if not c: c=await get_campaign()
        c=await db.get(Crowdfunding,c.id)
        c.text=text; await db.commit()

async def set_campaign_target(amount:int, cid:int|None=None):
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid) if cid else None
        if not c: c=await get_campaign()
        c=await db.get(Crowdfunding,c.id)
        c.target_amount=max(amount,1); await db.commit()

async def set_campaign_image(file_id:str, cid:int|None=None):
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid) if cid else None
        if not c: c=await get_campaign()
        c=await db.get(Crowdfunding,c.id)
        c.image_file_id=file_id; await db.commit()

async def refresh_last_crowd_message(bot:Bot, cid:int):
    last_cid=await st.get_value('last_crowd_campaign_id','0')
    mid=await st.get_value('last_crowd_message_id','')
    if str(cid)!=str(last_cid) or not mid: return
    async with SessionLocal() as db:
        c=await db.get(Crowdfunding,cid)
    if not c: return
    text=f'{c.text or c.title}\n\nObjectif :\n{c.current_amount}€ / {c.target_amount}€\n\n{bar(c.current_amount,c.target_amount)}'
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='💰 Je participe',callback_data='crowd_join')]])
    try:
        if c.image_file_id:
            await bot.edit_message_caption(chat_id=get_settings().main_group_id, message_id=int(mid), caption=text, reply_markup=kb)
        else:
            await bot.edit_message_text(text, chat_id=get_settings().main_group_id, message_id=int(mid), reply_markup=kb)
    except Exception:
        pass

async def stats_text():
    c=await get_campaign()
    return f'💰 Crowdfunding\n\nMontant : {c.current_amount}€ / {c.target_amount}€\n\n{bar(c.current_amount,c.target_amount)}\nImage : {"OK" if c.image_file_id else "non configurée"}'

async def crowd_health_text():
    c=await get_campaign()
    last=await st.get_value('last_crowd_sent_at','jamais')
    mid=await st.get_value('last_crowd_message_id','-')
    state='ouvert' if await st.is_open() else 'fermé'
    return f'💰 Crowdfunding\n\nGroupe : {state}\nDernier envoi : {last}\nDernier message ID : {mid}\nProgression : {c.current_amount}€ / {c.target_amount}€\n{bar(c.current_amount,c.target_amount)}\nProchain envoi automatique : pendant ouverture selon planning.'
