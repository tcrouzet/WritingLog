# Writing Log — dashboard local

Writing Log transforme l’historique Git d’un vault Obsidian en statistiques d’écriture, puis les présente dans un dashboard statique. Il suit les fichiers Markdown situés dans les chemins des projets configurés ainsi que les projets ordinaires placés à la racine du vault.

## Avertissement — limites de la mesure

Writing Log reste un prototype : Git ne contient pas assez d’information pour reconstituer avec certitude la production et le temps de travail. Les classifications sont contrôlables, mais ne doivent pas être confondues avec une observation de la frappe.

### Ce que Git permet réellement d’observer

Pour chaque commit, Git fournit deux instantanés du texte et leur date d’enregistrement. Il ne fournit ni la date de frappe des passages, ni leur provenance, ni la durée de travail, ni la distinction entre écriture, collage, déplacement et génération d’un fichier de compilation. Une addition de 25 000 signes dans un diff reste donc seulement une addition de 25 000 signes entre deux instantanés.

La taille du projet au dernier commit est directement observable, à condition que `folder` désigne exactement son emplacement. En revanche, les métriques historiques produites par l’analyse sont des inférences.

### Pourquoi la méthode précédente échouait

Jusqu’à la version 18, l’algorithme appliquait un seuil d’import proportionnel au temps écoulé :

```text
seuil = threshold_chars × durée_depuis_le_commit_précédent / threshold_window_minutes
```

Cette formule ne possède pas de fondement permettant d’identifier la provenance du texte. Après un long intervalle, un collage ou une compilation très volumineuse peut passer sous le seuil et être déclaré « écriture réelle ». Après un intervalle court, une production humaine rapide peut au contraire être déclarée « import ». Le même texte reçoit donc une classe différente selon la fréquence des commits.

La détection des déplacements reposait sur des blocs séparés par paragraphes, des ancres de quatre mots et une similarité calculée par `SequenceMatcher`. Elle ne constituait pas une filiation du texte : une réécriture pouvait casser les ancres, des formulations répétitives créer de faux rapprochements et un même passage copié plusieurs fois recevoir un crédit incohérent.

La règle « fichier créé puis supprimé = fichier transitoire » pouvait écarter un véritable texte abandonné, tandis qu’un fichier de compilation conservé durablement échappait à la règle. Ces trois mécanismes ont été retirés au profit de l’index winnowé décrit plus bas.

### Pourquoi les jours et les durées sont artificiels

Lorsque plusieurs jours séparent deux commits, la production attribuée au second est divisée uniformément entre les dates intermédiaires. Cette ventilation évite un pic sur la date du commit, mais elle **n’observe aucun jour de production réel**. Un texte écrit en une soirée et commité quatre jours plus tard devient quatre journées fictives de même volume. Un commit intermédiaire sans rapport avec le projet raccourcit en plus cette fenêtre, puisque chaque commit constitue un nouvel instantané du vault.

Les écarts courts entre commits ne mesurent pas davantage le temps passé à écrire : l’auteur peut avoir travaillé hors d’Obsidian, laissé l’éditeur ouvert ou effectué plusieurs opérations entre deux sauvegardes. La vitesse moyenne apprise à partir de ces écarts propage donc cette incertitude dans les estimations ultérieures. Afficher `temps inconnu` évite d’inventer une valeur lorsque la base manque, mais ne rend pas fiables les durées dites observées ou estimées.

Les suppressions négatives décrivent uniquement des caractères présents dans un instantané puis absents du suivant. Elles peuvent correspondre à une coupe éditoriale, mais aussi à une restructuration, un déplacement non reconnu, une normalisation ou une modification de format.

### Conséquence

Les JSON et graphiques ne permettent pas d’affirmer avec certitude combien de signes ont été écrits un jour donné ni combien d’heures ont été travaillées. La méthode actuelle limite désormais la reconnaissance des déplacements à une trace textuelle vérifiable et exporte ses sources, mais la distinction écriture/import reste une inférence statistique.

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

Dans `config.yaml`, indiquez `vault_path`. Un chemin relatif est résolu depuis le dossier qui contient la configuration, pas depuis le terminal. Ajustez ensuite les dossiers exclus, les paramètres du winnowing, le percentile du rythme et la durée maximale d’une session observable.

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

### 4. Les changements sont classés

Tous les fingerprints de tous les fragments ajoutés par un même commit sont d’abord réunis. L’analyse effectue ensuite une seule recherche de recouvrement pour le commit entier — une paire de requêtes dans la limite maximale de paramètres acceptée par SQLite — puis répartit en mémoire les résultats par fragment et par fichier :

```sql
SELECT DISTINCT hash FROM fingerprints WHERE hash IN (...);
```

Le ratio est le nombre de fingerprints du bloc déjà connus divisé par le nombre total de ses fingerprints. À partir de `internal_detection.overlap_threshold` — 0,85 par défaut — le bloc entier est classé comme déplacement ou duplication interne. Les toutes premières provenances correspondantes sont exportées dans `duplications.json` pour contrôle.

Sous ce seuil, le bloc est textuellement nouveau. Son rythme en signes par minute est comparé au percentile empirique `import_detection.rate_percentile` — 99 par défaut — des rythmes d’écriture déjà observés sur ce projet :

- ratio de recouvrement supérieur ou égal au seuil : **déplacement/duplication interne** ;
- ratio inférieur et rythme inférieur ou égal au percentile historique : **écriture réelle** ;
- ratio inférieur et rythme supérieur au percentile historique : **import externe**.

Tant que le projet ne possède aucun rythme antérieur observable, aucun seuil fixe ne le remplace : le texte non dupliqué est conservé comme écriture et le seuil n’est appris que sur les intervalles courts réellement disponibles. Déplacements et imports sont exclus des signes produits mais restent inclus dans la taille actuelle, calculée directement depuis le contenu des fichiers. Dans les trois cas, les fingerprints du bloc sont ajoutés avec leur nouvelle provenance.

Les suppressions effectuées à l’intérieur d’un fichier suivi constituent une quatrième mesure : l’**activité éditoriale négative**. Elles sont exportées comme une quantité positive `signes_supprimes`, puis dessinées sous l’axe zéro. Une suppression recouverte par les fingerprints d’un déplacement reconnu dans le même commit et la disparition d’un fichier complet sont exclues de cette mesure.

La comparaison commence par retirer les grands préfixes et suffixes identiques, puis travaille sur les zones modifiées au niveau des mots et des caractères. Changer un mot dans un paragraphe ne transforme donc pas tout le paragraphe en texte nouveau.

Une fusion comme `zone.md` est reconnue par ses fingerprints déjà présents, sans recharger les chapitres sources ni comparer le nouveau fichier à tous les fichiers historiques.

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

Elle effectue une seule passe chronologique. Les métadonnées Git sont lues par lots et chaque commit ne charge que les blobs des fichiers modifiés. Le coût du fingerprinting est proportionnel aux fragments réellement ajoutés ou supprimés, jamais à la taille totale répétée du fichier. La recherche SQLite est regroupée au niveau du commit. Dans un terminal, une barre persistante affiche en continu le pourcentage, le nombre de commits, la vitesse, le temps écoulé et l’ETA. Dans des logs redirigés, un jalon est écrit tous les 250 commits.

Un checkpoint est enregistré dans `.cache/full-analysis.checkpoint` tous les 100 commits, simultanément à une transaction SQLite. Après une interruption, `./analyse.sh full` reprend uniquement si le checkpoint et le dernier commit enregistré dans l’index coïncident. Le checkpoint est supprimé après une génération complète réussie.

Le mode incrémental exige un `site/data/state.json` et un index SQLite portant le même dernier commit. Git est interrogé directement sur la plage `dernier_commit..HEAD` : la liste de l’historique antérieur n’est pas relue. Dans cette plage, seuls les fichiers modifiés sont chargés :

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
- `state.json` mémorise les tailles, les rythmes observés et les événements nécessaires aux agrégats. L’index SQLite reste dans `.cache/` et n’est pas publié.
- `duplications.json` expose volontairement les chemins des fichiers et commits d’origine afin de rendre chaque rapprochement contrôlable. Aucun contenu Markdown n’est exporté.

## Structure des sorties

- `overview.json` : totaux et fraîcheur des données ;
- `projects.json` : totaux d’écriture réelle, suppressions éditoriales, temps, taille actuelle et métadonnées de chaque projet ;
- `daily.json`, `weekly.json`, `monthly.json` : signes ajoutés, signes supprimés, temps disponible, statut estimé et dossiers racines par période et projet ;
- `size_evolution.json` : production réelle cumulée par projet (nom de fichier historique conservé pour compatibilité) ;
- `duplications.json` : blocs classés comme déplacements/duplications, ratios et provenances d’origine ;
- `state.json` : état interne nécessaire au traitement incrémental.

Le dossier `site/` est autonome : il peut être copié et servi tel quel.
