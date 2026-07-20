from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import math

def now_tz(tz:str): return datetime.now(ZoneInfo(tz))
def day_key(tz:str): return now_tz(tz).strftime('%Y%m%d')
def parse_slot(slot:str):
    a,b=slot.split('-')
    sh,sm=map(int,a.split(':')); eh,em=map(int,b.split(':'))
    return (sh,sm),(eh,em)
def slot_times(slot:str,tz:str, ref:datetime|None=None):
    n=ref or now_tz(tz)
    (sh,sm),(eh,em)=parse_slot(slot)
    start=n.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end=n.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start: end += timedelta(days=1)
    if n > end: start += timedelta(days=1); end += timedelta(days=1)
    return start,end
def in_slot(slot:str,tz:str):
    n=now_tz(tz); start,end=slot_times(slot,tz,n)
    if n < start and (start-n)>timedelta(hours=12):
        start-=timedelta(days=1); end-=timedelta(days=1)
    return start <= n <= end
def minutes_to_open(slot:str,tz:str)->int:
    start,_=slot_times(slot,tz)
    delta=start-now_tz(tz)
    return max(0,int(delta.total_seconds()//60))

def countdown_text(slot:str,tz:str, achieved:bool=False)->str:
    """Affichage STABLE du compte à rebours public.

    Le scheduler tourne toutes les minutes, mais le texte public ne doit pas
    changer toutes les minutes. Cette fonction retourne donc uniquement des
    paliers stables:

    - plus d'1h: heure par heure, arrondie au-dessus (14h, 13h, 12h...)
    - dernière heure: 1h, 30 min, 10 min, 5 min, 2 min, 1 min

    Cette règle vaut pour les deux états:
    - objectif non atteint (🔴 groupe fermé)
    - objectif atteint (🟡 compte à rebours)
    """
    mins=minutes_to_open(slot,tz)
    if mins<=0:
        return 'maintenant'

    # Plus d'une heure: jamais de minutes dans le message public.
    # Exemple: 13h37 -> 14h; 13h00 -> 13h.
    if mins>60:
        return f'{math.ceil(mins/60)}h'

    # Dernière heure: paliers uniquement, pas minute par minute.
    if mins>30:
        return '1h'
    if mins>10:
        return '30 minutes'
    if mins>5:
        return '10 minutes'
    if mins>2:
        return '5 minutes'
    if mins>1:
        return '2 minutes'
    return '1 minute'

def next_open_text(slot:str,tz:str):
    # Backward-compatible exact-ish text for places that still call it.
    start,end=slot_times(slot,tz)
    delta=start-now_tz(tz)
    if delta.total_seconds()<0: return 'maintenant'
    mins=int(delta.total_seconds()//60); h=mins//60; m=mins%60
    return f'{h}h {m}min' if h else f'{m}min'

def next_status_update_text(slot:str,tz:str)->str:
    mins=minutes_to_open(slot,tz)
    if mins<=0: return 'maintenant'
    if mins>60:
        # next full hour boundary
        rem=mins%60
        wait=rem if rem else 60
        return f'dans {wait} min'
    if mins>30: return f'dans {mins-30} min'
    if mins>10: return f'dans {mins-10} min'
    if mins>5: return f'dans {mins-5} min'
    if mins>2: return f'dans {mins-2} min'
    if mins>1: return 'dans 1 min'
    return 'dans 1 min'

def mid_time(slot:str,tz:str):
    s,e=slot_times(slot,tz); return s+(e-s)/2
