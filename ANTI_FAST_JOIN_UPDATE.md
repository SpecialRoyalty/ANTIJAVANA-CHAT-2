# Anti publication immédiate

Nouveau module configurable depuis **⚙️ Paramètres → 🛡️ Anti publication immédiate**.

- ON/OFF
- délais : 1, 3, 5, 10 ou 15 minutes
- défaut : ON / 5 minutes
- seuls les membres dont l'arrivée a réellement été observée sont concernés
- admins et trusted sont exclus
- si un nouveau membre publie un média pendant la fenêtre : ban immédiat + suppression de tous ses messages/médias suivis
- si l'heure d'arrivée est inconnue : aucune sanction automatique
- le pipeline s'arrête immédiatement : aucune copie VIP après la sanction
- Santé affiche le nombre de bans et le dernier nettoyage

La table `rapid_join_guards` est créée automatiquement au démarrage via SQLAlchemy `create_all`.
