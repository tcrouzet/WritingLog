# Cahier des charges — Dashboard de suivi du processus d'écriture

**Auteur** : Thierry Crouzet
**Destinataire** : Codex (agent de génération de code)
**Date** : 2026-09-09

---

## 1. Contexte

Le vault Obsidian de l'auteur est synchronisé automatiquement sur GitHub, avec un commit environ toutes les 15 minutes (l'écart exact n'a pas d'importance, seul l'ordre chronologique des commits compte).

L'historique Git constitue donc un journal implicite et granulaire de l'activité d'écriture : à chaque commit, on peut mesurer combien de signes ont été ajoutés ou retirés dans chaque fichier, et donc dans chaque projet.

## 1bis. Dépôts concernés

Deux dépôts GitHub distincts sont impliqués :

- **`tcrouzet/text`** (privé) : contient le vault Obsidian synchronisé, c'est la source de vérité pour l'analyse Git (historique des commits, contenu des fichiers `.md`).
- **`tcrouzet/WritingLog`** (public, [github.com/tcrouzet/WritingLog](https://github.com/tcrouzet/WritingLog)) : héberge le dashboard statique publié via GitHub Pages. Ce dépôt ne contient jamais les textes bruts, uniquement le code du dashboard (HTML/CSS/JS) et les données déjà agrégées (JSON de statistiques, sans contenu littéraire).

Le repo `text` étant privé, la publication d'un dashboard sur GitHub Pages nécessite un pont explicite entre les deux dépôts — voir §10 pour le mécanisme retenu.

## 2. Objectif

Construire un outil Python qui :

1. Analyse l'historique Git du vault.
2. En déduit des métriques de production d'écriture par projet.
3. Génère un dashboard web **statique** (HTML/CSS/JS), publié via **GitHub Pages** depuis le même dépôt.
4. Se met à jour automatiquement via **GitHub Actions**.

L'enjeu principal n'est pas de compter des octets, mais de **distinguer l'écriture réelle** (frappe progressive) **d'un import massif** (copier-coller d'un texte déjà écrit ailleurs, remaniement de plan, etc.), qui ne doit pas gonfler artificiellement les statistiques de production ni le temps d'écriture — mais doit tout de même apparaître dans l'évolution de la taille des projets.

## 3. Définitions

### 3.1 Projet
Un projet = un dossier de premier niveau du vault (ex. `/RomanX`, `/EssaiY`). Tous les fichiers `.md` sous ce dossier (récursivement) appartiennent au projet.

### 3.2 Fichiers suivis
Seuls les fichiers `.md` sont analysés. Tout autre format (images, PDF, `.canvas`, etc.) est ignoré.

### 3.3 Dossiers exclus
Un fichier de config liste des dossiers à ignorer complètement (ex. `templates/`, `.obsidian/`, `journal/` si souhaité). Ces dossiers ne sont ni des projets, ni comptés dans aucune métrique.

### 3.4 Signe
Un signe = un caractère du contenu markdown brut du fichier (`len(texte)`). Pas de traitement particulier du markdown (pas de strip des balises) sauf décision contraire ultérieure — on reste sur une mesure simple et reproductible.

### 3.5 Delta
Pour un fichier donné, entre deux commits consécutifs qui le modifient, le delta = `taille_apres - taille_avant` (en signes). Peut être négatif (suppression/réécriture).

### 3.6 Import vs écriture réelle
Pour chaque delta positif d'un fichier entre deux commits :

- On calcule un **seuil normalisé** selon l'écart de temps réel entre les deux commits :
  `seuil = 1000 × (écart_minutes / 15)`
  (1000 signes / 15 min, proportionnel si l'écart est plus grand ou plus petit que 15 min — par exemple pas de commit pendant 1h → seuil ≈ 4000).
- Si `delta > seuil` → la transition est classée **IMPORT**.
- Si `delta <= seuil` → la transition est classée **ÉCRITURE RÉELLE**.
- Les deltas négatifs (suppression/réécriture) sont toujours classés comme **édition normale** (comptent dans le temps de session, mais pas dans le total de "signes produits").

Le seuil de base (1000 signes/15 min) doit être un paramètre de configuration modifiable, pas une valeur codée en dur.

### 3.7 Session d'écriture
Les commits d'un même projet sont regroupés en sessions : deux commits consécutifs appartiennent à la même session si l'écart entre eux est ≤ `SESSION_TIMEOUT` (paramètre de config, valeur par défaut : 45 minutes). Sinon, une nouvelle session démarre.

Durée d'une session = `timestamp(dernier commit) - timestamp(premier commit) + un forfait forfaitaire` (ex. + 10 min, pour couvrir le temps d'écriture après le dernier commit avant l'arrêt réel — paramètre configurable, défaut 10 min).

**Le temps passé sur un projet = somme des durées des sessions de ce projet.**

Les sessions ne sont pas invalidées par la présence d'un import dans leur historique — mais un import ne doit pas, à lui seul, créer une session artificiellement longue si le reste de la session est inactif ; à valider empiriquement une fois les premières données observées (voir §12 points ouverts).

## 4. Métriques à produire

Pour chaque projet, et globalement :

1. **Temps passé** : total, par jour, par semaine, par mois (basé sur les sessions, §3.7).
2. **Signes produits (écriture réelle uniquement)** : total, par jour, par semaine, par mois, par projet.
3. **Évolution de la taille du projet** : série temporelle de la taille totale en signes (tous fichiers .md du projet cumulés), incluant les imports — utile pour voir la croissance réelle du texte même quand il vient d'un import.
4. **Vue d'ensemble** : totaux tous projets confondus, projet le plus actif récemment, etc.

## 5. Pipeline d'analyse (script Python)

### 5.1 Étapes

1. Lire la configuration (`config.yaml`).
2. Lister chronologiquement tous les commits du dépôt affectant des fichiers `.md` hors dossiers exclus (`git log --all --reverse --numstat` ou équivalent via `subprocess` ou `GitPython`).
3. Pour chaque commit, pour chaque fichier `.md` modifié :
   - Récupérer la taille en signes du fichier après le commit (`git show <hash>:<chemin>`).
   - Calculer le delta par rapport à la taille connue précédente de ce fichier.
   - Classer le delta (import / réel / édition négative) selon §3.6.
   - Attribuer le fichier à son projet (dossier racine), en respectant les exclusions.
4. Construire, par projet, la liste chronologique des transitions (timestamp, delta, classification).
5. Regrouper en sessions (§3.7) et calculer le temps passé.
6. Agréger les signes réels produits par jour / semaine / mois / projet.
7. Construire la série d'évolution de taille par projet (taille cumulée à chaque commit, ou échantillonnée par jour pour alléger le dashboard).
8. Exporter les résultats en JSON dans `docs/data/`.

### 5.2 Traitement incrémental (performance)

Pour éviter de rejouer tout l'historique Git à chaque exécution (le vault grossira avec les années), le script doit maintenir un **état persistant** (`docs/data/state.json` ou similaire, committé dans le dépôt) contenant :
- Le hash du dernier commit traité.
- Pour chaque fichier suivi, sa dernière taille connue (en signes).
- Les agrégats déjà calculés (temps par session, sommes par jour/semaine/mois, séries de taille).

À chaque exécution : ne traiter que les commits postérieurs au dernier hash connu, et mettre à jour l'état + les agrégats en conséquence. Une commande `--full-rebuild` doit permettre de forcer une réanalyse complète depuis le premier commit (utile en cas de changement de config ou de bug corrigé).

### 5.3 Cas limites à gérer

- Renommage de fichier (`git log --follow` ou détection de rename dans `--numstat`) : continuer l'historique de taille du fichier sous son nouveau nom/chemin.
- Fichier déplacé d'un projet à un autre : à partir du déplacement, les signes doivent être comptés dans le nouveau projet (pas de rétroactivité).
- Fichier supprimé : sa taille sort du total de taille du projet à partir du commit de suppression ; ne compte pas comme un "import négatif" anormal, reste une édition normale.
- Premier commit d'un fichier (création) : la taille initiale n'est pas un delta "depuis rien" à classer import/réel de la même façon — un fichier nouvellement créé avec beaucoup de contenu dès le premier commit doit être traité comme un cas d'import potentiel (même règle de seuil, delta = taille initiale, écart de temps = depuis le commit précédent du même projet ou une valeur par défaut si c'est le premier commit du projet).
- Vault avec plusieurs années d'historique : le script doit rester exécutable en quelques minutes max sur CI (voir §5.2).

## 6. Architecture technique attendue

L'architecture est répartie sur les deux dépôts (§1bis). Le repo privé porte le pipeline d'analyse et pousse ses résultats dans le repo public, qui porte le dashboard lui-même.

### 6.1 Dépôt privé `tcrouzet/text`

```
/
├── config.yaml                     # configuration (voir §7.1)
├── projet.yml                      # métadonnées des projets (voir §7.2)
├── scripts/
│   ├── analyze_vault.py            # script principal (pipeline §5)
│   ├── git_utils.py                # wrappers autour des commandes git
│   ├── serve_local.py              # prévisualisation locale (§10bis)
│   └── requirements.txt
├── dashboard_output/                # généré localement par analyze_vault.py (voir §7.1, local_output_dir)
│   └── data/
│       ├── state.json              # état incrémental (voir §5.2)
│       ├── overview.json
│       ├── projects.json
│       ├── daily.json
│       ├── weekly.json
│       ├── monthly.json
│       └── size_evolution.json
└── .github/
    └── workflows/
        └── update-dashboard.yml    # analyse + push cross-repo vers WritingLog (voir §10)
```

`dashboard_output/` est un dossier de travail local (généré, à ajouter au `.gitignore` du repo privé — il n'a pas besoin d'être versionné dans `text`, seule sa copie poussée vers `WritingLog` compte).

### 6.2 Dépôt public `tcrouzet/WritingLog`

```
/
├── index.html                      # dashboard
├── style.css
├── dashboard.js                    # logique Chart.js, fetch des JSON
└── data/
    ├── state.json                  # copie de l'état incrémental (utile pour le mode --full-rebuild distant, voir §10)
    ├── overview.json
    ├── projects.json
    ├── daily.json
    ├── weekly.json
    ├── monthly.json
    └── size_evolution.json
```

GitHub Pages sert ce dépôt directement depuis sa branche principale (racine, pas de sous-dossier `/docs` nécessaire puisque tout le repo est dédié au dashboard).

Le code du dashboard (`index.html`, `style.css`, `dashboard.js`) est écrit une fois par Codex directement dans `WritingLog` et n'est ensuite modifié qu'en cas d'évolution du dashboard lui-même ; seul le dossier `data/` y est réécrit automatiquement à chaque exécution du workflow.

## 7. Fichiers de configuration

### 7.1 `config.yaml` — paramètres techniques

Doit permettre de définir, sans toucher au code :

```yaml
vault_path: "."                     # racine du dépôt/vault
excluded_folders:
  - "templates"
  - ".obsidian"
  - "journal"                       # à ajuster selon besoin réel

file_extensions: [".md"]

import_detection:
  threshold_chars: 1000             # seuil de base
  threshold_window_minutes: 15      # fenêtre de référence du seuil

session:
  timeout_minutes: 45               # écart max entre deux commits d'une même session
  trailing_buffer_minutes: 10       # forfait ajouté à la fin de chaque session

local_output_dir: "dashboard_output"   # dossier de travail local (voir §6.1)

publish:
  target_repo: "tcrouzet/WritingLog"    # dépôt public qui héberge le dashboard
  target_branch: "main"
  data_path: "data"                     # sous-dossier de WritingLog recevant les JSON
```

### 7.2 `projet.yml` — métadonnées des projets

Fichier séparé de `config.yaml`, dédié à la description humaine des projets (le dossier racine sert de clé technique, mais l'auteur veut pouvoir lui associer un titre lisible et, plus tard, d'autres informations sans devoir toucher au code).

Structure : un dictionnaire, une clé par dossier racine (= identifiant technique du projet), chaque valeur est elle-même un dictionnaire de champs libres. Seul `title` est utilisé pour l'instant par le dashboard (affiché à la place du nom brut du dossier), mais le format doit rester ouvert à l'ajout de champs futurs sans aucune modification du script Python — le pipeline doit lire ce fichier de façon générique (charger tout le dictionnaire de métadonnées et le transmettre tel quel dans `projects.json`, sans liste blanche de champs autorisés en dur dans le code).

```yaml
# projet.yml
dossier_racine1:
  title: "Nom du projet"
  # champs additionnels libres, ajoutés librement au fil du temps, ex. :
  # genre: "roman"
  # objectif_signes: 500000
  # statut: "en cours"
  # date_debut: "2025-01-10"

dossier_racine2:
  title: "Autre projet"
```

Règles :
- Un dossier racine présent dans le vault mais absent de `projet.yml` doit tout de même apparaître dans le dashboard (le script utilise alors le nom du dossier tel quel comme titre par défaut), pour ne jamais bloquer le pipeline si l'auteur oublie de déclarer un nouveau projet.
- Un dossier racine listé dans `excluded_folders` (§7.1) reste exclu même s'il apparaît dans `projet.yml`.
- Les champs additionnels (au-delà de `title`) doivent être reportés tels quels dans `projects.json` (§8), pour être exploitables plus tard côté dashboard sans changement du script Python.

## 8. Format des données JSON (contrat entre le script Python et le dashboard JS)

À définir précisément par Codex lors de l'implémentation, mais doit au minimum couvrir :

- **`overview.json`** : totaux globaux (temps total, signes totaux, nombre de projets, dernière mise à jour).
- **`projects.json`** : liste des projets avec, pour chacun : temps total passé, signes réels totaux produits, taille actuelle, date de création, date de dernière activité.
- **`daily.json` / `weekly.json` / `monthly.json`** : séries `{ periode, projet, signes_reels, temps_minutes }` pour alimenter les graphiques en barres empilées par projet.
- **`size_evolution.json`** : séries `{ date, projet, taille_signes }` (taille cumulée totale du projet, imports inclus) pour un graphique en lignes.

## 9. Spécification du dashboard (`docs/index.html` + `dashboard.js`, Chart.js)

### 9.1 Sections attendues

1. **Vue d'ensemble** (cartes chiffrées) : temps total toutes périodes, signes réels totaux, nombre de projets actifs, dernière mise à jour des données.
2. **Signes produits par jour** (30 derniers jours) : histogramme empilé par projet.
3. **Signes produits par semaine** : histogramme empilé par projet (12 dernières semaines par défaut).
4. **Signes produits par mois** : histogramme empilé par projet (depuis le début, ou 24 derniers mois).
5. **Temps passé par projet** : histogramme (total, et/ou par semaine/mois — au choix de l'implémentation, en cohérence avec le reste).
6. **Évolution de la taille des projets** : graphique en lignes, une courbe par projet, axe X = temps, axe Y = signes cumulés.
7. Un sélecteur permettant de filtrer/isoler un projet sur l'ensemble des graphiques (pratique quand il y a beaucoup de projets).

### 9.2 Style
Interface simple et lisible, pas d'exigence graphique particulière au-delà d'un rendu propre et responsive. Palette de couleurs cohérente entre les graphiques (un projet = une couleur stable partout).

## 10. Automatisation (GitHub Actions) — publication cross-repo

Principe retenu : **le pipeline d'analyse tourne dans les Actions du dépôt privé `text`** (accès natif à son propre historique, aucune autorisation particulière à demander pour lire le contenu). Le job ne fait ensuite sortir du dépôt privé que les **JSON déjà agrégés** (aucun texte brut, aucun extrait de fichier `.md`), qu'il pousse vers le dépôt public `WritingLog`.

### 10.1 Secret nécessaire

Un unique secret à créer dans **Settings → Secrets and variables → Actions** du dépôt `tcrouzet/text` :

- `WRITINGLOG_PUSH_TOKEN` : un **Personal Access Token fine-grained** (recommandé, plutôt qu'un classic token), restreint :
  - **Repository access** : uniquement `tcrouzet/WritingLog` (pas d'accès à `text`, pas d'accès "All repositories").
  - **Permissions** : `Contents: Read and write` (rien d'autre n'est nécessaire).
- Ce token n'autorise donc que l'écriture dans `WritingLog` ; il ne donne à `WritingLog` aucun accès en lecture au contenu de `text`. L'inverse (lire `text`) n'est jamais nécessaire puisque le job tourne déjà à l'intérieur de `text`.

### 10.2 Déroulé du workflow `.github/workflows/update-dashboard.yml` (dans `tcrouzet/text`)

1. Se déclencher sur chaque `push` vers la branche principale du vault (et éventuellement en plus sur un `schedule` cron, ex. toutes les heures, en cas d'inactivité de push — utile puisque la sync Obsidian→GitHub tourne en tâche de fond indépendamment des actions de l'auteur).
2. Faire un checkout de `text` avec historique complet (`fetch-depth: 0`, indispensable pour l'analyse Git).
3. Installer Python + dépendances (`scripts/requirements.txt`).
4. Exécuter `scripts/analyze_vault.py` (mode incrémental par défaut), qui écrit ses résultats dans `local_output_dir` (§7.1), en local sur le runner.
5. Cloner `tcrouzet/WritingLog` sur le runner, en utilisant `WRITINGLOG_PUSH_TOKEN` pour l'authentification (`https://x-access-token:${WRITINGLOG_PUSH_TOKEN}@github.com/tcrouzet/WritingLog.git`).
6. Copier le contenu de `local_output_dir/data/` vers `data/` dans le clone de `WritingLog` (écrase les JSON existants, ne touche pas à `index.html`/`style.css`/`dashboard.js`).
7. Committer et pousser vers `WritingLog` si des changements sont détectés (ne pas créer de commit vide si rien n'a changé, par ex. si aucun nouveau commit n'a été fait dans `text` depuis la dernière exécution).
8. GitHub Pages est configuré, une fois manuellement dans les Settings de `WritingLog` (Settings → Pages → Source = branche principale, racine `/`), pour servir automatiquement le contenu du dépôt à chaque nouveau push — aucune action supplémentaire nécessaire côté `WritingLog` après ça.

### 10.3 État incrémental et le double dépôt

`state.json` (§5.2) doit rester cohérent entre les deux dépôts : il est généré/mis à jour dans `text` (à côté de `local_output_dir`, non versionné dans `text` lui-même — voir §6.1) puis copié vers `WritingLog/data/state.json` en même temps que les autres JSON, pour que son contenu soit visible/inspectable publiquement si besoin (aucune donnée sensible dedans : uniquement des hash de commit et des tailles agrégées en signes, jamais de texte).

⚠️ Point d'attention pour Codex : comme `state.json` ne vit "officiellement" que dans `WritingLog` (le repo privé ne le committe pas), le workflow doit, à chaque exécution, **récupérer l'état précédent depuis `WritingLog` avant de lancer l'analyse** (télécharger `data/state.json` depuis `WritingLog`, ou le cloner avec le reste), plutôt que de supposer qu'il existe localement dans `text`. Sinon chaque run repartirait de zéro (perte de l'incrémental, §5.2).

## 10bis. Test et exécution en local

Deux besoins distincts à couvrir, avec deux commandes séparées (pas de mélange des responsabilités) :

Ces commandes s'exécutent dans le dépôt privé `text` (là où vivent le vault et les scripts), et permettent de tester tout le pipeline **sans jamais toucher à `WritingLog`**, donc sans avoir besoin du token `WRITINGLOG_PUSH_TOKEN` en local.

### 10bis.1 Générer les données

```bash
python scripts/analyze_vault.py
```

- Lit `config.yaml` et `projet.yml`, analyse l'historique Git local du vault (mode incrémental via `state.json`, §5.2), et (ré)écrit les fichiers JSON dans `local_output_dir/data/` (par défaut `dashboard_output/data/`, voir §6.1 et §7.1).
- Option `--full-rebuild` : ignore `state.json` et réanalyse tout l'historique depuis le premier commit.
- En local, `state.json` est simplement lu/écrit dans `local_output_dir/` (pas besoin d'aller le chercher dans `WritingLog` comme en CI, §10.3) — le script doit accepter les deux cas (état trouvé localement en priorité, sinon comportement CI décrit en §10.3).
- Ne pousse rien vers `WritingLog` : c'est un mode 100% local, utile pour itérer rapidement.

### 10bis.2 Visualiser le dashboard en local

Un dashboard statique qui charge ses données via `fetch()` (JSON) ne fonctionne pas de façon fiable en ouvrant simplement un fichier `index.html` depuis le disque (`file://`), à cause des restrictions CORS de certains navigateurs. Il faut donc un petit serveur HTTP local.

Pour prévisualiser en local, il faut aussi une copie du code du dashboard (`index.html`, `style.css`, `dashboard.js`) à côté des données générées. Deux approches possibles, à trancher par Codex selon simplicité d'implémentation :
- soit `serve_local.py` sert directement `local_output_dir/` et le script s'assure qu'une copie à jour du HTML/CSS/JS du dashboard (normalement versionné dans `WritingLog`) est aussi présente dans `text` (ex. `scripts/dashboard_template/`) pour permettre un test 100% autonome sans dépendre de `WritingLog` ;
- soit un simple clone local (en lecture) de `WritingLog` est utilisé comme base, et seul `data/` y est remplacé localement par le contenu de `local_output_dir/data/` avant de lancer le serveur.

```bash
python scripts/serve_local.py
```

- Lance un serveur HTTP local (`http.server` de la librairie standard, pas de dépendance supplémentaire).
- Ouvre automatiquement le navigateur par défaut sur l'URL locale (ex. `http://localhost:8000`).
- Port configurable via un argument (`--port`), avec une valeur par défaut raisonnable.
- Ne relance pas l'analyse : suppose que les données existent déjà (générées au préalable via §10bis.1). Le README doit préciser l'enchaînement des deux commandes pour un test local complet :
  ```bash
  python scripts/analyze_vault.py
  python scripts/serve_local.py
  ```

## 11. Livrables attendus de Codex

**Dans `tcrouzet/text` (privé) :**
- Script(s) Python complets et commentés (`scripts/analyze_vault.py`, `scripts/git_utils.py`, `scripts/serve_local.py`).
- Fichier `config.yaml` avec valeurs par défaut raisonnables, incluant la section `publish` (§7.1).
- Fichier `projet.yml` avec un exemple de projet commenté (structure §7.2), prêt à être complété par l'auteur.
- Workflow GitHub Actions `update-dashboard.yml` opérationnel, avec la logique de push cross-repo (§10).
- `README.md` expliquant : comment configurer (`config.yaml` et `projet.yml`), comment créer et restreindre le `WRITINGLOG_PUSH_TOKEN` (§10.1), comment générer les données et tester en local (§10bis), comment ajuster les seuils.

**Dans `tcrouzet/WritingLog` (public) :**
- Dashboard statique fonctionnel (`index.html`, `style.css`, `dashboard.js`) utilisant Chart.js via CDN, affichant le `title` de `projet.yml` quand il existe.
- Un `data/` initial (même vide ou avec des données de démonstration) pour que le site ne soit pas cassé avant la première exécution du workflow.
- Un court `README.md` rappelant que ce dépôt est généré/alimenté automatiquement depuis `tcrouzet/text` et que `data/` ne doit pas être modifié manuellement.
- Rappel (documentation, pas de code) : activer GitHub Pages une fois dans Settings → Pages (source = branche principale, racine `/`).

**Transverse :**
- Tests ou au minimum un jeu de données de démonstration / mode simulation permettant de vérifier tout le pipeline (génération locale + affichage local, §10bis) sans attendre des mois d'historique réel et sans avoir besoin du token de publication.

## 12. Points ouverts (à ajuster empiriquement après les premières exécutions réelles)

- Le seuil de 1000 signes/15 min et le timeout de session de 45 min sont des valeurs de départ raisonnables mais devront probablement être affinés une fois les vraies données observées (le fichier de config est prévu pour ça, aucune valeur ne doit être codée en dur dans le script).
- Comportement exact quand un import survient au milieu d'une session par ailleurs active (actuellement : n'invalide pas la session, mais n'ajoute pas non plus de temps d'écriture supplémentaire au-delà de la durée normale de session).
- Granularité de la série `size_evolution.json` (par commit vs échantillonnage quotidien) : à trancher selon le volume réel de données pour rester performant côté navigateur.
- Choix final entre les deux approches de test local décrites en §10bis.2 (copie locale du template de dashboard dans `text`, vs clone en lecture de `WritingLog`) : à trancher par Codex selon ce qui reste le plus simple à maintenir dans la durée.
