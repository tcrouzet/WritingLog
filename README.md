# Writing Log — dashboard local

Writing Log transforme l’historique Git d’un vault Obsidian en statistiques d’écriture, puis les présente dans un dashboard statique. Il suit les fichiers Markdown situés dans les chemins des projets configurés ainsi que les projets ordinaires placés à la racine du vault.

## Avertissement — limites de la mesure

Writing Log reste un prototype : Git ne contient pas assez d’information pour reconstituer avec certitude la production. Les classifications sont contrôlables, mais ne doivent pas être confondues avec une observation de la frappe.

### Ce que Git permet réellement d’observer

Pour chaque commit, Git fournit deux instantanés du texte et leur date d’enregistrement. Il ne fournit ni la date de frappe des passages, ni leur provenance, ni la durée de travail, ni la distinction entre écriture, collage, déplacement et génération d’un fichier de compilation. Une addition de 25 000 signes dans un diff reste donc seulement une addition de 25 000 signes entre deux instantanés.

La taille du projet au dernier commit est directement observable, à condition que `folder` et `history_folders` décrivent tous ses emplacements. En revanche, les métriques historiques produites par l’analyse sont des inférences.

### Pourquoi la méthode précédente échouait

Jusqu’à la version 18, l’algorithme appliquait un seuil d’import proportionnel au temps écoulé :

```text
seuil = threshold_chars × durée_depuis_le_commit_précédent / threshold_window_minutes
```

Cette formule ne possède pas de fondement permettant d’identifier la provenance du texte. Après un long intervalle, un collage ou une compilation très volumineuse peut passer sous le seuil et être déclaré « écriture réelle ». Après un intervalle court, une production humaine rapide peut au contraire être déclarée « import ». Le même texte reçoit donc une classe différente selon la fréquence des commits.

La détection des déplacements reposait sur des blocs séparés par paragraphes, des ancres de quatre mots et une similarité calculée par `SequenceMatcher`. Elle ne constituait pas une filiation du texte : une réécriture pouvait casser les ancres, des formulations répétitives créer de faux rapprochements et un même passage copié plusieurs fois recevoir un crédit incohérent.

La règle trop large « tout fichier créé puis supprimé = fichier transitoire » pouvait écarter un véritable texte abandonné. Elle a été remplacée par un signal plus strict : seule la réapparition d’un même chemin déjà créé puis supprimé, dont la majorité des fingerprints possède une origine antérieure, caractérise une compilation récurrente.

### Pourquoi les jours sont artificiels

Lorsque plusieurs jours séparent deux commits, la production attribuée au second est divisée uniformément entre les dates intermédiaires. Cette ventilation évite un pic sur la date du commit, mais elle **n’observe aucun jour de production réel**. Un texte écrit en une soirée et commité quatre jours plus tard devient quatre journées fictives de même volume. Un commit intermédiaire sans rapport avec le projet raccourcit en plus cette fenêtre, puisque chaque commit constitue un nouvel instantané du vault.

Les écarts entre commits ne mesurent pas le temps passé à écrire : l’auteur peut avoir travaillé hors d’Obsidian, laissé l’éditeur ouvert ou effectué plusieurs opérations entre deux sauvegardes. Writing Log ne calcule donc plus aucune durée de travail ni aucun rythme en signes par heure.

Les suppressions négatives décrivent uniquement des caractères présents dans un instantané puis absents du suivant. Elles peuvent correspondre à une coupe éditoriale, mais aussi à une restructuration, un déplacement non reconnu, une normalisation ou une modification de format.

### Conséquence

Les JSON et graphiques ne permettent pas d’affirmer avec certitude quel jour un passage a été frappé. La production repose désormais sur une règle textuelle vérifiable : un passage déjà connu est une copie ou un déplacement ; un passage inconnu est nouveau. Aucune durée de travail n’est produite.

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

`./cache.sh` supprime et recrée le miroir Git local. `./cache.sh update` conserve le miroir existant et y ajoute les nouveaux commits. Il annonce explicitement s’il n’existe aucun ajout ou combien de commits ont été récupérés, affiche jusqu’aux dix derniers avec leur SHA, leur date et leur message, puis indique le changement de `HEAD`. `./analyse.sh full` purge la base d’analyse et rejoue systématiquement tout l’historique depuis le premier commit. `./analyse.sh` ne traite que les commits postérieurs au dernier commit enregistré. Ces deux commandes écrivent exclusivement dans SQLite. `./web.sh` ne lance ni analyse ni serveur : il exporte les JSON depuis SQLite puis génère les fichiers statiques de `site/`.

## Installation

Python 3.10 ou plus récent et Git sont requis.

Les environnements sont séparés : `.venv/` est réservé à l’analyse et `.venv-web/` à la génération statique du site. Cette génération n’utilise que la bibliothèque standard Python.

```bash
python -m pip install -r scripts/requirements.txt
```

Dans `config.yaml`, indiquez `vault_path`. Un chemin relatif est résolu depuis le dossier qui contient la configuration, pas depuis le terminal. Ajustez ensuite les dossiers exclus et les paramètres du winnowing.

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

`title` est affiché dans le dashboard. `folder` est le chemin complet du dossier actuellement suivi depuis la racine du vault. `history_folders` liste ses anciens chemins. Le mapping complet sert à la classification et à la filiation du texte. Pour la taille, une seule de ces racines représente le manuscrit actif à un instant donné. Une définition explicite prime sur `excluded_folders` : `folder: "Archives/Rush/manuscrit"` suit donc Rush sous Archives. Tous les autres champs sont transmis sans modification à `projects.json`.

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

La filiation textuelle ne s’arrête pas à cette liste statique. Dès qu’un fichier attribué à un projet est renommé ou déplacé, il conserve cet identifiant même si sa destination — par exemple `Isa/archives/Maison` ou `Isa/archives/manuscritV1-2025` — n’était pas encore déclarée dans `history_folders`. Le registre des fichiers propage ensuite cette attribution aux modifications et à la suppression éventuelle du nouveau chemin : son texte reste reconnaissable et son activité reste rattachée au bon projet.

La **taille du manuscrit** obéit à une règle distincte. Une seule racine est active : lorsqu’une nouvelle racine historique apparaît ou reçoit les fichiers de la précédente, elle la remplace dans la courbe au lieu de s’y additionner. Les anciennes versions rangées dans un sous-dossier `archives` ne comptent jamais, sauf lorsque le `folder` courant du projet se trouve lui-même sous `Archives`. Ainsi, la coexistence temporaire de V1 et V2 ne crée pas une bosse artificielle.

### 3. Un index winnowé persistant représente le texte déjà rencontré

Le texte ajouté est normalisé en Unicode NFKC, passé en minuscules et ses espaces sont compactés. Il est ensuite découpé en fenêtres glissantes de `internal_detection.gram_chars` caractères — 36 par défaut. Chaque fenêtre reçoit un hash BLAKE2b de 64 bits.

Le winnowing conserve seulement le hash minimal de chaque fenêtre de sélection de `internal_detection.selection_chars` caractères — 180 par défaut. L’index SQLite `.cache/fingerprints.sqlite3` stocke ces seuls fingerprints avec leur projet, fichier et commit :

```sql
CREATE TABLE fingerprints (
    hash TEXT NOT NULL,
    project TEXT NOT NULL,
    file TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    removed_at_commit TEXT
);
CREATE INDEX idx_hash ON fingerprints(hash);
```

Quand un fichier change, son contenu complet sert uniquement à calculer le diff textuel. Le winnowing n’est appliqué qu’aux fragments `added_parts` et `removed_parts` produits par ce diff. Les fingerprints ajoutés incrémentent `active_file_hashes`; les fingerprints supprimés le décrémentent. Le contenu intégral n’est winnowé qu’à la première apparition du fichier, puisqu’il constitue alors lui-même l’unique fragment ajouté. Un renommage transfère directement les fingerprints actifs entre les deux chemins par SQL.

L’index complet n’est jamais rechargé ni recalculé à chaque commit. Lorsqu’un fingerprint n’est plus actif dans un fichier, sa ligne historique reste dans `fingerprints` et reçoit le commit dans `removed_at_commit`; elle peut donc identifier une réapparition future. La table auxiliaire mémorise un compteur d’occurrences par couple fichier/hash afin qu’une suppression partielle ne fasse pas disparaître un fingerprint encore présent ailleurs dans le même fichier.

SQLite conserve deux chronologies relationnelles distinctes. `commits` contient chaque commit global traité. `project_commits` contient un instantané pour chaque couple commit/projet à partir de la première apparition du projet, même lorsque le commit ne modifie pas ce projet : taille, racine active et indicateur `touched`. Sa clé primaire `(commit_hash, project)` empêche les doublons. `size_evolution.json` est exporté directement depuis cette table, sans regroupement quotidien, avec le timestamp complet et le hash du commit.

### 4. Les changements sont classés

Tous les fingerprints de tous les fragments ajoutés par un même commit sont d’abord réunis. L’analyse effectue ensuite une seule recherche de recouvrement pour le commit entier — une paire de requêtes dans la limite maximale de paramètres acceptée par SQLite — puis répartit en mémoire les résultats par fragment et par fichier. Les fragments sont découpés en petits groupes de phrases, en conservant exactement tous les caractères. Cette granularité empêche la correction d’un mot de recréditer tout un long paragraphe. L’ensemble des fingerprints connus est enrichi après chaque bloc : si deux fichiers ou deux passages identiques apparaissent dans le même commit, seule la première occurrence peut être considérée comme nouvelle.

```sql
SELECT DISTINCT hash FROM fingerprints WHERE hash IN (...);
```

Le ratio est le nombre de fingerprints du bloc déjà connus divisé par le nombre total de ses fingerprints. Pour un petit groupe de phrases, une seule empreinte exacte de 36 caractères suffit à conserver la filiation : le passage est classé comme texte déjà existant ou modifié. Exiger 85 % à ce niveau recréditerait tout le groupe dès que quelques mots ont été corrigés. Les toutes premières provenances correspondantes sont exportées dans `duplications.json` pour contrôle.

Un fichier nouvellement créé est d’abord testé comme un bloc unique. Si son recouvrement global atteint le seuil, il est traité intégralement comme une compilation et aucun de ses passages légèrement modifiés ou de ses séparateurs n’est recrédité en production. Si le fichier complet n’atteint pas le seuil, l’analyse descend au niveau des groupes de phrases afin de conserver les passages réellement nouveaux et d’écarter seulement les copies.

Le cycle de vie du chemin est également mémorisé, ainsi qu’un SHA-256 du contenu intégral lors de la création. Si un fichier apparaît rempli puis si le même contenu exact disparaît — sous le même chemin ou après un renommage — cette apparition est un artefact transitoire : elle est retirée rétroactivement de la production, des duplications publiées et de toute la courbe de taille, sans aucun seuil. Ses fingerprints restent uniquement dans SQLite afin de reconnaître une réapparition future. Lorsqu’un fichier réapparaît fortement modifié, la majorité de fingerprints antérieurs reste le signal secondaire permettant de reconnaître une compilation récurrente.

Les compilations dont le nom change à chaque export sont traitées rétroactivement. Leur cycle de vie suit aussi les renommages et toutes les retouches intermédiaires : chaque contribution des commits `M` et `R` reste rattachée à la création. Au moment de la suppression, le recouvrement est recalculé sur le contenu final par une nouvelle interrogation de SQLite ; les propres chemins du cycle de vie sont exclus pour empêcher le fichier de se reconnaître lui-même. Le verdict attend aussi que tous les ajouts du commit courant aient été examinés : une fusion supprimée dans le même commit que la création de ses chapitres reconnaît donc immédiatement ces nouveaux fichiers. Cette vérification réutilise les fingerprints des fragments déjà calculés et ne rehashe jamais le contenu intégral d’un fichier `M` ou `R`. Si le contenu final possède au moins 50 % de fingerprints connus ailleurs, toutes les contributions de cette compilation sont retirées de la production et transférées vers les duplications internes. Le ratio réellement utilisé est conservé dans `temporary_compilations` pour audit. Sa taille est retirée uniquement des points compris entre sa création incluse et sa suppression exclue ; après la suppression, la comptabilité normale a déjà retiré le fichier et aucune seconde soustraction n’est appliquée.

Enfin, Git peut rater un renommage lorsque chaque longue ligne ou chaque paragraphe a été légèrement corrigé. Pour chaque commit comportant simultanément des créations et suppressions Markdown dans le même dossier, Writing Log compare leurs ensembles de fingerprints. Deux fichiers de tailles proches partageant au moins 70 % des fingerprints du plus petit sont appariés comme un renommage édité. Le diff porte alors sur l’ancien et le nouveau contenu : le fichier de destination n’est jamais compté intégralement comme une création.

La classification ne possède plus que deux voies :

- au moins un fingerprint du groupe possède une origine : **texte existant, corrigé, déplacé ou dupliqué**, exclu de la production ;
- aucun fingerprint du groupe n’a jamais été rencontré : **texte nouveau**, inclus dans la production.

Le rythme en signes par minute n’est plus un critère de classification. Le champ historique `import_chars` est conservé pour compatibilité, mais vaut toujours zéro dans une reconstruction neuve. Un collage extérieur dont le texte n’a jamais existé dans le vault est donc considéré comme nouveau : les fingerprints ne peuvent pas en connaître la provenance externe. Dans les deux cas, les fingerprints du bloc sont ajoutés avec leur nouvelle provenance. La taille logique part du contenu physique, puis retire les fichiers reconnus comme compilations ou doublons.

Les suppressions effectuées à l’intérieur d’un fichier suivi constituent une quatrième mesure : l’**activité éditoriale négative**. Pour chaque fragment disparu, Writing Log recherche ses fingerprints dans les provenances persistantes des autres fichiers du vault et dans les occurrences encore actives. Une empreinte retrouvée suffit à identifier une copie, un déplacement ou une fusion ; le fragment n’entre alors pas dans `signes_supprimes`. La propre provenance historique du fichier supprimé est explicitement ignorée, sinon toute vraie coupe se reconnaîtrait elle-même. Cette recherche fonctionne si l’autre occurrence précède, accompagne ou suit la suppression. Dans ce dernier cas, une analyse incrémentale corrige rétroactivement l’événement ancien dès la réapparition du texte.

Seule une disparition sans autre provenance est exportée comme quantité positive `signes_supprimes`. Une occurrence identique encore active dans le même fichier suffit également à écarter la suppression : retirer la seconde copie d’un paragraphe ne crée donc aucune production négative. La disparition complète d’un fichier reste exclue, car les fichiers temporaires de fusion apparaissent puis disparaissent fréquemment. Un contrôle de cohérence avertit sur stderr si le total supprimé d’un projet dépasse tous les signes ajoutés au fil de son histoire. Cette mesure éditoriale reste disponible dans les JSON, mais elle n’est jamais injectée dans le graphique de production.

La comparaison commence par retirer les grands préfixes et suffixes identiques, puis travaille sur les zones modifiées au niveau des mots et des caractères. Changer un mot dans un paragraphe ne transforme donc pas tout le paragraphe en texte nouveau.

Une fusion comme `zone.md` est reconnue par ses fingerprints déjà présents, sans recharger les chapitres sources ni comparer le nouveau fichier à tous les fichiers historiques.

### 5. Git donne une fenêtre, pas toujours un jour d’écriture

Git enregistre la date du commit, pas la date de frappe de chaque caractère. Quand deux commits sont espacés de plusieurs jours, Writing Log n’attribue pas tout le travail au dernier jour : les signes sont répartis uniformément entre le lendemain du commit précédent et le jour du commit courant.

Cette répartition est une estimation imposée par l’absence de commits intermédiaires. Elle préserve exactement le total, mais ne prétend pas reconstruire l’heure ou le jour exact de chaque phrase.

### 6. Les sorties sont reconstruites depuis les événements

Les événements classés alimentent ensuite :

- les productions quotidiennes, hebdomadaires et mensuelles ;
- les signes supprimés pendant le travail éditorial, affichés sous l’axe zéro ;
- la production cumulée, qui additionne uniquement l’écriture réelle ;
- la taille logique actuelle de tous les chemins configurés dans `folder` et `history_folders`, compilations et doublons exclus.

La production cumulée et la taille actuelle sont volontairement différentes : la première mesure les caractères classés comme écrits au fil de l’historique, tandis que la seconde mesure le contenu unique du manuscrit aujourd’hui, sans ses assemblages temporaires.

## Générer les données

Deux modes sont volontairement séparés. La reconstruction complète rejoue tout l’historique et se lance uniquement en local, sans contrainte de durée :

```bash
python scripts/analyze_vault.py full
```

Raccourci à la racine : `./analyse.sh full`. `./analyse.sh` lance uniquement le mode incrémental.

Elle effectue une seule passe chronologique. Les métadonnées Git sont lues par lots et chaque commit ne charge que les blobs des fichiers modifiés. Le coût du fingerprinting est proportionnel aux fragments réellement ajoutés ou supprimés, jamais à la taille totale répétée du fichier. La recherche SQLite est regroupée au niveau du commit. Dans un terminal, une barre persistante affiche en continu le pourcentage, le nombre de commits, la vitesse, le temps écoulé et l’ETA. Dans des logs redirigés, un jalon est écrit tous les 250 commits.

Un `full` interrompu ne conserve aucun état partiel : sa transaction SQLite est abandonnée. Le prochain `full` purge de nouveau l’index et repart du premier commit.

Le mode incrémental n’utilise aucun numéro de version d’analyse. La table SQLite `commits` contient chaque commit déjà traité, son horodatage Git et la date de son analyse. Son dernier enregistrement est le curseur de reprise ; il doit correspondre au `last_commit` de la table SQLite `analysis_state`. La table `fingerprint_origins` contient une seule ligne par hash et l’attache définitivement au premier commit, projet et fichier où il a été rencontré. La table `fingerprints` conserve séparément toutes ses occurrences successives. Git est alors interrogé directement sur la plage `last_commit..HEAD` : la liste de l’historique antérieur n’est pas relue. Dans cette plage, seuls les fichiers modifiés sont chargés :

```bash
python scripts/analyze_vault.py incremental
```

Il refuse de démarrer si l’état manque ou ne correspond plus à la configuration, afin de ne jamais déclencher silencieusement une reconstruction complète.

Après une modification de la logique de classification, des exclusions, extensions, seuils ou réglages de session, reconstruisez les données. Une analyse incrémentale ne corrige jamais les événements historiques déjà produits :

```bash
python scripts/analyze_vault.py full
```

Options utiles :

```bash
python scripts/analyze_vault.py full --config /chemin/config.yaml
python scripts/analyze_vault.py incremental --dry-run
```

`--dry-run` analyse et valide les données sans enregistrer le nouvel état dans SQLite. Aucun JSON n’est produit par l’analyse.

Chaque export web génère aussi `projets_archives.yml`. Ce fichier recense les projets historiques désormais déplacés sous `Archives`, avec leur `folder` complet (le sous-dossier `manuscrit` est choisi automatiquement lorsqu’il existe). Pour en suivre un, copiez son bloc dans `projet.yml`, modifiez éventuellement son `title`, puis relancez une analyse complète et l’export.

## Générer le site web

L’analyse SQLite et la génération du site web sont deux commandes indépendantes :

```bash
./analyse.sh
./web.sh
```

`./analyse.sh` met à jour exclusivement `.cache/fingerprints.sqlite3` et ne touche jamais `site/`. La base contient l’état analytique brut et incrémental dans `analysis_state` ; aucune vue web ni aucun JSON n’y est sérialisé.

Les deux opérations web sont elles-mêmes séparées :

```bash
.venv-web/bin/python scripts/export_data.py
.venv-web/bin/python scripts/web.py
```

`export_data.py` lit SQLite, calcule les agrégats d’affichage et remplace `site/data/*.json`. `web.py` copie uniquement HTML, CSS, JavaScript et images depuis `web/` vers `site/`. `./web.sh` enchaîne ces deux commandes par commodité, sans relire Git, reclasser le texte ou démarrer un serveur. Chaque génération web inscrit un timestamp dans les URL du JavaScript, de la feuille de style et du favicon. À chaque chargement de page, le dashboard ajoute également une version unique aux URL des JSON et demande explicitement de ne pas utiliser le cache.

Le filtre principal permet d’isoler un projet. Le graphique « Production » regroupe les vues jour, semaine et mois dans un sélecteur unique. Ses barres représentent exclusivement le texte nouveau dont les fingerprints n’étaient pas déjà connus : aucune suppression ni duplication n’y entre. Son infobulle indique le chemin racine suivi.

Le graphique « Taille » est une série distincte, issue de `size_evolution.json`. Il représente la taille logique du manuscrit au dernier commit de chaque jour, y compris sous ses anciens chemins configurés. Il part de la taille physique et retranche les compilations, exports et doublons reconnus pendant toute leur période d’existence. La courbe monte ou descend sans lissage et n’est pas reconstruite à partir de la production. Chaque graphique possède son propre choix de période — 30 jours, 6 mois, 1 an ou tout l’historique lorsque cette granularité est pertinente. Les JSON conservent toujours l’historique complet.

Le bouton placé en haut à droite de chaque graphique permet de télécharger son rendu en PNG ou en SVG vectoriel. Le nom du fichier reprend le projet sélectionné et le titre du graphique.

Le dashboard utilise Chart.js depuis un CDN : les données restent dans `site/`, mais le premier affichage nécessite un accès réseau pour charger cette bibliothèque.

## Limites et confidentialité

- Un signe est un caractère du Markdown brut après décodage UTF-8 ; ce n’est ni un mot ni une lettre normalisée.
- Sans commit intermédiaire, aucune méthode ne peut retrouver exactement le jour de frappe. Writing Log affiche alors la répartition estimée décrite plus haut.
- La détection des duplications dépend de l’historique disponible. Un texte provenant de l’extérieur du vault est impossible à distinguer d’un texte frappé : tous deux possèdent des fingerprints nouveaux.
- La table SQLite `analysis_state` mémorise les tailles et les événements nécessaires au traitement incrémental. Elle reste dans `.cache/` et n’est jamais publiée.
- `duplications.json` expose volontairement les chemins des fichiers et commits d’origine afin de rendre chaque rapprochement contrôlable. Aucun contenu Markdown n’est exporté.

## Structure des sorties

- `overview.json` : totaux et fraîcheur des données ;
- `projects.json` : totaux d’écriture réelle, suppressions éditoriales, taille actuelle et métadonnées de chaque projet ;
- `daily.json`, `weekly.json`, `monthly.json` : signes ajoutés, signes supprimés et dossiers racines par période et projet ;
- `size_evolution.json` : taille logique du manuscrit à chaque commit global, avec timestamp, hash et indicateur de modification du projet ;
- `duplications.json` : blocs classés comme déplacements/duplications, ratios et provenances d’origine ;

`state.json` n’est plus généré : l’état interne reste exclusivement dans SQLite.

Le dossier `site/` est autonome : il peut être copié et servi tel quel.
