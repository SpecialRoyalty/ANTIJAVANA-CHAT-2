# Mise à jour Hash-ban

## /pedo

- Blackliste le `file_unique_id` et le `SHA256`.
- Si la commande répond à un élément d'album encore présent dans le cache du bot, tous les éléments de l'album sont blacklistés.
- Le bannissement des hashes est effectué avant la suppression et le bannissement de l'utilisateur.

## /hashdemande

Répondre à un média avec `/hashdemande` depuis le groupe principal.
Le rapport est envoyé en privé à tous les `ADMIN_IDS` et affiche séparément :

- `file_unique_id` ;
- SHA256 ;
- présence en base ;
- statut blacklist ID ;
- statut blacklist SHA256 ;
- verdict global.

La commande traite également un album complet lorsque ses éléments ont été reçus depuis le dernier démarrage du bot.

## Menu admin Hash ban

Après l'envoi d'un média, le bot le blackliste puis affiche immédiatement les deux empreintes et leur statut réel en base. Les albums sont acceptés ; Telegram les livre élément par élément et le menu reste actif brièvement pour tous les traiter.
