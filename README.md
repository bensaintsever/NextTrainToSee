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

# 3. Vérifier le modèle de marche — sans capteur ni présence sur place
nexttraintosee validate
nexttraintosee validate --fit          # propose une vitesse de ligne par branche

# 4. Écouter le capteur et journaliser les passages réellement observés
nexttraintosee listen --duration 60

# 5. Recaler le modèle et lister ce qui n'est dans aucun horaire
nexttraintosee calibrate --days 7
```

La configuration du site vit dans
[`config/toulouse-guilhemery.toml`](config/toulouse-guilhemery.toml) : point,
gare d'appui, branches, profil de marche, réglages du capteur.

## Vérifier le modèle sans se déplacer

Le point d'observation est situé **entre deux gares**. Les horaires publient donc
déjà, pour chaque circulation, le temps mis à parcourir un segment qui contient
le point : une vérité terrain gratuite.

`nexttraintosee validate` s'en sert, avec deux précautions qui font toute la
différence :

* **On compare à l'horaire le plus rapide, jamais à la médiane.** Sur ce site,
  la marge de régularité médiane atteint deux minutes ; seules les circulations
  les plus tendues approchent la limite physique.
* **On ne s'en sert pas pour interpoler.** Les horaires sont arrondis à la
  minute et inégalement margés : répartir une durée horaire le long du segment
  placerait les passages jusqu'à une minute trop tard. Le modèle reste ancré sur
  l'heure en gare ; l'horaire ne sert qu'à le contrôler.

Sur le site de Toulouse, la vérification a montré qu'une vitesse de ligne unique
ne convenait pas : l'axe de Saint-Agne tient 113 km/h, celui de Montaudran
plafonne à 66 km/h, très en deçà des 120 km/h de l'infrastructure. `--fit`
propose ces valeurs, branche par branche.

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

Le noyau est écrit et testé (`python -m pytest`, 244 tests, sans réseau), et la
chaîne complète a tourné sur les données réelles : géométrie OpenStreetMap
résolue, horaires SNCF chargés, retards temps réel appliqués, modèle de marche
confronté aux horaires.

La configuration de Toulouse ne porte plus aucune estimation : 1 564 m par la
voie jusqu'à Matabiau pour l'axe de Saint-Agne (dont les voies passent à 1 m du
point) à 113 km/h, 1 565 m pour l'axe de Narbonne à 66 km/h.

Reste la moitié capteur, qui demande un micro **au point d'observation** — c'est
elle qui fera tomber les ±13 s à quelques secondes et révélera le fret.

### Un piège macOS : iCloud et les environnements virtuels

Si le dépôt est dans `~/Documents` ou `~/Desktop` avec « Bureau et Documents »
activé, **iCloud synchronise aussi l'environnement virtuel** — et le corrompt :
fichiers dupliqués en `nom 2.ext`, marqués « hidden », parfois évincés du disque.

Le symptôme est déroutant : `pip install -e .` réussit, mais
`import nexttraintosee` échoue. En cause, le fichier `.pth` de l'installation
éditable marqué « hidden » par iCloud, que **Python 3.13+ ignore
délibérément** — sans le moindre message.

`scripts/bootstrap.sh` crée désormais le venv dans `.venv.nosync/`, suffixe que
macOS exclut de la synchronisation, avec un lien symbolique `.venv` pour que les
commandes habituelles ne changent pas. Pour un venv existant :

```bash
mv .venv .venv.nosync && ln -s .venv.nosync .venv
chflags nohidden .venv/lib/python*/site-packages/*.pth
find .venv -name "* [0-9]*" -delete      # copies de conflit iCloud
```

## Licence

MIT.
