# Writing Log — dashboard local

Writing Log transforme l’historique Git d’un vault Obsidian en statistiques d’écriture, puis les présente dans un dashboard statique. Il suit les fichiers Markdown situés dans les chemins des projets configurés ainsi que les projets ordinaires placés à la racine du vault.

## Avertissement — méthode actuelle non fiable

La version actuelle est un **prototype dont les mesures de production, d’import, de déplacement et de temps ne sont pas fiables**. Le dashboard fonctionne techniquement, mais ses chiffres ne doivent pas être interprétés comme une mesure réelle du travail d’écriture. Ce défaut ne relève pas seulement d’un seuil à ajuster : la méthode tente de déduire des événements absents de l’historique Git.

### Ce que Git permet réellement d’observer

Pour chaque commit, Git fournit deux instantanés du texte et leur date d’enregistrement. Il ne fournit ni la date de frappe des passages, ni leur provenance, ni la durée de travail, ni la distinction entre écriture, collage, déplacement et génération d’un fichier de compilation. Une addition de 25 000 signes dans un diff reste donc seulement une addition de 25 000 signes entre deux instantanés.

La taille du projet au dernier commit est directement observable, à condition que `folder` désigne exactement son emplacement. En revanche, les métriques historiques produites par l’analyse sont des inférences.

### Pourquoi la classification actuelle échoue

L’algorithme applique un seuil d’import proportionnel au temps écoulé :

```text
seuil = threshold_chars × durée_depuis_le_commit_précédent / threshold_window_minutes
```

Cette formule ne possède pas de fondement permettant d’identifier la provenance du texte. Après un long intervalle, un collage ou une compilation très volumineuse peut passer sous le seuil et être déclaré « écriture réelle ». Après un intervalle court, une production humaine rapide peut au contraire être déclarée « import ». Le même texte reçoit donc une classe différente selon la fréquence des commits.

La détection des déplacements repose sur des blocs séparés par paragraphes, des ancres de quatre mots et une similarité calculée par `SequenceMatcher`. Elle ne constitue pas une filiation du texte : une réécriture peut casser les ancres, des formulations répétitives peuvent créer de faux rapprochements et un même passage copié plusieurs fois peut recevoir un crédit incohérent. La détection de renommage `git diff -M` repose elle aussi sur un seuil de similarité propre à Git.

La règle « fichier créé puis supprimé = fichier transitoire » est une hypothèse propre à ce vault, pas une propriété démontrable. Elle peut écarter un véritable texte abandonné. Inversement, un fichier de compilation conservé durablement n’est pas transitoire et peut rester mal classé.

### Pourquoi les jours et les durées sont artificiels

Lorsque plusieurs jours séparent deux commits, la production attribuée au second est divisée uniformément entre les dates intermédiaires. Cette ventilation évite un pic sur la date du commit, mais elle **n’observe aucun jour de production réel**. Un texte écrit en une soirée et commité quatre jours plus tard devient quatre journées fictives de même volume. Un commit intermédiaire sans rapport avec le projet raccourcit en plus cette fenêtre, puisque chaque commit constitue un nouvel instantané du vault.

Les écarts courts entre commits ne mesurent pas davantage le temps passé à écrire : l’auteur peut avoir travaillé hors d’Obsidian, laissé l’éditeur ouvert ou effectué plusieurs opérations entre deux sauvegardes. La vitesse moyenne apprise à partir de ces écarts propage donc cette incertitude dans les estimations ultérieures. Afficher `temps inconnu` évite d’inventer une valeur lorsque la base manque, mais ne rend pas fiables les durées dites observées ou estimées.

Les suppressions négatives décrivent uniquement des caractères présents dans un instantané puis absents du suivant. Elles peuvent correspondre à une coupe éditoriale, mais aussi à une restructuration, un déplacement non reconnu, une normalisation ou une modification de format.

### Conséquence

Les JSON et graphiques actuels servent à examiner le prototype et ses erreurs. Ils ne permettent pas encore d’affirmer combien de signes ont été écrits un jour donné, combien d’heures ont été travaillées, ni quelle part provient réellement d’un import ou d’un déplacement. Une méthode de remplacement devra soit s’appuyer sur des événements capturés au moment de l’édition, soit assumer explicitement des métriques limitées aux différences observables entre commits, sans les présenter comme un historique réel de production.

## Commandes à utiliser

Premier lancement — reconstruire le cache local puis analyser tout l’historique :

```bash
./cache.sh
./analyse.sh full
./web.sh
```

Utilisation courante — synchroniser les nouveaux commits locaux puis lancer l’analyse incrémentale :

```bash
./cache.sh update
./analyse.sh
./web.sh
```

`./cache.sh` supprime et recrée le miroir Git local. `./cache.sh update` conserve le miroir existant et y ajoute les nouveaux commits. `./analyse.sh full` effectue une reconstruction complète ou reprend son dernier checkpoint compatible. `./analyse.sh` ne traite que les commits postérieurs à la dernière analyse terminée. `./web.sh` ne lance ni analyse ni serveur : il génère uniquement les fichiers statiques de `site/` en conservant `site/data/`.

## Installation

Python 3.10 ou plus récent et Git sont requis.

Les environnements sont séparés : `.venv/` est réservé à l’analyse et `.venv-web/` à la génération statique du site. Cette génération n’utilise que la bibliothèque standard Python.

```bash
python -m pip install -r scripts/requirements.txt
```

Dans `config.yaml`, indiquez `vault_path`. Un chemin relatif est résolu depuis le dossier qui contient la configuration, pas depuis le terminal. Ajustez ensuite les dossiers exclus, le seuil d’import et les durées de session.

`history_repo` peut pointer vers un miroir Git local dédié. La configuration fournie utilise `.cache/vault-history.git`. Créez ou actualisez ce miroir vous-même avant l’analyse :

```bash
.venv/bin/python scripts/cache_history.py
```

Raccourcis à la racine : `./cache.sh` supprime et reconstruit le cache ; `./cache.sh update` synchronise le miroir existant.

La reconstruction crée une copie Git indépendante de tout l’historique local (`--no-hardlinks`) et vérifie le nombre de commits. Un cache ancien ou interrompu est supprimé par `rebuild`, sans sauvegarde résiduelle. Aucun accès réseau n’est nécessaire lorsque `vault_path` est un dépôt local.

Dans `projet.yml`, chaque clé est l’identifiant stable d’un projet et `folder` indique son emplacement complet dans le vault :

```yaml
mon-roman:
  title: "Mon roman"
  folder: "mon-roman/manuscrit"
  history_folders:
    - "ancien-dossier/manuscrit"
  genre: "roman"
  objectif_signes: 500000
```

`title` est affiché dans le dashboard. `folder` est le chemin complet du dossier actuellement suivi depuis la racine du vault. `history_folders` liste ses anciens chemins : ils servent à retrouver la production et les sessions passées, mais jamais à calculer la taille actuelle. Une définition explicite prime sur `excluded_folders` : `folder: "Archives/Rush/manuscrit"` suit donc Rush sous Archives. Tous les autres champs sont transmis sans modification à `projects.json`.

Les projets qui n’existent plus à la racine du vault — notamment ceux déplacés dans un dossier exclu comme `Archives` — sont recensés dans `projets_archives.yml`. Ils redeviennent visibles et suivis dès que leur bloc est copié dans `projet.yml`.

## Comment fonctionne Writing Log

### 1. Le cache ne contient que l’historique Git

`cache.sh` construit un miroir Git indépendant dans `.cache/vault-history.git`. L’analyse travaille exclusivement sur ce miroir et ne modifie jamais le vault source.

`./cache.sh update` copie les nouveaux commits du dépôt local configuré par `vault_path`. Il ne lit pas les modifications non commitées du répertoire de travail et ne lance aucun accès réseau lorsque `vault_path` est local. Une modification Obsidian ne devient donc visible qu’après sa présence dans un commit, puis une mise à jour du cache.

### 2. Chaque fichier est rattaché à un projet stable

L’identifiant YAML du projet reste stable même si son dossier change. Pour chaque chemin Markdown rencontré dans l’historique, l’analyse cherche successivement :

1. le chemin actuel `folder` ;
2. les anciens chemins `history_folders` ;
3. à défaut, le dossier de premier niveau pour les projets non configurés.

Une correspondance explicite dans `projet.yml` est prioritaire sur `excluded_folders`. Le chemin racine qui a produit chaque valeur — par exemple `Isa/manuscrit` ou `Zone/manuscrit` — est conservé dans les données quotidiennes et affiché dans l’infobulle du graphique.

### 3. Le cycle de vie des fichiers est examiné avant les signes

Avant de compter les modifications, l’analyse repère les créations, suppressions et renommages sur l’ensemble de la période traitée.

- Un fichier Markdown créé puis supprimé est transitoire. Dans ce vault, il correspond à un artefact de fusion ou d’export : toutes ses créations et modifications sont exclues de la production.
- Lorsqu’une suppression et une création correspondante sont identifiables dans le même commit, elles sont suivies comme un renommage grâce à la détection native de Git, y compris lorsque le fichier a été modifié pendant son déplacement.
- Si une suppression révèle en mode incrémental qu’un fichier déjà compté était transitoire, l’analyse demande explicitement un `full` afin de corriger tout son passé.

### 4. Les changements sont classés

Pour chaque commit et chaque projet, Writing Log sépare trois catégories :

- **Écriture réelle** : caractères nouveaux qui ne sont ni une copie interne ni un import détecté.
- **Déplacement ou duplication interne** : texte déjà présent dans le vault, déplacé, recopié ou rassemblé dans un fichier de compilation. Cette quantité ne contribue jamais à la production.
- **Import externe** : volume de texte neuf incompatible avec la fenêtre comprise entre le commit précédent du vault et le commit courant. Cette quantité ne contribue jamais à la production.

Les suppressions effectuées à l’intérieur d’un fichier suivi constituent une quatrième mesure : l’**activité éditoriale négative**. Elles sont exportées comme une quantité positive `signes_supprimes`, puis dessinées sous l’axe zéro dans le graphique. Les suppressions dues à un déplacement, à un renommage, à un fichier transitoire ou à la disparition d’un fichier complet sont exclues : elles ne prouvent pas un travail de coupe du texte.

La comparaison commence par retirer les grands préfixes et suffixes identiques, puis travaille sur les zones modifiées au niveau des mots et des caractères. Changer un mot dans un paragraphe ne transforme donc pas tout le paragraphe en texte nouveau.

Les déplacements effectués dans un même commit sont rapprochés avec les fragments supprimés. Un gros fichier nouvellement créé est en plus comparé à tous les fichiers du projet au commit précédent. Une fusion comme `zone.md`, composée de chapitres encore présents, est ainsi reconnue comme duplication interne.

### 5. Git donne une fenêtre, pas toujours un jour d’écriture

Git enregistre la date du commit, pas la date de frappe de chaque caractère. Quand deux commits sont espacés de plusieurs jours, Writing Log n’attribue pas tout le travail au dernier jour : les signes sont répartis uniformément entre le lendemain du commit précédent et le jour du commit courant.

Cette répartition est une estimation imposée par l’absence de commits intermédiaires. Elle préserve exactement le total, mais ne prétend pas reconstruire l’heure ou le jour exact de chaque phrase.

### 6. Le temps n’est jamais déduit d’un taux arbitraire

Une durée est observable uniquement lorsqu’un commit touchant le projet suit immédiatement un autre commit touchant ce même projet et que leur écart ne dépasse pas `session.timeout_minutes`.

Ces fenêtres observées construisent progressivement une vitesse moyenne propre au projet. Pour un commit espacé :

- si le projet possède déjà une vitesse historique fiable, elle permet une estimation ;
- si l’estimation dépasserait l’intervalle Git disponible, elle est rejetée ;
- sans vitesse historique fiable, le temps et le ratio signes/heure restent `inconnus`.

Aucune vitesse fixe, aucun minimum de temps et aucun plafond de signes par heure ne sont inventés. Les JSON distinguent les temps observés, les temps estimés (`temps_estime: true`) et les temps inconnus (`temps_minutes: null`).

### 7. Les sorties sont reconstruites depuis les événements

Les événements classés alimentent ensuite :

- les productions quotidiennes, hebdomadaires et mensuelles ;
- les signes supprimés pendant le travail éditorial, affichés sous l’axe zéro ;
- les temps et rythmes lorsqu’ils sont disponibles ;
- la production cumulée, qui additionne uniquement l’écriture réelle ;
- la taille actuelle, calculée uniquement dans le chemin `folder` actuel du projet.

La production cumulée et la taille actuelle sont volontairement différentes : la première mesure les caractères classés comme écrits au fil de l’historique, tandis que la seconde mesure le contenu physiquement présent aujourd’hui.

## Générer les données

Deux modes sont volontairement séparés. La reconstruction complète rejoue tout l’historique et se lance uniquement en local, sans contrainte de durée :

```bash
python scripts/analyze_vault.py full
```

Raccourci à la racine : `./analyse.sh full`. `./analyse.sh` lance uniquement le mode incrémental.

Elle pré-indexe d’abord le cycle de vie des fichiers, puis effectue une passe chronologique de classification. Les métadonnées Git sont lues par lots (et non par un processus Git lancé pour chaque commit) ; les déplacements d’un gros commit sont indexés par blocs au lieu d’être tous comparés deux à deux. Dans un terminal, une barre persistante affiche en continu le pourcentage, le nombre de commits, la vitesse, le temps écoulé et l’ETA. Dans des logs redirigés, un jalon est écrit tous les 250 commits. Sur un gros vault, lancez-la directement dans votre terminal. Une interruption ne touche pas au vault et les JSON existants ne sont remplacés qu’à la fin.

Un checkpoint est enregistré dans `.cache/full-analysis.checkpoint` tous les 100 commits. Après une interruption, `./analyse.sh full` reprend automatiquement si le miroir, la configuration des projets et la version de l’algorithme sont inchangés. Si l’un d’eux change, le checkpoint est ignoré et la reconstruction repart de zéro. Le checkpoint est supprimé après une génération complète réussie.

Le mode incrémental exige un `site/data/state.json` valide et ne traite que les commits nouveaux :

```bash
python scripts/analyze_vault.py incremental
```

Il refuse de démarrer si l’état manque ou ne correspond plus à la configuration, afin de ne jamais déclencher silencieusement une reconstruction complète.

Après une modification des exclusions, extensions, seuils ou réglages de session, reconstruisez les données :

```bash
python scripts/analyze_vault.py full
```

Options utiles :

```bash
python scripts/analyze_vault.py full --config /chemin/config.yaml
python scripts/analyze_vault.py incremental --dry-run
```

`--dry-run` analyse et valide les données sans écrire les JSON. Le dashboard livré contient un petit jeu de démonstration ; la première analyse le remplace.

Chaque analyse génère aussi `projets_archives.yml`. Ce fichier recense les projets historiques désormais déplacés sous `Archives`, avec leur `folder` complet (le sous-dossier `manuscrit` est choisi automatiquement lorsqu’il existe). Pour en suivre un, copiez son bloc dans `projet.yml`, modifiez éventuellement son `title`, puis lancez `python scripts/analyze_vault.py full`.

## Générer le site web

La génération des JSON et celle du site web sont deux commandes indépendantes :

```bash
./analyse.sh
./web.sh
```

`./analyse.sh` met à jour uniquement `site/data/*.json`. `./web.sh` copie les sources de `web/` vers `site/`, sans modifier les JSON et sans démarrer de serveur.

Le filtre principal permet d’isoler un projet. Le graphique « Signes ajoutés et supprimés » regroupe les vues jour, semaine et mois dans un sélecteur unique : les ajouts sont positifs et les coupes négatives. Son infobulle indique le chemin racine suivi, le temps observé ou estimé lorsqu’il existe, et le volume total de signes travaillés par heure. Chaque graphique possède son propre choix de période — 30 jours, 6 mois, 1 an ou tout l’historique lorsque cette granularité est pertinente. Les JSON conservent toujours l’historique complet.

Le bouton placé en haut à droite de chaque graphique permet de télécharger son rendu en PNG ou en SVG vectoriel. Le nom du fichier reprend le projet sélectionné et le titre du graphique.

Le dashboard utilise Chart.js depuis un CDN : les données restent dans `site/`, mais le premier affichage nécessite un accès réseau pour charger cette bibliothèque.

## Limites et confidentialité

- Un signe est un caractère du Markdown brut après décodage UTF-8 ; ce n’est ni un mot ni une lettre normalisée.
- Sans commit intermédiaire, aucune méthode ne peut retrouver exactement le jour de frappe. Writing Log affiche alors la répartition estimée décrite plus haut.
- La détection des imports et duplications est une classification fondée sur l’historique disponible. Les valeurs doivent rester contrôlables grâce au chemin racine affiché dans les infobulles.
- L’état incrémental mémorise les tailles, les identifiants de blobs et les événements nécessaires au recalcul. Les chemins complets des fichiers y sont hachés ; seuls les chemins racines configurés sont exportés pour permettre le contrôle du suivi.
- Aucun contenu Markdown n’est écrit dans les JSON du site.

## Structure des sorties

- `overview.json` : totaux et fraîcheur des données ;
- `projects.json` : totaux d’écriture réelle, suppressions éditoriales, temps, taille actuelle et métadonnées de chaque projet ;
- `daily.json`, `weekly.json`, `monthly.json` : signes ajoutés, signes supprimés, temps disponible, statut estimé et dossiers racines par période et projet ;
- `size_evolution.json` : production réelle cumulée par projet (nom de fichier historique conservé pour compatibilité) ;
- `state.json` : état interne nécessaire au traitement incrémental.

Le dossier `site/` est autonome : il peut être copié et servi tel quel.
