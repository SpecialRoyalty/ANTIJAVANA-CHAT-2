# Telegram Railway Bot V24

Corrections V24 :

- VIP : un utilisateur qui possède déjà Pass Total ou VIP JAVANA ne peut plus acheter le Pass Soirée seul.
- VIP : blocage des doublons Pass Total / VIP / Pass Soirée déjà actifs ou en attente.
- Pass Soirée terminé à 05h : relance PV avec boutons 🎟 Pass Soirée prochaine session / 📦 Pass Total / 💎 VIP JAVANA.
- Justice populaire : nouveaux membres protégés. Éligible seulement après vraie exposition :
  - 0 média après au moins 3 sessions ouvertes connues ; ou
  - aucun média depuis 14 sessions ouvertes connues.
- Justice populaire : admins, trusted et bot protégés.
- Notifications entrée/sortie : supprimées automatiquement.
- Notifications de retrait pendant justice : restent visibles pendant la justice, mais sont maintenant suivies et supprimées au nettoyage/fermeture.
- À chaque ouverture, les membres connus non protégés gagnent +1 session_present pour le calcul d’inactivité.

Rappel Telegram : le bot ne peut nettoyer que les messages qu'il voit depuis son lancement.

## V25 — Justice populaire configurable

Inspection V24 : une limite existait déjà dans `app/services/justice.py` (`MAX_JUSTICE_REMOVALS = 20`), mais elle était codée en dur et le compteur affiché ne montrait que les candidats déjà limités.

V25 corrige ça :
- limite configurée en base avec valeur par défaut `justice_limit=20` ;
- menu `⚙️ Paramètres → ⚖️ Limite justice populaire` ;
- boutons `−10`, `−1`, `+1`, `+10`, presets 10/20/30/50 ;
- preview justice affiche : total éligible, limite, supprimés, reportés ;
- justice auto et manuelle appliquent exactement la même limite ;
- Santé affiche la limite et le nombre de justifiables actuels ;
- rapport admin après justice : éligibles / supprimés / reportés / limite.

## V26 — Justice visible

Correction justice populaire : Telegram ne garantit pas une notification système visible quand un bot retire un membre via l'API. La V26 conserve le retrait réel par ban/unban, mais ajoute une notification publique courte `ANTIJAVANA CHAT removed @pseudo` pour chaque membre retiré.

Ces notifications sont suivies en base avec `kind='justice_removed_notification'` et restent visibles pendant les 5 minutes de justice. Elles sont supprimées automatiquement lors du nettoyage de fermeture.
