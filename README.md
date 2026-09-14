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

L'outil est installé dans l'environnement virtuel `.venv`. **Activez-le à chaque
nouveau terminal**, sinon `nexttraintosee` reste introuvable :

```bash
cd NextTrainToSee
source .venv/bin/activate
```

Pour une commande isolée, `./.venv/bin/nexttraintosee …` fonctionne sans rien
activer. Les scripts de `scripts/` s'en chargent eux-mêmes.

```bash
# Ce qui est disponible dans votre environnement
nexttraintosee doctor

# 1. Résoudre la géométrie réelle des voies autour du point (première étape !)
nexttraintosee tracks

# 2. Les prochains passages, temps réel compris
nexttraintosee next
nexttraintosee next --horizon 30 --no-realtime
nexttraintosee next --watch             # « guetter dès HH:MM:SS »
nexttraintosee next --at 2026-09-09T18:00 --record

# 3. Répartition horaire des passages
nexttraintosee histogram
nexttraintosee histogram --csv data/histogramme.csv

# 4. Vérifier le modèle de marche — sans capteur ni présence sur place
nexttraintosee validate
nexttraintosee validate --fit          # propose une vitesse de ligne par branche

# 5. Collecter à intervalle régulier, pour mesurer ce que valent les annonces
./scripts/collect.sh 180        # puis, plus tard :
nexttraintosee coverage --days 7

# 6. Écouter le capteur et journaliser les passages réellement observés
nexttraintosee listen --duration 60

# 7. Recaler le modèle et lister ce qui n'est dans aucun horaire
nexttraintosee calibrate --days 7
```

La configuration du site vit dans
[`config/toulouse-guilhemery.toml`](config/toulouse-guilhemery.toml) : point,
gare d'appui, branches, profil de marche, réglages du capteur.

## Annoncer tôt plutôt que juste

L'erreur d'annonce n'est pas symétrique. Pour qui veut voir passer le train,
annoncer trop tôt coûte quelques secondes d'attente ; annoncer trop tard fait
manquer le passage, et rien ne le rattrape.

`next --watch` annonce donc la **borne basse** de la fenêtre, diminuée de
`lead_margin_s` — le temps de se poster :

```
  dans 5.1 min  guetter dès 11:26:30 · passage vers 11:26:44 (±13 s) ← Axe Saint-Agne …
```

`coverage` mesure la même asymétrie : sa colonne « annoncé trop tard » compte
les estimations qui plaçaient le passage après son heure finale — les seules qui
coûtent vraiment.

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

Deux garde-fous évitent d'en conclure trop :

* **Un segment dont tous les horaires sont identiques ne dit rien.** Son minimum
  est une allocation standard reconduite, pas une marche tendue : `--fit` s'y
  refuse et l'explique.
* **Une catégorie de train qui ne s'arrête nulle part à proximité n'est pas
  calable.** À Toulouse, aucun Intercités ni TGV ne dessert Saint-Agne ou
  Montaudran : leur profil reste une estimation, et `validate` le dit.

## Types de matériel

Un automoteur régional, une rame tractée et une rame à grande vitesse n'ont ni
la même accélération ni la même vitesse pratique en sortie de gare. Les
`[[categories]]` de la configuration rattachent chaque circulation à un profil,
d'après son numéro et l'intitulé de sa ligne.

L'ordre des couches compte : profil du site, puis vitesse de la branche, puis
matériel — le plus précis l'emporte. C'est nécessaire, car une vitesse de branche
est calée sur les omnibus qui desservent les haltes de l'axe. Sans cette
distinction, un Intercités traversant sans arrêt héritait des 66 km/h d'un
omnibus qui, lui, freine pour s'arrêter à Montaudran.

## L'application

![L'application v0](docs/img/app-v0.jpeg)

Une PWA servie par un serveur local — le pipeline validé reste en Python, le
téléphone n'affiche que le résultat :

```bash
nexttraintosee serve            # puis http://<votre-mac>.local:8770 sur le téléphone
```

Prochain passage en grand sur la maquette pixel-art, direction « Depuis / Vers
Matabiau » avec le côté où regarder, compte à rebours piloté par l'heure
d'annonce (jamais en retard), passage suivant en bas, bouton « Il passe ! »
qui journalise une observation à ±3 s, et bottom sheet avec l'histogramme
semaine / week-end à échelle commune. Le serveur rafraîchit le temps réel
toutes les 90 s et journalise ses relevés — la collecte pour `coverage`
devient automatique. Conception détaillée : [`docs/app-v0.md`](docs/app-v0.md).

### Le serveur en continu

En usage réel, `serve` tourne en permanence plutôt que dans un terminal ouvert :
un `LaunchAgent` macOS (`~/Library/LaunchAgents/com.nexttraintosee.serve.plist`)
le démarre à l'ouverture de session et le relance seul en cas d'arrêt. Ses
journaux vivent dans `logs/serve.log` et `logs/serve.err.log`.

```bash
launchctl print gui/$(id -u)/com.nexttraintosee.serve   # état
tail -f logs/serve.log                                   # journal en direct
launchctl kickstart -k gui/$(id -u)/com.nexttraintosee.serve  # redémarrer
launchctl bootout gui/$(id -u)/com.nexttraintosee.serve  # arrêter définitivement
```

### Accès hors Wi-Fi domestique

Le téléphone doit pouvoir joindre l'app même loin de la maison, en données
mobiles. Sans IPv4 dédiée (le cas courant avec les box françaises — le FAI
partage l'adresse entre plusieurs foyers), ouvrir un port sur la box ne
suffit pas : la solution retenue est [Tailscale](https://tailscale.com), un
réseau privé chiffré entre les appareils, gratuit en usage personnel, sans
rien exposer publiquement.

Une fois l'app installée et connectée sur le Mac et sur le téléphone (même
compte), le nom stable à utiliser depuis le téléphone, Wi-Fi coupé ou non, est :

```
http://macbook-air-de-benjamin.tail04145f.ts.net:8770
```

Le serveur écoute déjà sur toutes les interfaces (`0.0.0.0`) : aucune
configuration supplémentaire n'est nécessaire côté application, Tailscale
ajoute simplement un chemin réseau vers la machine.

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

Le noyau est écrit et testé (`python -m pytest`, 377 tests, sans réseau), et la
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
