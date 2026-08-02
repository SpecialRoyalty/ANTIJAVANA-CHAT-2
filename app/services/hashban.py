from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message
from PIL import Image
from sqlalchemy import func, select

from app.db.models import MediaHash, VideoFingerprint
from app.db.session import SessionLocal
from app.services.state import log_error

_ALBUM_CACHE: dict[tuple[int, str], list[Message]] = {}
_ALBUM_CACHE_AT: dict[tuple[int, str], float] = {}
_ALBUM_TTL_SECONDS = 6 * 60 * 60

# Seuils prudents : le fingerprint sert à reconnaître une vidéo réencodée,
# tout en limitant les faux positifs.
_DURATION_TOLERANCE_SECONDS = 3
_ASPECT_RATIO_TOLERANCE = 0.08
_MAX_AVERAGE_HAMMING = 9.0
_MAX_SINGLE_FRAME_HAMMING = 20


@dataclass(slots=True)
class HashBanMatch:
    matched: bool = False
    method: str = "none"  # file_unique_id | sha256 | perceptual_video | none
    key: str | None = None
    media_type: str | None = None
    similarity: float | None = None


@dataclass(slots=True)
class HashAuditEntry:
    media_type: str
    file_unique_id: str
    sha256: str | None
    id_present: bool
    id_banned: bool
    sha_present: bool
    sha_banned: bool
    perceptual_hash: str | None = None
    perceptual_present: bool = False
    perceptual_banned: bool = False
    perceptual_match: bool = False
    perceptual_similarity: float | None = None
    message_id: int | None = None
    media_group_id: str | None = None
    file_size: int | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None


def media_file_entries(msg: Message) -> list[tuple[str, str, str]]:
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


def _media_metadata(msg: Message) -> tuple[int | None, int | None, int | None, int | None]:
    media = msg.photo[-1] if msg.photo else (
        msg.video or msg.document or msg.animation or msg.audio or msg.voice or msg.video_note
    )
    if media is None:
        return None, None, None, None
    return (
        getattr(media, "file_size", None),
        getattr(media, "duration", None),
        getattr(media, "width", None),
        getattr(media, "height", None),
    )


def remember_media_message(msg: Message) -> None:
    if not msg.media_group_id or not media_file_entries(msg):
        return
    now = time.monotonic()
    for key in [k for k, at in _ALBUM_CACHE_AT.items() if now - at > _ALBUM_TTL_SECONDS]:
        _ALBUM_CACHE.pop(key, None)
        _ALBUM_CACHE_AT.pop(key, None)
    key = (msg.chat.id, str(msg.media_group_id))
    items = _ALBUM_CACHE.setdefault(key, [])
    if all(existing.message_id != msg.message_id for existing in items):
        items.append(msg)
        items.sort(key=lambda item: item.message_id)
    _ALBUM_CACHE_AT[key] = now


def related_media_messages(msg: Message) -> list[Message]:
    remember_media_message(msg)
    if not msg.media_group_id:
        return [msg] if media_file_entries(msg) else []
    return list(_ALBUM_CACHE.get((msg.chat.id, str(msg.media_group_id)), [msg]))


async def _download_to_temp(bot: Bot, file_id: str, suffix: str = ".bin") -> str | None:
    try:
        fd, path = tempfile.mkstemp(prefix="hashban_", suffix=suffix)
        os.close(fd)
        await bot.download(file_id, destination=path)
        return path
    except Exception as exc:
        await log_error("hashban_download", exc)
        try:
            if 'path' in locals() and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass
        return None


def _sha256_path(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


async def file_sha256(bot: Bot, file_id: str) -> str | None:
    path = await _download_to_temp(bot, file_id)
    if not path:
        return None
    try:
        return _sha256_path(path)
    except Exception as exc:
        await log_error("hashban_sha256", exc)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _dhash(image_path: str) -> str:
    with Image.open(image_path) as image:
        image = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | int(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return f"{value:016x}"


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _video_frame_hashes(path: str, duration: int | None) -> list[str]:
    """Extrait plusieurs images représentatives, y compris pour les clips très courts.

    L'ancienne stratégie utilisait une image toutes les quatre secondes au minimum.
    Une vidéo d'une seconde ne produisait donc souvent qu'une seule image, tandis que
    le fingerprint exigeait au moins deux hashes. Pour les clips de deux secondes ou
    moins, on échantillonne désormais à 4 images/seconde. Si FFmpeg ne fournit malgré
    tout qu'une seule image (clip fixe ou fichier atypique), cette image est dupliquée
    afin de conserver un fingerprint exploitable et comparable.
    """
    short_clip = duration is not None and duration <= 2
    with tempfile.TemporaryDirectory(prefix="hashban_frames_") as directory:
        output = str(Path(directory) / "frame_%02d.jpg")
        if short_clip:
            video_filter = "fps=4,scale=192:-2"
            max_frames = "8"
        else:
            interval = max(1, int((duration or 30) / 6))
            video_filter = f"fps=1/{interval},scale=192:-2"
            max_frames = "8"

        command = [
            _ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-i", path,
            "-vf", video_filter, "-frames:v", max_frames, "-q:v", "3", output,
        ]
        subprocess.run(
            command,
            check=True,
            timeout=90,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        frames = sorted(Path(directory).glob("frame_*.jpg"))
        hashes = [_dhash(str(frame)) for frame in frames]
        if len(hashes) == 1:
            hashes.append(hashes[0])
        return hashes


def _fingerprint_key(frame_hashes: list[str]) -> str | None:
    if not frame_hashes:
        return None
    if len(frame_hashes) == 1:
        frame_hashes = [frame_hashes[0], frame_hashes[0]]
    return "vphash:" + ",".join(frame_hashes)


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _resample_hashes(values: list[str], target: int) -> list[str]:
    if len(values) == target:
        return values
    if target <= 1:
        return [values[0]]
    return [values[round(i * (len(values) - 1) / (target - 1))] for i in range(target)]


def _compare_fingerprints(left: str, right: str) -> tuple[bool, float]:
    left_values = left.removeprefix("vphash:").split(",")
    right_values = right.removeprefix("vphash:").split(",")
    count = min(len(left_values), len(right_values), 6)
    if count < 2:
        return False, 0.0
    a = _resample_hashes(left_values, count)
    b = _resample_hashes(right_values, count)
    distances = [_hamming(x, y) for x, y in zip(a, b)]
    average = sum(distances) / len(distances)
    maximum = max(distances)
    similarity = max(0.0, 100.0 * (1.0 - average / 64.0))
    return average <= _MAX_AVERAGE_HAMMING and maximum <= _MAX_SINGLE_FRAME_HAMMING, similarity


def _compatible_metadata(
    duration_a: int | None, width_a: int | None, height_a: int | None,
    duration_b: int | None, width_b: int | None, height_b: int | None,
) -> bool:
    if duration_a is not None and duration_b is not None:
        tolerance = max(_DURATION_TOLERANCE_SECONDS, int(max(duration_a, duration_b) * 0.05))
        if abs(duration_a - duration_b) > tolerance:
            return False
    if width_a and height_a and width_b and height_b:
        ratio_a, ratio_b = width_a / height_a, width_b / height_b
        if abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) > _ASPECT_RATIO_TOLERANCE:
            return False
    return True


async def _video_fingerprint_from_file(
    path: str, duration: int | None
) -> str | None:
    try:
        return _fingerprint_key(_video_frame_hashes(path, duration))
    except Exception as exc:
        await log_error("hashban_video_fingerprint", exc)
        return None


async def _upsert_hash(db, *, key: str, file_id: str, media_type: str, user_id: int | None, banned: bool) -> int:
    rows = list((await db.execute(select(MediaHash).where(MediaHash.file_unique_id == key))).scalars().all())
    if not rows:
        db.add(MediaHash(user_id=user_id, file_unique_id=key, file_id=file_id, media_type=media_type, banned=banned))
        return 1
    for row in rows:
        row.file_id, row.media_type = file_id, media_type
        if user_id is not None:
            row.user_id = user_id
        if banned:
            row.banned = True
    return len(rows)


async def _upsert_video_fingerprint(
    db, *, fingerprint: str, file_id: str, user_id: int | None,
    duration: int | None, width: int | None, height: int | None, banned: bool,
) -> int:
    rows = list((await db.execute(select(VideoFingerprint).where(VideoFingerprint.fingerprint == fingerprint))).scalars().all())
    if not rows:
        db.add(VideoFingerprint(
            user_id=user_id, file_id=file_id, fingerprint=fingerprint,
            duration=duration, width=width, height=height, banned=banned,
        ))
        return 1
    for row in rows:
        row.file_id = file_id
        if user_id is not None:
            row.user_id = user_id
        if banned:
            row.banned = True
    return len(rows)


async def _compute_all(bot: Bot, msg: Message) -> tuple[str | None, str | None]:
    entries = media_file_entries(msg)
    if not entries:
        return None, None
    _unique, file_id, media_type = entries[0]
    suffix = ".mp4" if media_type in {"video", "video_note", "animation"} else ".bin"
    path = await _download_to_temp(bot, file_id, suffix)
    if not path:
        return None, None
    try:
        sha = _sha256_path(path)
        duration = _media_metadata(msg)[1]
        perceptual = await _video_fingerprint_from_file(path, duration) if media_type in {"video", "video_note", "animation"} else None
        return sha, perceptual
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def store_message_hashes(msg: Message, bot: Bot, *, banned: bool = False) -> int:
    entries = media_file_entries(msg)
    if not entries:
        return 0
    sha, perceptual = await _compute_all(bot, msg)
    user_id = msg.from_user.id if msg.from_user else None
    _size, duration, width, height = _media_metadata(msg)
    count = 0
    async with SessionLocal() as db:
        for unique, file_id, media_type in entries:
            await _upsert_hash(db, key=unique, file_id=file_id, media_type=media_type, user_id=user_id, banned=banned)
            count += 1
            if sha:
                await _upsert_hash(db, key=sha, file_id=file_id, media_type=media_type, user_id=user_id, banned=banned)
                count += 1
            if perceptual:
                await _upsert_video_fingerprint(
                    db, fingerprint=perceptual, file_id=file_id, user_id=user_id,
                    duration=duration, width=width, height=height, banned=banned,
                )
                count += 1
        await db.commit()
    return count


async def ban_hash_from_message(msg: Message, bot: Bot | None = None) -> int:
    messages = related_media_messages(msg)
    if not messages:
        return 0
    if bot is not None:
        return sum([await store_message_hashes(item, bot, banned=True) for item in messages])
    count = 0
    async with SessionLocal() as db:
        for item in messages:
            for unique, file_id, media_type in media_file_entries(item):
                count += await _upsert_hash(
                    db, key=unique, file_id=file_id, media_type=media_type,
                    user_id=item.from_user.id if item.from_user else None, banned=True,
                )
        await db.commit()
    return count


async def _key_status(key: str | None) -> tuple[bool, bool]:
    if not key:
        return False, False
    values = list((await _db_scalars(select(MediaHash.banned).where(MediaHash.file_unique_id == key))))
    return bool(values), any(values)


async def _db_scalars(statement):
    async with SessionLocal() as db:
        return (await db.execute(statement)).scalars().all()


async def _perceptual_status(
    fingerprint: str | None, duration: int | None, width: int | None, height: int | None
) -> tuple[bool, bool, bool, float | None]:
    if not fingerprint:
        return False, False, False, None
    rows = list(await _db_scalars(select(VideoFingerprint)))
    exact = [row for row in rows if row.fingerprint == fingerprint]
    best_similarity: float | None = None
    matched_banned = False
    for row in rows:
        if not row.banned or not _compatible_metadata(duration, width, height, row.duration, row.width, row.height):
            continue
        matched, similarity = _compare_fingerprints(fingerprint, row.fingerprint)
        if best_similarity is None or similarity > best_similarity:
            best_similarity = similarity
        if matched:
            matched_banned = True
    return bool(exact), any(row.banned for row in exact), matched_banned, best_similarity


async def audit_hashes(bot: Bot, msg: Message) -> list[HashAuditEntry]:
    audits: list[HashAuditEntry] = []
    for media_message in related_media_messages(msg):
        size, duration, width, height = _media_metadata(media_message)
        sha, perceptual = await _compute_all(bot, media_message)
        for unique, _file_id, media_type in media_file_entries(media_message):
            id_present, id_banned = await _key_status(unique)
            sha_present, sha_banned = await _key_status(sha)
            p_present, p_banned, p_match, p_similarity = await _perceptual_status(perceptual, duration, width, height)
            audits.append(HashAuditEntry(
                media_type=media_type, file_unique_id=unique, sha256=sha,
                id_present=id_present, id_banned=id_banned,
                sha_present=sha_present, sha_banned=sha_banned,
                perceptual_hash=perceptual, perceptual_present=p_present,
                perceptual_banned=p_banned, perceptual_match=p_match,
                perceptual_similarity=p_similarity, message_id=media_message.message_id,
                media_group_id=str(media_message.media_group_id) if media_message.media_group_id else None,
                file_size=size, duration=duration, width=width, height=height,
            ))
    return audits


async def ensure_hashes_banned(bot: Bot, msg: Message) -> tuple[int, list[HashAuditEntry]]:
    count = await ban_hash_from_message(msg, bot)
    audits = await audit_hashes(bot, msg)
    if audits and any(
        not item.id_banned
        or (item.sha256 is not None and not item.sha_banned)
        or (item.perceptual_hash is not None and not item.perceptual_banned)
        for item in audits
    ):
        count += await ban_hash_from_message(msg, bot)
        audits = await audit_hashes(bot, msg)
    return count, audits


def format_hash_audit(entries: list[HashAuditEntry], *, title: str = "🔍 AUDIT HASH") -> str:
    if not entries:
        return f"{title}\n\n❌ Aucun média compatible trouvé."
    blocks = [title]
    if len(entries) > 1:
        blocks.append(f"\nAlbum détecté : {len(entries)} médias")
    for index, entry in enumerate(entries, 1):
        metadata = [f"message_id : {entry.message_id}"]
        if entry.media_group_id:
            metadata.append(f"media_group_id : {entry.media_group_id}")
        if entry.file_size is not None:
            metadata.append(f"taille : {entry.file_size} octets")
        if entry.duration is not None:
            metadata.append(f"durée : {entry.duration} s")
        if entry.width and entry.height:
            metadata.append(f"dimensions : {entry.width}×{entry.height}")
        perceptual = ""
        if entry.media_type in {"video", "video_note", "animation"}:
            perceptual = (
                f"\n\nFingerprint vidéo perceptuel :\n{entry.perceptual_hash or '⚠️ CALCUL IMPOSSIBLE'}\n"
                f"Présent exactement en base : {'✅ OUI' if entry.perceptual_present else '❌ NON'}\n"
                f"Blacklist perceptuelle exacte : {'✅ OUI' if entry.perceptual_banned else '❌ NON'}\n"
                f"Correspondance perceptuelle bannie : {'✅ OUI' if entry.perceptual_match else '❌ NON'}\n"
                f"Meilleure similarité : {entry.perceptual_similarity:.1f}%" if entry.perceptual_similarity is not None else
                f"\n\nFingerprint vidéo perceptuel :\n{entry.perceptual_hash or '⚠️ CALCUL IMPOSSIBLE'}\n"
                f"Présent exactement en base : {'✅ OUI' if entry.perceptual_present else '❌ NON'}\n"
                f"Blacklist perceptuelle exacte : {'✅ OUI' if entry.perceptual_banned else '❌ NON'}\n"
                f"Correspondance perceptuelle bannie : {'✅ OUI' if entry.perceptual_match else '❌ NON'}"
            )
        blocks.append(
            "\n────────────\n" + f"Média {index}/{len(entries)} — {entry.media_type}\n" + "\n".join(metadata)
            + f"\n\nfile_unique_id :\n{entry.file_unique_id}\nPrésent en base : {'✅ OUI' if entry.id_present else '❌ NON'}\nBlacklist ID : {'✅ OUI' if entry.id_banned else '❌ NON'}"
            + f"\n\nSHA256 :\n{entry.sha256 or '⚠️ CALCUL IMPOSSIBLE'}\nPrésent en base : {'✅ OUI' if entry.sha_present else '❌ NON'}\nBlacklist SHA : {'✅ OUI' if entry.sha_banned else '❌ NON'}"
            + perceptual
        )
    blocked = sum(1 for e in entries if e.id_banned or e.sha_banned or e.perceptual_match)
    verdict = "🟢 Tous les médias seraient bloqués." if blocked == len(entries) else (
        "🟠 Une partie de l’album serait bloquée." if blocked else "🔴 Aucun média ne serait bloqué."
    )
    blocks.append(
        "\n────────────\nRésumé\n\n"
        f"Médias contrôlés : {len(entries)}\nMédias bloqués par au moins une méthode : {blocked}\n\n"
        f"Verdict : {verdict}\n\n"
        "ℹ️ Pour les vidéos, le fingerprint perceptuel compare le contenu des images et résiste au réencodage Telegram."
    )
    return "".join(blocks)


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
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
    entries = media_file_entries(msg)
    if not entries:
        return HashBanMatch()
    async with SessionLocal() as db:
        for unique, _file_id, media_type in entries:
            found = (await db.execute(select(MediaHash.id).where(MediaHash.file_unique_id == unique, MediaHash.banned.is_(True)).limit(1))).first()
            if found:
                return HashBanMatch(True, "file_unique_id", unique, media_type, 100.0)
    sha, perceptual = await _compute_all(bot, msg)
    if sha:
        async with SessionLocal() as db:
            found = (await db.execute(select(MediaHash.id).where(MediaHash.file_unique_id == sha, MediaHash.banned.is_(True)).limit(1))).first()
            if found:
                return HashBanMatch(True, "sha256", sha, entries[0][2], 100.0)
    if perceptual:
        _size, duration, width, height = _media_metadata(msg)
        rows = list(await _db_scalars(select(VideoFingerprint).where(VideoFingerprint.banned.is_(True))))
        best: tuple[float, VideoFingerprint] | None = None
        for row in rows:
            if not _compatible_metadata(duration, width, height, row.duration, row.width, row.height):
                continue
            matched, similarity = _compare_fingerprints(perceptual, row.fingerprint)
            if matched and (best is None or similarity > best[0]):
                best = (similarity, row)
        if best:
            return HashBanMatch(True, "perceptual_video", best[1].fingerprint, entries[0][2], best[0])
    return HashBanMatch()


async def record_repost_verification(*, match: HashBanMatch, deleted: bool, user_banned: bool, pipeline_stopped: bool, user_id: int | None, chat_id: int, message_id: int) -> None:
    from app.services import settings as st
    total = int(await st.get_value("hashban_reposts_detected", "0") or "0") + 1
    success = deleted and user_banned and pipeline_stopped
    blocked = int(await st.get_value("hashban_reposts_blocked", "0") or "0") + int(success)
    failed = int(await st.get_value("hashban_reposts_failed", "0") or "0") + int(not success)
    values = {
        "hashban_reposts_detected": total, "hashban_reposts_blocked": blocked,
        "hashban_reposts_failed": failed, "hashban_last_at": datetime.utcnow().isoformat(timespec="seconds"),
        "hashban_last_method": match.method, "hashban_last_media_type": match.media_type or "unknown",
        "hashban_last_similarity": "" if match.similarity is None else f"{match.similarity:.1f}",
        "hashban_last_deleted": str(deleted).lower(), "hashban_last_user_banned": str(user_banned).lower(),
        "hashban_last_pipeline_stopped": str(pipeline_stopped).lower(), "hashban_last_success": str(success).lower(),
        "hashban_last_user_id": user_id or "", "hashban_last_chat_id": chat_id, "hashban_last_message_id": message_id,
    }
    for key, value in values.items():
        await st.set_value(key, str(value))
    method_key = f"hashban_detected_{match.method}"
    await st.set_value(method_key, str(int(await st.get_value(method_key, "0") or "0") + 1))


async def banned_hash_count() -> int:
    async with SessionLocal() as db:
        exact = int((await db.execute(select(func.count(MediaHash.id)).where(MediaHash.banned.is_(True)))).scalar() or 0)
        perceptual = int((await db.execute(select(func.count(VideoFingerprint.id)).where(VideoFingerprint.banned.is_(True)))).scalar() or 0)
    return exact + perceptual


async def hashban_health_text() -> str:
    from app.services import settings as st
    detected = int(await st.get_value("hashban_reposts_detected", "0") or "0")
    blocked = int(await st.get_value("hashban_reposts_blocked", "0") or "0")
    failed = int(await st.get_value("hashban_reposts_failed", "0") or "0")
    state = "🟡 EN ATTENTE DE VÉRIFICATION RÉELLE" if detected == 0 else (
        "🔴 ERREUR SUR LE DERNIER REPOST" if failed and await st.get_value("hashban_last_success", "false") != "true" else "🟢 VÉRIFIÉ SUR REPOST RÉEL"
    )
    yn = lambda value: "✅" if value == "true" else "❌"
    return f"""🛡️ HASH-BAN

État : {state}
Empreintes blacklistées : {await banned_hash_count()}
Reposts détectés : {detected}
Reposts bloqués complètement : {blocked}
Échecs de pipeline : {failed}
Détection ID Telegram : {await st.get_value('hashban_detected_file_unique_id', '0')}
Détection SHA256 : {await st.get_value('hashban_detected_sha256', '0')}
Détection perceptuelle vidéo : {await st.get_value('hashban_detected_perceptual_video', '0')}

Dernier repost : {await st.get_value('hashban_last_at', 'jamais')}
Méthode : {await st.get_value('hashban_last_method', 'aucune')}
Similarité : {await st.get_value('hashban_last_similarity', '-')}%
Message supprimé : {yn(await st.get_value('hashban_last_deleted', 'false'))}
Utilisateur banni : {yn(await st.get_value('hashban_last_user_banned', 'false'))}
Pipeline arrêté avant copie VIP : {yn(await st.get_value('hashban_last_pipeline_stopped', 'false'))}"""
