# Détection vidéo perceptuelle

Le Hash-ban utilise maintenant trois niveaux :

1. `file_unique_id` Telegram ;
2. SHA256 binaire ;
3. fingerprint perceptuel calculé sur plusieurs images extraites de la vidéo.

Le troisième niveau reconnaît une même vidéo réencodée par Telegram, même lorsque son ID et son SHA256 changent.

Une nouvelle table `video_fingerprints` est créée automatiquement au démarrage par SQLAlchemy.

Le rapport `/hashdemande` affiche désormais la correspondance perceptuelle et le pourcentage de similarité.
