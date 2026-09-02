# Décision — équivalent API et attribution Codex

Décision vérifiée le 1er septembre 2026 : `codex-usage.py` ne propose pas
`--api-equivalent` et n'affiche aucun montant.

## Échantillon et critère

Le relevé local, limité aux métadonnées et compteurs, a été obtenu avec :

```bash
python3 codex-usage.py --days 3650 --json --no-git
```

| Valeur `model` observée | Sessions | Avec intervalles exploitables |
|---|---:|---:|
| absente | 213 | 3 |
| `codex-auto-review` | 58 | 46 |
| `gpt-5.6-luna` | 17 | 16 |
| `gpt-5.6-terra` | 62 | 59 |

La documentation officielle décrit bien les prix API des identifiants
`gpt-5.6-luna` et `gpt-5.6-terra`, y compris l'entrée servie depuis le cache et
la règle applicable aux écritures de cache. Elle ne fournit pas ici de mapping
API explicite, stable et par intervalle pour l'étiquette `codex-auto-review`.
Une session peut aussi ne porter aucun modèle. Les conditions de l'option ne
sont donc pas réunies pour **tous** les tokens observés.

Sources officielles, consultées le 1er septembre 2026 :

- [GPT-5.6 Luna — OpenAI API](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [GPT-5.6 Terra — OpenAI API](https://developers.openai.com/api/docs/models/gpt-5.6-terra)

Les tarifs API ne seraient de toute façon pas une facture Codex Desktop ou
Plus. Ils restent une piste de simulation future, conditionnée à un identifiant
API explicite pour chaque intervalle et à la prise en compte documentée de toute
tarification par appel d'outil.

## Attribution des intervalles

La fixture `long-with-tools.jsonl` contient deux appels d'outils et deux deltas
de compteurs cumulés. Le premier appel se situe entre le premier et le deuxième
snapshot ; le second entre le deuxième et le troisième. Le journal ne relie pas
un delta à un appel, à un sous-agent ou à une réponse. La relation temporelle
ne suffit pas à inférer une causalité, et le lecteur `codex_log.py` n'en infère
aucune : il conserve deux séries séparées.

**Révision du 2 septembre 2026.** L'attribution par outil est ajoutée, dans une
couche distincte du lecteur (`codex_grade.py`), pour aligner la page d'une
session Codex sur celle de Claude Code. Elle n'est pas présentée comme une
mesure : les tokens observés d'un intervalle sont répartis **à parts égales**
entre les appels horodatés à l'intérieur, et un intervalle sans appel occupe sa
propre ligne. La règle est énoncée sur la page elle-même, elle est reproductible,
et un test vérifie qu'elle conserve exactement le total de chaque intervalle et
donc celui de la session. Le lecteur reste inchangé : il n'attache toujours aucun
outil à un delta.

La conclusion sur les montants ne change pas : aucune ligne ne porte de prix.

## Condition de réouverture

Ajouter `--api-equivalent` seulement lorsque le format fournit, pour chaque
intervalle facturable, un modèle API officiellement tarifé et sans ambiguïté.
Remplacer la répartition à parts égales par une attribution mesurée seulement
lorsqu'un lien explicite dans le journal relie un delta à un appel — la règle
actuelle devra alors être retirée, pas complétée.
