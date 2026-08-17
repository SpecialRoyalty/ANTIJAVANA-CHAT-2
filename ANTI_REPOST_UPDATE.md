# Anti repost ON/OFF

Ajout dans `⚙️ Paramètres → ♻️ Anti repost`.

- OFF par défaut pour ne pas modifier le comportement existant après déploiement.
- ON : un média déjà enregistré dans le groupe principal ne peut plus être renvoyé.
- Détection exacte via `file_unique_id` puis SHA256.
- Hash-ban testé avant anti-repost : un média blacklisté conserve donc la sanction hash-ban (suppression + ban).
- Anti-repost simple : suppression du message ou de l'album connu, avertissement temporaire, aucune sanction de ban.
- Admins et trusted sont exempts.
- Le contrôle est exécuté avant `record_media()`, ce qui évite qu'un premier envoi se détecte lui-même comme repost.
- Santé du bot affiche statut, nombre de reposts bloqués et dernier blocage.
