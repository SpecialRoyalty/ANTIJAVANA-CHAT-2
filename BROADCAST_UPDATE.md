# Mise à jour Broadcast

Fonctions ajoutées au panneau admin :

- Broadcast groupe principal : texte, photo, photo + légende.
- Broadcast privé : mêmes formats, envoyé aux utilisateurs ayant lancé `/start` en privé.

La table `private_subscribers` est créée automatiquement au démarrage par SQLAlchemy.
Les utilisateurs ayant lancé `/start` avant l'installation de cette version ne peuvent pas être identifiés rétroactivement par Telegram ; ils seront enregistrés à leur prochain `/start`.
