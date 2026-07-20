import re
from datetime import datetime
from aiogram.types import User as TgUser
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import User

def display_name(u):
    if getattr(u,'username',None): return '@'+u.username
    return (getattr(u,'full_name','') or 'Utilisateur').strip()
def anon_name(username:str|None, full_name:str=''):
    name=('@'+username) if username else (full_name or 'membre')
    if len(name)<=3: return name[0]+'*'
    return name[:3]+'****'
def is_gibberish(name:str):
    n=re.sub(r'[^A-Za-z]','',name or '')
    if len(n)<4: return False
    if re.search(r'(.)\1{3,}', n): return True
    vowels=sum(c.lower() in 'aeiouy' for c in n)
    ratio=vowels/len(n)
    return ratio<0.18 or ratio>0.82 or bool(re.match(r'^[A-Z]?[a-z]{1,2}[a-z]{1,2}\s+[A-Z]?[a-z]{1,4}$', name or ''))
async def upsert_user(tgu:TgUser):
    s=get_settings()
    async with SessionLocal() as db:
        u=await db.get(User,tgu.id)
        if not u:
            score=0
            if not tgu.username: score+=10
            if is_gibberish(tgu.full_name): score+=20
            u=User(id=tgu.id, username=tgu.username, full_name=tgu.full_name or '', suspect_score=score)
            db.add(u)
        u.username=tgu.username; u.full_name=tgu.full_name or ''; u.last_seen=datetime.utcnow()
        u.is_admin=tgu.id in s.admin_ids; u.is_trusted=(tgu.id in s.trusted_ids or tgu.id in s.admin_ids)
        await db.commit(); return u
async def protected(user_id:int):
    s=get_settings(); return user_id in s.all_admin_ids
