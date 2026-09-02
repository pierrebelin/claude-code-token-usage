# Contrat observé des journaux Codex

Ce document décrit le contrat de lecture local consolidé le 1er septembre 2026.
Il ne décrit pas une API publique : le lecteur doit ignorer les champs inconnus
et traiter les champs optionnels comme absents, pas comme zéro.

## Échantillon local, sans contenu de transcript

L'échantillon a couvert 350 fichiers `rollout-*.jsonl` sous
`~/.codex/sessions/` et `~/.codex/archived_sessions/`, dont des journaux courts,
longs, archivés et avec appels d'outils. Le diagnostic n'a inspecté que les noms
de champs, leurs types et leurs compteurs : aucun prompt, réponse ni résultat
d'outil n'a été affiché ou copié.

Chaque ligne est un objet JSON avec `type`, `timestamp`, `payload` et, dans une
partie des journaux, `ordinal`. Un journal peut répéter `session_meta`; le lecteur
retient la première identité complète et ne doit pas dédupliquer ou exposer le
contenu conversationnel associé.

| Événement | Champs lus | Usage |
|---|---|---|
| `session_meta` | `payload.session_id`, `cwd`, `timestamp`, `model_provider` | identité, répertoire et date de début |
| `turn_context` | `payload.model`, `cwd` | modèle et répertoire observés pour un tour |
| `event_msg` / `thread_settings_applied` | `thread_settings.model`, `model_provider_id`, `cwd` | modèle et fournisseur les plus récents |
| `event_msg` / `token_count` | `info.last_token_usage`, `info.total_token_usage`, `rate_limits` | tokens par intervalle, cumulés et quotas observés |
| `response_item` / `function_call` ou `custom_tool_call` | `name`, `call_id` ou `id`, `input` ou `arguments` | outil réellement appelé, occurrence, et une description bornée de sa cible |

Le modèle est pris dans le dernier événement porteur d'un modèle (`turn_context`
ou configuration de thread). `session_meta` ne porte pas toujours ce champ. Les
valeurs de `last_token_usage` sont reconnues comme signal de format mais le calcul
s'appuie sur `total_token_usage`, seul compteur cumulatif de ce contrat.

## Tokens et quotas

Les 3 982 snapshots `token_count` de l'échantillon portaient les six entiers
suivants dans `info.total_token_usage` :

```text
input_tokens
cached_input_tokens
cache_write_input_tokens
output_tokens
reasoning_output_tokens
total_tokens
```

`total_tokens` est lu comme son propre compteur observé : il n'est pas recalculé à
partir des autres catégories. Les catégories cache et raisonnement restent donc
visibles séparément, sans supposer leur inclusion dans ce total.

`rate_limits` est parfois `null`. Lorsqu'il est présent, `primary` et `secondary`
sont deux fenêtres indépendantes et optionnelles avec `window_minutes`,
`used_percent` et `resets_at` (Unix, en secondes). `plan_type` est une étiquette
observée. Aucune de ces valeurs n'est une dépense API, une facture Desktop/Plus ni
une limite contractuelle inférée.

## Appels d'outils : ce qui est lu, ce qui est retenu

Le harness enveloppe chaque appel dans un petit programme. Le journal enregistre
donc presque tous les appels sous un seul `name`, `exec`, et l'outil réel apparaît
dans le corps du programme, en `input` (chaîne) ou `arguments` (objet JSON) :

```text
const r = await tools.exec_command({"cmd": ["rg", "-n", "pattern", "src"]});
```

Le lecteur en tire deux valeurs, et ne conserve rien d'autre du texte source :

- **`tool`** — le nom imbriqué capté par `tools.<nom>(`, ici `exec_command`. Repli
  sur le `name` du journal quand aucun appel imbriqué n'apparaît (~5 % des appels
  sur l'échantillon local). C'est un identifiant de l'API du harness, jamais une
  donnée utilisateur.
- **`detail`** — **un** champ des arguments, le premier trouvé parmi `cmd`,
  `command`, `search_query`, `query`, `path`, `file_path`, `url`, `pattern`,
  `patch`, `prompt`, `code`, `input`, `chars` — replié sur une ligne et **coupé à
  40 caractères** (`TOOL_DETAIL_WIDTH`). Un chemin est réduit à son nom de
  fichier. Un objet d'arguments illisible ne produit aucune description plutôt
  qu'une approximation.

Ce `detail` **est du contenu** : un fragment de ligne de commande, de chemin ou de
prompt. Il est visible dans le dashboard et donc dans tout `report.html` transmis.
C'est un choix assumé, aligné sur le lecteur Claude Code qui tronque de la même
façon (`describe_tool`, 40 caractères). Restent hors de portée du lecteur, sans
exception :

- les sorties d'outil (`function_call_output`, `custom_tool_call_output`) ;
- les prompts et réponses du modèle (`message`) ;
- les blocs `reasoning` ;
- le texte source des arguments au-delà des 40 caractères retenus.

## Règle de calcul des intervalles

Le compteur est cumulatif **depuis zéro pour le journal**. Le premier snapshot
complet est donc lui-même un intervalle, courant depuis le `timestamp` de
`session_meta` : il porte la consommation de la première requête, pas une
référence à retrancher. Pour chaque snapshot complet ultérieur, le lecteur
calcule champ par champ :

```text
delta = total_courant - total_précédent_valide
```

La somme des intervalles vaut ainsi le dernier compteur cumulatif du journal,
sans perte.

**Correction du 2 septembre 2026.** Le premier snapshot servait auparavant de
simple baseline et sa valeur disparaissait du total. Vérification sur les 354
journaux locaux : les sessions portant `parent_thread_id` (61) ou
`forked_from_id` (3) ont un premier snapshot du même ordre que les sessions
neuves — médiane 18 640, maximum 31 413 — donc un thread repris **ouvre son
propre compte** et le lire ne double aucune consommation. La correction rend
10 392 378 tokens au total observé et fait sortir 197 sessions du statut
`no_exploitable_intervals`, où elles s'affichaient comme illisibles alors
qu'elles portaient un compteur valide.

Un snapshot incomplet, identique au précédent ou décroissant est ignoré, assorti
d'un avertissement et ne remplace pas la référence courante. Cette règle évite
d'inventer une consommation lors d'une écriture en cours ou d'un changement de
format. Une ligne JSON finale partiellement écrite est également signalée et les
lignes déjà valides restent exploitables.

Un premier snapshot entièrement nul ne produit aucun intervalle : c'est un
compteur écrit avant toute consommation, et la session reste en
`no_exploitable_intervals` — une lecture, pas un chiffre manquant.

Les fixtures anonymisées dans `tests/fixtures/codex/` couvrent un journal court,
un long journal avec outils et deux quotas, un journal archivé, une session sans
compteur, une session à compteur nul seul, les trois compteurs invalides et une
dernière ligne tronquée. Elles ne
contiennent ni prompt réel, ni texte de réponse, ni résultat d'outil ; leurs
arguments d'appel sont inventés pour couvrir l'enveloppe du harness, un objet
JSON simple et un programme sans appel imbriqué.

## Ce que le contrat ne porte pas : la note A-F

La note d'une session Codex n'appartient pas à ce contrat. Elle est calculée par
`codex_grade.py`, au-dessus du lecteur, à partir des seuls éléments décrits ici :
les compteurs par intervalle, l'horodatage des appels et la cible bornée de
chaque appel. Les bandes, les poids par catégorie et le fondu d'entrée sont ceux
du lecteur Claude Code, partagés dans `session_grade.py` ; seule l'unité change,
des tokens observés au lieu de dollars.

Trois limites du format se lisent directement dans les règles retenues :

- le journal ne chiffre aucun appel, donc les tokens d'un intervalle sont
  répartis **à parts égales** entre les appels qu'il contient. C'est une
  attribution énoncée comme telle, jamais une mesure ;
- une cible tronquée à 40 caractères ne prouve pas une répétition : huit URL
  différentes partagent le début d'une même ligne `curl`. Seule une cible
  conservée entière compte comme cible répétée ;
- le journal ne décrit pas la composition d'une requête. Le volume de réponses
  rejouées est donc le cumul des sorties déjà produites, plafonné à l'entrée
  réellement observée sur l'étape — un plancher, pas une décomposition.

Validation locale :

```bash
python3 -m unittest discover -s tests
```
