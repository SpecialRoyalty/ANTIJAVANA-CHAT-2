# Correctif Hash-ban

## Cause corrigée

Les anciennes versions pouvaient créer plusieurs lignes `media_hashes` avec la même clé.
L'utilisation de `scalar_one_or_none()` provoquait alors une exception pendant `/pedo`.
L'utilisateur pouvait être banni, tandis que les empreintes restaient avec `banned = false`.

## Modifications

- `/pedo` traite le média ciblé ou l'album complet encore présent dans le cache.
- Chaque média est blacklisté par `file_unique_id` et par SHA256.
- Toutes les anciennes lignes dupliquées sont mises à `banned = true`.
- Une vérification immédiate est effectuée après l'écriture.
- L'admin reçoit un rapport privé de confirmation après `/pedo`.
- Le menu Hash-ban utilise la même écriture suivie d'une vérification.
- Les recherches de repost ne plantent plus en présence de doublons.
- `/hashdemande` affiche aussi `message_id`, `media_group_id`, taille, durée et dimensions.
- Le pipeline s'arrête avant la copie VIP lorsqu'une empreinte bannie est détectée.

## Limite normale du SHA256

Le SHA256 reconnaît un fichier binaire identique. Si Telegram réencode une vidéo lors d'un nouvel envoi depuis la galerie, le SHA256 et le `file_unique_id` peuvent changer même si la vidéo paraît identique.
