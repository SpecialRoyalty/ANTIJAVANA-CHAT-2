from sqlalchemy import select, func
from aiogram import Bot
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import ErrorLog, TrackedMessage, User, VipOrder, Vote, FreePassReservation
from app.services import settings as st
from app.utils.time import mid_time, slot_times, next_open_text, next_status_update_text
from app.services.freepass import reserved_count, places as freepass_places, remaining_places, is_locked as freepass_locked, published_session_key
from app.services.justice import candidate_count
from app.services.hashban import hashban_health_text

async def health_text(bot:Bot):
    s=get_settings(); slot=await st.time_slot(); start,end=slot_times(slot,s.timezone)
    groups=[('Principal',s.main_group_id),('Pass soirée',s.pass_soiree_group_id),('Pass total',s.pass_total_group_id),('VIP JAVANA',s.vip_javana_group_id),('Logs',s.log_group_id)]
    group_lines=[]; missing=[]
    for name,gid in groups:
        if not gid:
            group_lines.append(f'{name}: non configuré'); missing.append(name); continue
        try:
            me=await bot.get_me(); member=await bot.get_chat_member(gid,me.id)
            group_lines.append(f'{name}: OK ({member.status})')
        except Exception:
            group_lines.append(f'{name}: ERREUR')
    async with SessionLocal() as db:
        errors=(await db.execute(select(func.count(ErrorLog.id)))).scalar() or 0
        tracked=(await db.execute(select(func.count(TrackedMessage.id)).where(TrackedMessage.deleted==False))).scalar() or 0
        suspects=(await db.execute(select(func.count(User.id)).where(User.suspect_score>=50))).scalar() or 0
        vip_pending=(await db.execute(select(func.count(VipOrder.id)).where(VipOrder.status=='pending'))).scalar() or 0
    mode='🟢 Fonctionnement total' if not missing else '🟡 Fonctionnement partiel sans modules manquants'
    return f'''{mode}

Bot: OK
PostgreSQL: OK
Scheduler: OK

Session:
Auto: {'ON' if await st.auto_enabled() else 'OFF'}
Ouvert: {'OUI' if await st.is_open() else 'NON'}
Créneau: {slot}
Prochaine ouverture: {next_open_text(slot,s.timezone)}
Prochaine mise à jour statut: {next_status_update_text(slot,s.timezone)}
Dernière mise à jour statut: {await st.get_value('last_status_update_at','jamais')}
Prochaine justice: {mid_time(slot,s.timezone).strftime('%H:%M')}
Limite justice: {await st.justice_limit()} / session
Justifiables actuels: {await candidate_count()}
Prochaine fermeture: {end.strftime('%H:%M')}

Groupes:
{chr(10).join(group_lines)}

Contrôles:
Messages suivis non supprimés: {tracked}
Comptes suspects: {suspects}
Paiements VIP en attente: {vip_pending}
Erreurs loggées: {errors}

{await hashban_health_text()}

Diffusions planifiées:
Publicité — dernier envoi: {await st.get_value('last_ad_sent_at','jamais')} — prochain: automatique pendant ouverture
Crowdfunding — dernier envoi: {await st.get_value('last_crowd_sent_at','jamais')} — prochain: automatique pendant ouverture
VIP — dernier envoi: {await st.get_value('last_vip_sent_at','jamais')} — prochain: automatique pendant ouverture
Règles — dernier envoi: {await st.get_value('last_rules_sent_at','jamais')} — prochain: toutes les 30 min si ouvert
Top inviteurs — dernier envoi: {await st.get_value('last_top_sent_at','jamais')}
Pass gratuit — statut: {await st.get_value('free_pass_enabled','false')} — dernier envoi: {await st.get_value('last_free_pass_sent_at','jamais')}
Pass gratuit — campagne: {await published_session_key() or 'non publiée'} — verrou: {'OUI' if await freepass_locked() else 'NON'} — places restantes: {await remaining_places()} / {await freepass_places()}
'''
