# NextTrainToSee

Prédire le passage des trains devant un point d'observation quelconque — pas
devant une gare, mais devant une fenêtre, un pont, un bout de talus.

Site de référence : **43°35'52.2"N 1°27'29.5"E** (43.597833, 1.458194), au sud
de Toulouse-Matabiau.

## La réponse courte

**Oui, c'est possible**, et la proximité de la gare rend le problème *plus*
facile, pas seulement possible :

| Ce qui passe devant le point | Prévisible ? | Précision réaliste |
| --- | --- | --- |
| TER, Intercités, TGV | oui | ±15 s en théorique, ±10 s avec le temps réel, **±5 s après recalage** |
| Trains supprimés / déviés | oui | signalés par le flux temps réel |
| **Fret, haut-le-pied, travaux** | **non** | invisible dans toute donnée ouverte |

La dernière ligne est la vraie limite, et elle n'a rien à voir avec la
difficulté du calcul : ces circulations **ne sont publiées nulle part**. C'est
pour cela que ce projet combine deux sources — les horaires ouverts pour dire
*quand et quoi*, un capteur local pour voir *ce que personne ne publie* et pour
recaler le modèle sur la réalité du terrain.

## Oui, il y a des API ouvertes

Tout ce qui suit est gratuit, sans inscription pour les deux premières :

| Source | Ce qu'elle donne | Accès |
| --- | --- | --- |
| **GTFS SNCF** (transport.data.gouv.fr) | horaires théoriques TER / IC / TGV sur ~150 jours | [archive zip](https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip), sans clé |
| **GTFS-RT SNCF** | retards et suppressions, rafraîchis ~toutes les 2 min | [`proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates`](https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates), sans clé |
| **Navitia / api.sncf.com** | prochains départs, dessertes, calcul d'itinéraire | clé gratuite sur [navitia.io](https://doc.navitia.io/), 1 requête/s |
| **OpenStreetMap / Overpass** | géométrie des voies, vitesses, électrification | [Overpass API](https://overpass-api.de/), sans clé |

Ce qu'aucune ne donne : la **position en direct** d'un train, et le **fret**.
La SNCF ne publie ni l'une ni l'autre. Le projet contourne le premier point par
un modèle de marche, et le second par le capteur.

## Comment ça marche

Le GTFS ne donne des horaires qu'**aux gares**. Pour dater un passage en pleine
voie, la chaîne est la suivante :

```
Overpass  ──▶ quelles voies passent près du point, à quelle distance,
                dans quel axe, à quelle vitesse limite
                        │
GTFS      ──▶ quelles circulations desservent la gare d'appui,
                et quel est leur arrêt voisin
                        │
                        ▼
              le cap « gare ─▶ arrêt voisin » désigne la branche empruntée :
              on ne garde que celles qui passent devant le point
                        │
GTFS-RT   ──▶ décalage du retard réel de chaque circulation
                        │
modèle de ──▶ temps de parcours gare ◀─▶ point, selon le régime :
  marche        départ (accélération) · arrivée (freinage) · passage (vitesse de ligne)
                        │
                        ▼
                heure de passage prédite, avec sa fenêtre d'incertitude
                        │
capteur   ──▶ heure de passage *observée* → biais du modèle, et détections
                inexpliquées = candidats fret
```

Le détail du raisonnement, le budget d'erreur et les cas limites sont dans
[`docs/faisabilite.md`](docs/faisabilite.md).

## Installation

En une commande — environnement virtuel, dépendances, horaires SNCF, puis
résolution de la géométrie des voies :

```bash
git clone https://github.com/bensaintsever/NextTrainToSee.git
cd NextTrainToSee
./scripts/bootstrap.sh
```

Le script est tolérant aux pannes : si le téléchargement des horaires ou
l'appel à Overpass échoue, il le signale et poursuit — chaque étape est
reprenable indépendamment.

<details>
<summary>Ou à la main</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[realtime,sensor]"     # temps réel + capteur audio
pip install -e .                        # noyau seul, sans dépendance externe

mkdir -p data
curl -L -o data/sncf-gtfs.zip \
  https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip
```

Le capteur audio a besoin de PortAudio (`brew install portaudio` sur macOS,
`sudo apt install libportaudio2` sur Debian/Ubuntu). Sans lui, tout le reste
fonctionne : `nexttraintosee doctor` vous le dira.

</details>

## Utilisation

```bash
# Ce qui est disponible dans votre environnement
nexttraintosee doctor

# 1. Résoudre la géométrie réelle des voies autour du point (première étape !)
nexttraintosee tracks

# 2. Les prochains passages, temps réel compris
nexttraintosee next
nexttraintosee next --horizon 30 --no-realtime
nexttraintosee next --at 2026-09-09T18:00 --record

# 3. Écouter le capteur et journaliser les passages réellement observés
nexttraintosee listen

# 4. Recaler le modèle et lister ce qui n'est dans aucun horaire
nexttraintosee calibrate --days 7
```

La configuration du site vit dans
[`config/toulouse-guilhemery.toml`](config/toulouse-guilhemery.toml) : point,
gare d'appui, branches, profil de marche, réglages du capteur.

## Le capteur

Le détecteur (`nexttraintosee.sensor`) travaille sur un **niveau scalaire**, pas
sur de l'audio : micro, piézo collé à une vitre, accéléromètre, magnétomètre —
tout ce qui produit une bosse au passage du train fait l'affaire. Un adaptateur
audio est fourni ; l'algorithme est le même quel que soit le capteur.

Il suit un fond adaptatif, déclenche sur dépassement avec hystérésis, et rejette
ce qui est trop court (claquement de portière) ou trop long (travaux, averse).

Deux usages :

* **recalage** — `calibrate` compare observé et prédit, en déduit le biais du
  modèle par régime de marche, et peut ré-ajuster accélération, freinage et
  vitesse de ligne sur les temps réellement mesurés ;
* **découverte** — les détections qui ne correspondent à aucune prédiction sont
  listées séparément : sur une ligne classique, ce sont très probablement des
  sillons fret, des haut-le-pied ou des trains de travaux.

## État du projet

Le noyau est écrit et testé (`python -m pytest`, 195 tests, sans réseau), et la
chaîne complète a tourné sur les données réelles : géométrie OpenStreetMap
résolue, horaires SNCF chargés, retards temps réel appliqués.

La configuration de Toulouse porte des **valeurs mesurées**, plus des
estimations : 1 564 m par la voie jusqu'à Matabiau pour l'axe de Saint-Agne
(dont les voies passent à 1 m du point), 1 565 m pour l'axe de Narbonne.

### Un piège macOS

Sur macOS, le fichier `.pth` de l'installation éditable se retrouve parfois
marqué « hidden » par le système de fichiers — et **Python 3.13+ ignore
délibérément les `.pth` cachés**. L'installation ne fait alors rien, sans le
moindre message : `import nexttraintosee` échoue alors que `pip` a réussi.

`scripts/bootstrap.sh` détecte et répare le cas. À la main :

```bash
chflags nohidden .venv/lib/python*/site-packages/*.pth
```

## Licence

MIT.
