from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import func, select

from app.db.models import MediaHash
from app.db.session import SessionLocal
from app.services.state import log_error


# Cache mémoire des albums reçus. Telegram envoie chaque élément d'un album comme
# un Message séparé et ne fournit pas d'API pour relire les autres éléments.
# Ce cache permet donc à /pedo et /hashdemande, lorsqu'ils répondent à un élément,
# de traiter tout l'album tant que le processus n'a pas redémarré.
_ALBUM_CACHE: dict[tuple[int, str], list[Message]] = {}
_ALBUM_CACHE_AT: dict[tuple[int, str], float] = {}
_ALBUM_TTL_SECONDS = 6 * 60 * 60


@dataclass(slots=True)
class HashBanMatch:
    matched: bool = False
    method: str = "none"  # file_unique_id | sha256 | none
    key: str | None = None
    media_type: str | None = None


@dataclass(slots=True)
class HashAuditEntry:
    media_type: str
    file_unique_id: str
    sha256: str | None
    id_present: bool
    id_banned: bool
    sha_present: bool
    sha_banned: bool


def media_file_entries(msg: Message) -> list[tuple[str, str, str]]:
    """Retourne (file_unique_id, file_id, type) pour le média du message."""
    if msg.photo:
        media = msg.photo[-1]
        return [(media.file_unique_id, media.file_id, "photo")]
    if msg.video:
        return [(msg.video.file_unique_id, msg.video.file_id, "video")]
    if msg.document:
        return [(msg.document.file_unique_id, msg.document.file_id, "document")]
    if msg.animation:
        return [(msg.animation.file_unique_id, msg.animation.file_id, "animation")]
    if msg.audio:
        return [(msg.audio.file_unique_id, msg.audio.file_id, "audio")]
    if msg.voice:
        return [(msg.voice.file_unique_id, msg.voice.file_id, "voice")]
    if msg.video_note:
        return [(msg.video_note.file_unique_id, msg.video_note.file_id, "video_note")]
    return []


def remember_media_message(msg: Message) -> None:
    """Mémorise un élément d'album afin de pouvoir auditer/bannir l'album complet."""
    if not msg.media_group_id or not media_file_entries(msg):
        return
    now = time.monotonic()
    expired = [key for key, stored_at in _ALBUM_CACHE_AT.items() if now - stored_at > _ALBUM_TTL_SECONDS]
    for key in expired:
        _ALBUM_CACHE.pop(key, None)
        _ALBUM_CACHE_AT.pop(key, None)

    key = (msg.chat.id, str(msg.media_group_id))
    items = _ALBUM_CACHE.setdefault(key, [])
    if all(existing.message_id != msg.message_id for existing in items):
        items.append(msg)
        items.sort(key=lambda item: item.message_id)
    _ALBUM_CACHE_AT[key] = now


def related_media_messages(msg: Message) -> list[Message]:
    """Retourne tout l'album connu, sinon seulement le message ciblé."""
    remember_media_message(msg)
    if not msg.media_group_id:
        return [msg] if media_file_entries(msg) else []
    key = (msg.chat.id, str(msg.media_group_id))
    items = _ALBUM_CACHE.get(key, [])
    return list(items) if items else ([msg] if media_file_entries(msg) else [])


async def file_sha256(bot: Bot, file_id: str) -> str | None:
    """Télécharge le fichier Telegram et retourne un SHA256 préfixé."""
    try:
        bio = await bot.download(file_id)
        if not bio:
            return None
        bio.seek(0)
        return "sha256:" + hashlib.sha256(bio.read()).hexdigest()
    except Exception as exc:
        await log_error("hashban_sha256", exc)
        return None


async def _upsert_hash(
    db,
    *,
    key: str,
    file_id: str,
    media_type: str,
    user_id: int | None,
    banned: bool,
) -> None:
    result = await db.execute(select(MediaHash).where(MediaHash.file_unique_id == key))
    row = result.scalar_one_or_none()
    if row is None:
        db.add(
            MediaHash(
                user_id=user_id,
                file_unique_id=key,
                file_id=file_id,
                media_type=media_type,
                banned=banned,
            )
        )
        return

    row.file_id = file_id
    row.media_type = media_type
    if user_id is not None:
        row.user_id = user_id
    if banned:
        row.banned = True


async def store_message_hashes(msg: Message, bot: Bot, *, banned: bool = False) -> int:
    """Enregistre file_unique_id ET SHA256 pour le média du message."""
    entries = media_file_entries(msg)
    if not entries:
        return 0

    stored = 0
    user_id = msg.from_user.id if msg.from_user else None
    async with SessionLocal() as db:
        for unique, file_id, media_type in entries:
            await _upsert_hash(
                db,
                key=unique,
                file_id=file_id,
                media_type=media_type,
                user_id=user_id,
                banned=banned,
            )
            stored += 1

            sha = await file_sha256(bot, file_id)
            if sha:
                await _upsert_hash(
                    db,
                    key=sha,
                    file_id=file_id,
                    media_type=media_type,
                    user_id=user_id,
                    banned=banned,
                )
                stored += 1
        await db.commit()
    return stored


async def ban_hash_from_message(msg: Message, bot: Bot | None = None) -> int:
    """Blacklist le média ou tout son album par ID Telegram et par SHA256."""
    messages = related_media_messages(msg)
    if not messages:
        return 0

    if bot is not None:
        count = 0
        for media_message in messages:
            count += await store_message_hashes(media_message, bot, banned=True)
        return count

    count = 0
    async with SessionLocal() as db:
        for media_message in messages:
            user_id = media_message.from_user.id if media_message.from_user else None
            for unique, file_id, media_type in media_file_entries(media_message):
                await _upsert_hash(
                    db,
                    key=unique,
                    file_id=file_id,
                    media_type=media_type,
                    user_id=user_id,
                    banned=True,
                )
                count += 1
        await db.commit()
    return count


async def _key_status(key: str | None) -> tuple[bool, bool]:
    if not key:
        return False, False
    async with SessionLocal() as db:
        result = await db.execute(
            select(MediaHash.banned).where(MediaHash.file_unique_id == key)
        )
        values = list(result.scalars().all())
    return bool(values), any(values)


async def audit_hashes(bot: Bot, msg: Message) -> list[HashAuditEntry]:
    """Calcule et vérifie les hashes du média ciblé ou de son album complet."""
    entries: list[HashAuditEntry] = []
    for media_message in related_media_messages(msg):
        for unique, file_id, media_type in media_file_entries(media_message):
            sha = await file_sha256(bot, file_id)
            id_present, id_banned = await _key_status(unique)
            sha_present, sha_banned = await _key_status(sha)
            entries.append(
                HashAuditEntry(
                    media_type=media_type,
                    file_unique_id=unique,
                    sha256=sha,
                    id_present=id_present,
                    id_banned=id_banned,
                    sha_present=sha_present,
                    sha_banned=sha_banned,
                )
            )
    return entries


def format_hash_audit(entries: list[HashAuditEntry], *, title: str = "🔍 AUDIT HASH") -> str:
    if not entries:
        return f"{title}\n\n❌ Aucun média compatible trouvé."

    blocks: list[str] = [title]
    if len(entries) > 1:
        blocks.append(f"\nAlbum détecté : {len(entries)} médias")

    for index, entry in enumerate(entries, start=1):
        blocks.append(
            "\n────────────\n"
            f"Média {index}/{len(entries)} — {entry.media_type}\n\n"
            f"file_unique_id :\n{entry.file_unique_id}\n"
            f"Présent en base : {'✅ OUI' if entry.id_present else '❌ NON'}\n"
            f"Blacklist ID : {'✅ OUI' if entry.id_banned else '❌ NON'}\n\n"
            f"SHA256 :\n{entry.sha256 or '⚠️ CALCUL IMPOSSIBLE'}\n"
            f"Présent en base : {'✅ OUI' if entry.sha_present else '❌ NON'}\n"
            f"Blacklist SHA : {'✅ OUI' if entry.sha_banned else '❌ NON'}"
        )

    fully_banned = sum(1 for entry in entries if entry.id_banned and entry.sha_banned)
    partially_banned = sum(1 for entry in entries if entry.id_banned != entry.sha_banned)
    if fully_banned == len(entries):
        verdict = "🟢 Tous les médias sont blacklistés par ID et SHA256."
    elif fully_banned or partially_banned:
        verdict = "🟠 Blacklist partielle : au moins une empreinte manque."
    else:
        verdict = "🔴 Aucun média n'est totalement blacklisté."

    blocks.append(
        "\n────────────\n"
        "Résumé\n\n"
        f"Médias contrôlés : {len(entries)}\n"
        f"Totalement blacklistés : {fully_banned}\n"
        f"Partiellement blacklistés : {partially_banned}\n\n"
        f"Verdict : {verdict}"
    )
    return "".join(blocks)


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    """Découpe sans dépasser la limite Telegram de 4096 caractères."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n────────────\n"):
        candidate = block if not current else current + "\n────────────\n" + block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


async def find_banned_hash(bot: Bot, msg: Message) -> HashBanMatch:
    """Cherche d'abord l'ID Telegram, puis le SHA256 du contenu."""
    entries = media_file_entries(msg)
    if not entries:
        return HashBanMatch()

    async with SessionLocal() as db:
        for unique, _file_id, media_type in entries:
            result = await db.execute(
                select(MediaHash.id).where(
                    MediaHash.file_unique_id == unique,
                    MediaHash.banned.is_(True),
                )
            )
            if result.scalar_one_or_none() is not None:
                return HashBanMatch(True, "file_unique_id", unique, media_type)

    for _unique, file_id, media_type in entries:
        sha = await file_sha256(bot, file_id)
        if not sha:
            continue
        async with SessionLocal() as db:
            result = await db.execute(
                select(MediaHash.id).where(
                    MediaHash.file_unique_id == sha,
                    MediaHash.banned.is_(True),
                )
            )
            if result.scalar_one_or_none() is not None:
                return HashBanMatch(True, "sha256", sha, media_type)

    return HashBanMatch()


async def record_repost_verification(
    *,
    match: HashBanMatch,
    deleted: bool,
    user_banned: bool,
    pipeline_stopped: bool,
    user_id: int | None,
    chat_id: int,
    message_id: int,
) -> None:
    """Conserve une preuve persistante du dernier repost réellement détecté."""
    from app.services import settings as st

    total = int(await st.get_value("hashban_reposts_detected", "0") or "0") + 1
    success = deleted and user_banned and pipeline_stopped
    success_total = int(await st.get_value("hashban_reposts_blocked", "0") or "0")
    failure_total = int(await st.get_value("hashban_reposts_failed", "0") or "0")

    if success:
        success_total += 1
    else:
        failure_total += 1

    await st.set_value("hashban_reposts_detected", str(total))
    await st.set_value("hashban_reposts_blocked", str(success_total))
    await st.set_value("hashban_reposts_failed", str(failure_total))
    await st.set_value("hashban_last_at", datetime.utcnow().isoformat(timespec="seconds"))
    await st.set_value("hashban_last_method", match.method)
    await st.set_value("hashban_last_media_type", match.media_type or "unknown")
    await st.set_value("hashban_last_deleted", str(deleted).lower())
    await st.set_value("hashban_last_user_banned", str(user_banned).lower())
    await st.set_value("hashban_last_pipeline_stopped", str(pipeline_stopped).lower())
    await st.set_value("hashban_last_success", str(success).lower())
    await st.set_value("hashban_last_user_id", str(user_id or ""))
    await st.set_value("hashban_last_chat_id", str(chat_id))
    await st.set_value("hashban_last_message_id", str(message_id))

    method_key = f"hashban_detected_{match.method}"
    method_total = int(await st.get_value(method_key, "0") or "0") + 1
    await st.set_value(method_key, str(method_total))


async def banned_hash_count() -> int:
    async with SessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count(MediaHash.id)).where(MediaHash.banned.is_(True))
                )
            ).scalar()
            or 0
        )


async def hashban_health_text() -> str:
    """Indicateur basé uniquement sur de vrais reposts détectés."""
    from app.services import settings as st

    detected = int(await st.get_value("hashban_reposts_detected", "0") or "0")
    blocked = int(await st.get_value("hashban_reposts_blocked", "0") or "0")
    failed = int(await st.get_value("hashban_reposts_failed", "0") or "0")
    by_id = int(await st.get_value("hashban_detected_file_unique_id", "0") or "0")
    by_sha = int(await st.get_value("hashban_detected_sha256", "0") or "0")

    if detected == 0:
        state = "🟡 EN ATTENTE DE VÉRIFICATION RÉELLE"
    elif failed > 0 and (await st.get_value("hashban_last_success", "false")) != "true":
        state = "🔴 ERREUR SUR LE DERNIER REPOST"
    else:
        state = "🟢 VÉRIFIÉ SUR REPOST RÉEL"

    yes_no = lambda key: "✅" if key == "true" else "❌"
    return f"""🛡️ HASH-BAN

État : {state}
Hash blacklistés : {await banned_hash_count()}
Reposts détectés : {detected}
Reposts bloqués complètement : {blocked}
Échecs de pipeline : {failed}
Détection par ID Telegram : {by_id}
Détection par SHA256 : {by_sha}

Dernier repost : {await st.get_value('hashban_last_at', 'jamais')}
Méthode : {await st.get_value('hashban_last_method', 'aucune')}
Type : {await st.get_value('hashban_last_media_type', 'inconnu')}
Message supprimé : {yes_no(await st.get_value('hashban_last_deleted', 'false'))}
Utilisateur banni : {yes_no(await st.get_value('hashban_last_user_banned', 'false'))}
Pipeline arrêté avant copie VIP : {yes_no(await st.get_value('hashban_last_pipeline_stopped', 'false'))}"""
