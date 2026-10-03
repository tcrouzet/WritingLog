# WritingLog

WritingLog transforme l’historique Git d’un vault Obsidian en tableau de bord d’écriture. Il aide à suivre l’évolution d’un manuscrit, à comparer la production dans le temps et à repérer les changements de dossier. Il suit aussi des fichiers précis, même lorsqu’ils changent de nom.

WritingLog analyse les versions enregistrées dans Git. Il ne lit pas les modifications non commitées et ne mesure pas la frappe en direct.

## Ce que montrent les graphiques

- **Signes produits** : texte considéré comme nouveau par rapport au texte déjà rencontré dans l’historique du vault. Les passages reconnus comme déplacés ou dupliqués ne sont pas comptés comme nouvelle production.
- **Taille** : évolution estimée du manuscrit suivi. Elle sert à voir le volume du texte au fil du temps, et non le travail réalisé pendant une journée.
- **Production par jour de la semaine** : répartition de la production estimée entre lundi et dimanche.
- **Activité horaire d’un fichier** : activité enregistrée aux heures des commits qui touchent ce fichier.

Ces mesures ne reconstituent pas exactement les gestes d’écriture. Git date les commits, pas la frappe. Quand plusieurs jours séparent deux commits, la production peut être répartie entre ces dates : cette répartition est une estimation, pas une observation. Un texte collé depuis l’extérieur du vault peut être considéré comme nouveau, car son origine n’est pas connue de Git.

## Installation

Prérequis : Git et Python 3.10 ou plus récent.

À la racine du dépôt, créez les environnements utilisés par les commandes :

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt
python3 -m venv .venv-web
```

L’environnement `.venv` sert au cache et à l’analyse. `.venv-web` sert à exporter les données et le site ; ces opérations utilisent la bibliothèque standard de Python.

## Configuration

### `config.yaml`

Renseignez `vault_path` avec le chemin du dépôt Git de votre vault. Les chemins relatifs sont résolus depuis le dossier du dépôt WritingLog. Les autres valeurs indiquent où conserver le miroir Git et la base SQLite ; les valeurs par défaut les placent dans `.cache/`.

### `projet.yml`

Ce fichier centralise les exclusions, les projets suivis et les fichiers suivis. Un projet utilise un identifiant stable, un titre et le chemin complet de son dossier actuel. Ajoutez les anciens emplacements dans `history_folders` pour conserver son suivi lors des déplacements :

```yaml
Isa:
  title: "L'expérience humaine"
  folder: "Isa/manuscrit"
  history_folders:
    - "Zone/manuscrit"
```

Les chemins sont relatifs à la racine du vault. Un projet explicitement défini reste suivi même s’il se trouve dans un dossier exclu, par exemple `Archives/Rush/manuscrit`.

Pour suivre un fichier indépendamment de son projet, ajoutez son chemin à la section `Files` :

```yaml
Files:
  - id: hypocrisie-editoriale
    title: "Hypocrisie éditoriale"
    file: "tcrouzet/2026/10/hypocrisie-editoriale.md"
    history_files:
      - "brouillons/ancien-titre.md"
```

`history_files` est facultatif. S’il est renseigné, il définit les chemins historiques retenus pour ce fichier. Les projets archivés détectés sont listés dans `projets_archives.yml` ; copiez un bloc dans `projet.yml` pour le suivre.

## Utilisation

### Première analyse

Depuis la racine de WritingLog :

```bash
./cache.sh
./analyse.sh full
./web.sh
```

`cache.sh` construit le miroir Git local à partir du dépôt indiqué par `vault_path`. `analyse.sh full` analyse tout l’historique et reconstruit la base d’analyse SQLite. Ce mode peut être long ; il n’est nécessaire qu’au premier traitement ou lorsque l’historique doit être recalculé.

### Mise à jour courante

Après avoir commité du texte dans le vault :

```bash
./cache.sh update
./analyse.sh
./web.sh
```

`cache.sh update` ajoute au miroir les nouveaux commits du vault. `analyse.sh` traite les commits qui ne figurent pas encore dans la base. `web.sh` exporte les JSON depuis SQLite puis génère le site statique dans `site/`. Il ne lance ni analyse ni serveur.

Après avoir changé uniquement la liste des fichiers de `projet.yml`, utilisez le mode ciblé, qui nécessite une base d’analyse existante :

```bash
./analyse.sh files
./web.sh
```

Après une modification des projets, des exclusions ou des règles d’analyse, reconstruisez la base pour recalculer tout l’historique :

```bash
./analyse.sh full
./web.sh
```

### Ouvrir le tableau de bord

Pour le consulter localement, démarrez un serveur HTTP standard depuis la racine du dépôt :

```bash
python3 -m http.server 8000 --directory site
```

Ouvrez ensuite `http://localhost:8000`. Pour publier le tableau de bord, publiez le contenu de `site/` sur un hébergement statique. Vérifiez les données exportées avant publication : elles contiennent des noms de projets, des chemins de fichiers et des dates de commits.

## Explorer les résultats

Le sélecteur en haut de page permet de choisir tous les projets, un projet ou un fichier suivi. Le choix d’un projet ou d’un fichier est conservé dans l’URL afin de pouvoir partager une vue précise.

Les graphiques **Production** et **Taille** ont chacun leur propre granularité, zoom et défilement horizontal. Les boutons `−` et `+` changent l’échelle ; faites défiler le graphique pour parcourir la période. Le bouton de téléchargement exporte la vue visible en PNG ou SVG.

Pour un fichier suivi, le tableau de bord affiche aussi sa grille d’activité horaire. Les cases représentent l’activité observée aux dates de commit ; sélectionnez une case pour afficher ses détails. Le réglage `−` ou `+` change la taille des tranches horaires.

## Fichiers produits

- `.cache/vault-history.git/` : miroir de l’historique Git source.
- `.cache/fingerprints.sqlite3` : base d’analyse et index du texte déjà rencontré.
- `site/data/` : données JSON consommées par le tableau de bord.
- `site/` : site statique généré.

L’analyse met à jour SQLite ; l’export web transforme cette base en JSON et en fichiers de site. Le navigateur affiche les données et ne classe pas le texte.
