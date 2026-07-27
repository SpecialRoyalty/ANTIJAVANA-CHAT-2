# Correction stabilité Telegram

Cette version ajoute :

- un client Telegram global avec retries sur 502, timeouts et erreurs réseau ;
- le respect automatique de `RetryAfter` ;
- une limite de requêtes concurrentes ;
- un léger espacement entre les appels API ;
- des reprises progressives avec jitter ;
- des jobs APScheduler décalés pour éviter leur lancement simultané ;
- `coalesce=True` et `max_instances=1` pour éviter l'empilement des jobs ;
- une gestion du message d'état qui ne crée plus de doublon après un timeout.

Les erreurs Telegram persistantes peuvent encore apparaître dans les logs si
l'API est réellement indisponible, mais elles sont réessayées et ne doivent plus
provoquer un pic d'appels ou un redémarrage immédiat du conteneur.
