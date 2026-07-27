# Mise à jour — mots complets

Les trois types de règles utilisent désormais une comparaison par mots complets :

- `nameban`
- `ban`
- `forbidden`

Exemple avec la règle `cp` :

- `cp`, `je cp`, `cp-75`, `je_cp_quoi` : correspondance
- `jecpquoi`, `macpro`, `scpfoundation` : aucune correspondance

La normalisation Unicode NFKC et `casefold()` rendent la comparaison insensible à la casse et aux variantes Unicode courantes. Les règles composées de plusieurs mots sont recherchées comme une séquence complète de mots consécutifs.
