from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from app.config import get_settings
from app.services.state import add_vote, ensure_status_message, vote_count
from app.services import settings as st
from app.services.session_ops import set_group_open
from app.utils.time import in_slot
from app.services.vip import send_vip_private, toggle_cart, user_cart, vip_menu_text, vip_private_kb, create_order_from_cart, payment_text_for_cart, payment_kb, vip_cart_block_reason
from app.services.crowdfunding import start_crowd_private, handle_crowd_text, handle_crowd_proof
from app.services.invites import send_invite_private
from app.services.vip import handle_vip_proof
from app.services.freepass import reserve_free_pass, refresh_free_pass_message
router=Router()

async def dm_or_deeplink(cb:CallbackQuery, bot:Bot, offer:str):
    try:
        await send_vip_private(bot, cb.from_user.id, offer)
        await cb.answer('Menu VIP ouvert en privé ✅')
    except (TelegramForbiddenError, TelegramBadRequest):
        username=get_settings().public_bot_username.strip().lstrip('@')
        if username:
            await cb.answer(url=f'https://t.me/{username}?start=vip_{offer}')
        else:
            await cb.answer('Démarre le bot en privé puis reclique.', show_alert=True)

@router.callback_query(F.data=='vote_open')
async def vote(cb:CallbackQuery,bot:Bot):
    added=await add_vote(cb.message.chat.id,cb.from_user.id)
    goal=await st.vote_goal(); votes=await vote_count(cb.message.chat.id)
    if votes>=goal and in_slot(await st.time_slot(), get_settings().timezone) and not await st.is_open():
        await set_group_open(bot,True,'auto_vote')
    else:
        await ensure_status_message(bot,cb.message.chat.id)
    await cb.answer('Vote pris en compte ✅' if added else 'Vote déjà compté ✅')

@router.callback_query(F.data.startswith('vip_offer:'))
async def vip_offer(cb:CallbackQuery, bot:Bot):
    offer=cb.data.split(':')[1]
    await dm_or_deeplink(cb, bot, offer)

@router.callback_query(F.data=='vip_menu')
async def vip_menu(cb:CallbackQuery, bot:Bot):
    items=await user_cart(cb.from_user.id)
    try:
        await cb.message.edit_text(await vip_menu_text(cb.from_user.id), reply_markup=vip_private_kb(items))
    except Exception:
        await bot.send_message(cb.from_user.id, await vip_menu_text(cb.from_user.id), reply_markup=vip_private_kb(items))
    await cb.answer()

@router.callback_query(F.data.startswith('vip_toggle:'))
async def vip_toggle(cb:CallbackQuery):
    offer=cb.data.split(':')[1]
    items=await toggle_cart(cb.from_user.id, offer)
    await cb.message.edit_text(await vip_menu_text(cb.from_user.id), reply_markup=vip_private_kb(items))
    await cb.answer('Sélection mise à jour')

@router.callback_query(F.data=='vip_checkout')
async def vip_checkout(cb:CallbackQuery):
    items=await user_cart(cb.from_user.id)
    reason=await vip_cart_block_reason(cb.from_user.id, items)
    if reason:
        await cb.answer(reason, show_alert=True)
        return
    await create_order_from_cart(cb.from_user.id, cb.from_user.username or cb.from_user.full_name or '')
    await cb.message.edit_text(await payment_text_for_cart(cb.from_user.id), reply_markup=payment_kb())
    await cb.answer()

@router.callback_query(F.data=='crowd_join')
async def crowd(cb:CallbackQuery, bot:Bot):
    try:
        await start_crowd_private(bot, cb.from_user.id)
        await cb.answer('Je t’ai envoyé les infos en privé ✅')
    except (TelegramForbiddenError, TelegramBadRequest):
        username=get_settings().public_bot_username.strip().lstrip('@')
        if username: await cb.answer(url=f'https://t.me/{username}?start=crowd')
        else: await cb.answer('Démarre le bot en privé puis reclique.', show_alert=True)


@router.callback_query(F.data=='invite_private')
async def invite_private(cb:CallbackQuery, bot:Bot):
    try:
        await send_invite_private(bot, cb.from_user.id)
        await cb.answer('Lien envoyé en privé ✅')
    except (TelegramForbiddenError, TelegramBadRequest):
        username=get_settings().public_bot_username.strip().lstrip('@')
        if username: await cb.answer(url=f'https://t.me/{username}?start=invite')
        else: await cb.answer('Démarre le bot en privé puis reclique.', show_alert=True)


@router.callback_query(F.data=='freepass_reserve')
async def freepass_reserve_cb(cb:CallbackQuery, bot:Bot):
    # Fallback uniquement si PUBLIC_BOT_USERNAME n'est pas configuré et que le
    # bouton URL ne peut pas être généré. Le fonctionnement recommandé reste le
    # deep-link /start freepass.
    username=cb.from_user.username or cb.from_user.full_name or ''
    ok,msg=await reserve_free_pass(bot, cb.from_user.id, username)
    if ok:
        try:
            await bot.send_message(cb.from_user.id, msg)
        except Exception:
            await cb.answer('Ouvre le bot en privé pour confirmer ta réservation.', show_alert=True)
            return
        await refresh_free_pass_message(bot)
        await cb.answer('Place réservée ✅', show_alert=True)
    else:
        await cb.answer(msg, show_alert=True)

@router.callback_query(F.data.startswith('vip_pay:') | F.data.startswith('crowd_pay:'))
async def paynoop(cb:CallbackQuery):
    method=cb.data.split(':')[1]
    s=get_settings()
    info={'paypal':s.paypal_text or 'PayPal non configuré.', 'revolut':s.revolut_text or 'Revolut non configuré.', 'crypto':s.crypto_text or 'Crypto non configuré.'}.get(method,'')
    await cb.message.answer(f'💳 {method.upper()}\n\n{info}\n\nAprès paiement, envoie une capture ici.')
    await cb.answer('Instructions envoyées ✅')


@router.message(F.chat.type=='private')
async def private_user_flows(msg:Message, bot:Bot):
    # Parcours privés pour les non-admins et fallback pour tout utilisateur.
    if not msg.from_user:
        return
    if await handle_crowd_text(msg):
        return
    if await handle_crowd_proof(bot, msg):
        return
    if await handle_vip_proof(bot, msg):
        return
