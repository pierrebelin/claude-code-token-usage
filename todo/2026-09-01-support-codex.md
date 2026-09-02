# Plan d'implémentation — prise en charge de Codex

## Objectif

Ajouter une analyse locale des sessions Codex Desktop, sans modifier le comportement
actuel de l'outil Claude Code. Le résultat initial doit permettre de comprendre, par
projet, par jour et par session :

- les tokens d'entrée, de sortie et servis depuis le cache ;
- la consommation visible des quotas Codex (fenêtres courte et hebdomadaire) ;
- les sessions et les projets les plus consommateurs ;
- les appels d'outils associés à chaque session ;
- le résultat Git des sessions, lorsque le dépôt est accessible localement.

## Périmètre et décisions

- Créer un lecteur Codex séparé (`codex-usage.py`) durant cette première livraison.
  `cc-usage.py` et son installation comme skill Claude restent inchangés.
- Lire uniquement les journaux locaux `~/.codex/sessions/**/rollout-*.jsonl` et,
  si utile, les sessions archivées. Aucun transcript ne sort de la machine.
- Afficher les **tokens et quotas** comme valeurs observées. Ne pas présenter une
  estimation API comme une dépense réellement facturée par Codex Desktop/Plus.
- Garder l'interface sans JavaScript obligatoire et réemployer le style du dashboard
  existant quand les données s'y prêtent.
- Reporter l'unification Claude + Codex, la notation A–F et les recommandations
  d'optimisation à une livraison ultérieure : elles dépendent d'une attribution
  fiable qui n'est pas encore prouvée par le format Codex.

> **Révision du 2 septembre 2026.** Cette dernière décision est levée. La page
> d'une session Codex est alignée sur celle de Claude Code, notation A–F
> comprise. Le format n'a pas changé : ce qui change est que l'attribution est
> désormais **énoncée comme telle** au lieu d'être omise. Elle vit dans
> `codex_grade.py`, au-dessus du lecteur, avec une règle unique — les tokens d'un
> intervalle répartis à parts égales entre ses appels — un test de conservation
> des totaux, et une cible tronquée qui ne compte jamais comme répétition. Les
> bandes et les poids sont partagés avec le lecteur Claude Code dans
> `session_grade.py` ; seule l'unité diffère. Voir
> `docs/codex-api-equivalent-decision.md`.

## Étape 1 — Consolider le contrat des journaux Codex

1. Échantillonner des sessions locales courtes, longues, archivées et avec appels
   d'outils, sans afficher leur contenu dans les sorties de diagnostic.
2. Documenter les événements et champs réellement stables : `session_meta`,
   `event_msg/token_count`, `response_item`, `cwd`, date, modèle, appels d'outils,
   compteurs cumulés et quotas.
3. Définir la règle de lecture : la différence entre deux compteurs cumulés fournit
   les tokens d'un intervalle ; ignorer les compteurs incomplets, décroissants ou
   dupliqués en les signalant.
4. Ajouter des fixtures JSONL anonymisées couvrant ces cas, plus un cas de journal
   partiellement écrit.

**Terminé lorsque** le lecteur sait extraire les métadonnées et des deltas de tokens
à partir des fixtures, avec des totaux attendus vérifiés par test.

## Étape 2 — Construire le noyau de lecture Codex

1. Créer `codex-usage.py` avec les structures de données propres à Codex : session,
   intervalle de consommation, outil et quota observé.
2. Parcourir les répertoires de sessions par date et filtrer par `--days` / `--since`
   avant d'analyser les lignes utiles.
3. Grouper par racine Git, avec repli sur le `cwd`, en reprenant les règles de
   worktrees déjà éprouvées dans `cc-usage.py`.
4. Exposer un format JSON stable, afin que le rendu terminal et le dashboard reposent
   sur la même charge utile.

**Terminé lorsque** une commande locale affiche des totaux cohérents par projet,
jour et session, et que les sessions sans compteurs exploitables restent visibles avec
un statut explicite plutôt qu'un faux zéro.

## Étape 3 — Livrer le MVP terminal

1. Ajouter les commandes : `--days`, `--since`, `--project`, `--by`, `--top`,
   `--sessions`, `--daily`, `--tools`, `--json` et `--no-git`.
2. Afficher séparément les entrées non cachées, entrées en cache, sorties et tokens
   de raisonnement lorsqu'ils sont disponibles.
3. Afficher le dernier état de quota observé pour chaque session, avec sa date de
   mesure ; ne pas l'agréger comme une dépense ni l'interpréter comme une limite
   garantie.
4. Réutiliser la corrélation Git actuelle pour indiquer l'issue des sessions, en
   conservant les mêmes garde-fous : nombre de dépôts borné et `--no-git` sans appel
   à Git.

**Terminé lorsque** les tableaux terminal et JSON donnent les mêmes totaux sur le
même échantillon et que l'exécution ne nécessite ni réseau ni clé API.

## Étape 4 — Ajouter le dashboard Codex

1. Réemployer le gabarit HTML et les composants visuels compatibles : compteurs,
   tableau projets, courbe quotidienne et liste de sessions.
2. Créer une page de détail par session : métadonnées, répartition des tokens,
   appels d'outils, chronologie des intervalles et dernier quota observé.
3. Conserver les filtres et tris dans l'URL, comme dans le dashboard Claude.
4. Proposer `--dashboard` et `--serve`, sur loopback uniquement.

**Terminé lorsque** le dashboard généré est autonome, une session peut être ouverte
depuis la liste et aucun contenu de prompt ou de réponse n'est rendu par défaut.

## Étape 5 — Coûts et attribution : décider sur preuves

1. Vérifier si tous les modèles présents dans les journaux ont une tarification API
   officielle et un mapping non ambigu.
2. Si oui, ajouter une option explicite `--api-equivalent` : elle calcule une
   simulation documentée à partir des tokens observés et affiche son modèle, ses
   tarifs et la date de récupération.
3. Si non, garder les montants absents : une valeur inventée serait moins utile que
   les tokens et les quotas exacts.
4. Mesurer sur des fixtures si un delta peut être rattaché sans ambiguïté à un outil,
   un sous-agent ou une réponse. Ajouter seulement les catégories dont la règle est
   reproductible ; sinon afficher la chronologie sans causalité supposée.

**Terminé lorsque** chaque montant potentiel porte la mention « équivalent API » et
chaque attribution dispose d'un test de conservation des totaux.

## Étape 6 — Documentation, validation et livraison

1. Mettre à jour le README avec deux parcours séparés : Claude Code et Codex, leurs
   emplacements de journaux, leurs limites de mesure et des exemples de commandes.
2. Documenter précisément la différence entre coût API, quota Codex et tokens
   enregistrés localement.
3. Exécuter les tests de parsing et de rendu, `python3 codex-usage.py --help`, une
   analyse locale sans réseau, une génération dashboard et `git diff --check`.
4. Vérifier manuellement une page d'accueil et une page session : filtres, totaux,
   absence de fuite de transcript, lisibilité mobile et absence de débordement
   horizontal.

**Terminé lorsque** les résultats locaux concordent avec les compteurs de leurs
journaux source, la documentation ne promet pas de coût Desktop exact et l'outil
Claude existant continue de fonctionner sans régression.

## Évolutions après le MVP

- Option de rapport commun Claude + Codex, uniquement après validation des schémas
  et des unités ;
- ~~recommandations Codex limitées à des signaux mesurables et dépassant un seuil
  documenté~~ — livré le 2 septembre 2026 : les leads d'une session Codex
  apparaissent à partir d'un dixième de la session, ou du plancher de 50 000
  tokens seul lorsque le motif est sans ambiguïté ;
- intégration d'une vraie dépense organisationnelle seulement si une source de
  facturation autorisée est disponible, avec séparation stricte des données locales.
