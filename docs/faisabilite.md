# Peut-on prédire le passage des trains à 43°35'52.2"N 1°27'29.5"E ?

Réponse : **oui pour les trains de voyageurs, non pour le fret**, et la
précision atteignable dépend surtout d'une chose — la qualité du modèle de
marche entre la gare et le point, qu'un capteur local permet de recaler.

Ce document explique le raisonnement, chiffre l'erreur attendue, et liste
honnêtement ce qui n'a pas pu être vérifié.

---

## 1. Où est ce point

43°35'52.2"N 1°27'29.5"E = **43.597833, 1.458194**.

Distances et caps depuis le point (calculés, pas estimés) :

| Repère | Distance | Cap depuis le point |
| --- | ---: | ---: |
| Port Saint-Sauveur (canal du Midi) | 0,44 km | 287° |
| Grand Rond | 0,68 km | 268° |
| Pont des Demoiselles | 1,32 km | 182° |
| Faisceau de Toulouse-Raynal | 1,33 km | 13° |
| **Gare de Toulouse-Matabiau** | **1,53 km** | **346°** |
| Gare de Toulouse Saint-Agne | 2,11 km | 198° |
| Halte de Toulouse-Montaudran | 3,75 km | 150° |

Le point est donc à **1,5 km au sud-sud-est de Matabiau**, juste au sud du
quartier de Guilheméry — c'est-à-dire dans la zone où le faisceau sortant de la
gare, après les tunnels jumeaux de Guilheméry, se sépare entre :

* l'**axe sud-est** vers Montaudran, Villefranche-de-Lauragais, Castelnaudary,
  Carcassonne, Narbonne — la ligne de Bordeaux-Saint-Jean à Sète-Ville ;
* l'**axe sud** vers Toulouse Saint-Agne, Portet-Saint-Simon, puis Auch, Foix et
  Latour-de-Carol.

Les deux axes passent au sud de la gare, à quelques centaines de mètres l'un de
l'autre à cette latitude. **C'est une très bonne position d'observation**, et
c'est aussi ce qui rend indispensable l'étape `tracks` : selon que le point est
à 80 m d'un seul axe ou à mi-distance des deux, la configuration n'est pas la
même.

> ⚠️ Cette lecture s'appuie sur la géographie ferroviaire toulousaine et sur des
> calculs de distance, **pas** sur la géométrie OpenStreetMap : l'environnement
> dans lequel ce code a été écrit n'a accès ni à Overpass, ni à OSM, ni aux
> portails SNCF. Voir [§ 6](#6-ce-qui-reste-à-vérifier).

---

## 2. Les sources ouvertes, et ce qu'elles couvrent

### Horaires théoriques — GTFS SNCF

Publiés sur [transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/horaires-sncf),
en libre accès et sans clé, pour environ 150 jours glissants. Couvre TER,
Intercités et TGV.

Deux caractéristiques structurent tout le reste :

1. **Les horaires sont donnés aux gares, jamais en pleine voie.** Il n'existe
   aucun flux ouvert donnant l'heure de passage à un point kilométrique
   quelconque. C'est le cœur du problème à résoudre.
2. **Le GTFS SNCF ne fournit pas de `shapes.txt` exploitable pour le rail.** On
   n'a donc pas la géométrie du parcours côté horaires — d'où le recours à OSM.

### Temps réel — GTFS-RT

Deux flux, sans clé :

* `TripUpdates` — <https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates>
  Retards par circulation et par arrêt, rafraîchis environ toutes les 2 minutes,
  sur un horizon de l'ordre de l'heure.
* `ServiceAlerts` — <https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts>
  Perturbations et suppressions.

Un détail de la spécification qui compte : un retard n'est pas republié à chaque
arrêt, il **se propage** aux arrêts suivants jusqu'à la prochaine mise à jour.
`realtime.resolve_delay` applique cette règle plutôt que de conclure « pas de
donnée » dès qu'un arrêt est absent du flux.

### Alternative — Navitia / api.sncf.com

Une [clé gratuite](https://doc.navitia.io/) donne accès aux prochains départs,
aux dessertes complètes et au temps réel, avec une limite d'une requête par
seconde. Plus commode pour un usage ponctuel, moins pour un service qui tourne
en continu : le GTFS local évite d'appeler une API distante à chaque calcul.

### Géométrie — OpenStreetMap

Le réseau ferré français est très bien cartographié dans OSM : voies tracées une
par une, vitesses limites, électrification, relations de ligne. C'est ce qui
permet de mesurer une distance **le long de la voie** — et non à vol d'oiseau,
ce qui serait faux dès que la ligne courbe, comme en sortie de gare.

### Ce qu'aucune source ouverte ne donne

* **La position en direct d'un train.** Il n'y a pas d'équivalent ferroviaire à
  l'ADS-B aérien. On ne peut que dater un passage, jamais l'observer à distance.
* **Le fret.** Les sillons fret ne figurent dans aucun flux public. À 1,3 km du
  faisceau de Raynal, ce n'est pas anecdotique.
* **Les haut-le-pied et les trains de travaux.** Idem — et les seconds circulent
  surtout la nuit, quand le trafic voyageurs s'arrête.

---

## 3. Comment on date un passage en pleine voie

### Le principe : la gare d'appui

Toute circulation qui passe devant le point a nécessairement desservi (ou
traversé) Matabiau juste avant ou juste après. On part donc de l'horaire en
gare, et on ajoute — ou retranche — le temps de parcours sur 1,5 km.

### Choisir la bonne branche

Un train qui quitte Matabiau vers Bordeaux ne passe pas devant le point ; un
train qui la quitte vers Narbonne, oui. Comment le savoir à partir du GTFS ?

**Par le cap vers l'arrêt voisin.** Depuis Matabiau :

| Vers | Cap | Passe devant le point ? |
| --- | ---: | --- |
| Villefranche-de-Lauragais (axe Narbonne) | 137,5° | oui |
| Toulouse Saint-Agne (axe Latour-de-Carol) | 184,2° | oui (à confirmer) |
| Colomiers (axe Auch / Bayonne) | 269,8° | non |
| Montauban (axe Bordeaux) | 348,6° | non |

Les quatre secteurs sont largement séparés : un simple secteur angulaire autour
de chaque cap suffit à rattacher n'importe quelle circulation à sa branche, sans
avoir à énumérer les gares une par une. C'est robuste aux évolutions de
desserte, et ça se configure en quatre lignes de TOML.

### Trois régimes de marche, pas un

C'est là que se joue l'essentiel de la précision. Sur 1,5 km, la différence
entre un train qui démarre et un train qui passe à vitesse de ligne dépasse
**25 secondes** :

| Régime | Situation | Temps sur 1 534 m |
| --- | --- | ---: |
| `DEPARTING` | quitte Matabiau, accélère | 86 s |
| `ARRIVING` | freine pour s'arrêter à Matabiau | 82 s |
| `THROUGH` | traverse sans arrêt commercial | 61 s |

*(profil de référence : 0,5 m/s² en accélération, 0,6 m/s² en freinage, 90 km/h
de vitesse limite)*

Le modèle est trapézoïdal — rampe, palier, rampe. Sur 1 à 3 km en zone urbaine,
c'est largement suffisant ; raffiner davantage sans données de terrain serait de
la fausse précision.

### Le retard

Le retard publié **à la gare d'appui** décale l'ensemble. C'est bien celui-là
qu'il faut lire, pas le retard au terminus : un train peut rattraper ou perdre
du temps en aval sans que cela change son heure de passage ici.

---

## 4. Budget d'erreur

| Source d'erreur | Ordre de grandeur | Réductible ? |
| --- | ---: | --- |
| Modèle de marche (accélération, vitesse réelle) | ±15 s | **oui** — par recalage capteur |
| Distance gare → point (si estimée à vol d'oiseau) | ±10 s | **oui** — par `tracks` (géométrie OSM) |
| Fraîcheur du temps réel (flux à 2 min) | ±5 à 30 s | non |
| Arrondi des horaires GTFS à la minute | ±30 s | partiellement, via le temps réel |
| Voie empruntée dans le faisceau | ±2 s | négligeable |

**Sans rien faire** : la prédiction théorique seule donne une fenêtre de l'ordre
de ±45 s — largement suffisant pour « il y en a un dans 4 minutes », insuffisant
pour déclencher un appareil photo.

**Avec le temps réel** : ±20 s.

**Avec le temps réel et un capteur recalé** : ±5 s. Le recalage est ce qui fait
la différence, parce qu'il attaque les deux plus gros postes du tableau — et
qu'il les mesure au lieu de les supposer.

---

## 5. Ce que ce système ne saura jamais faire seul

* **Voir arriver un train de fret.** Il n'est dans aucune donnée ouverte.
* **Distinguer les voies d'un même faisceau.** Deux voies parallèles espacées de
  4 m sont, du point de vue de la prédiction, le même endroit.
* **Prédire un train qui traverse Matabiau sans y figurer.** Si une circulation
  n'a aucun `stop_time` à la gare d'appui, elle est invisible pour la méthode.
  En pratique, c'est rare pour les voyageurs à Matabiau.
* **Anticiper une déviation de dernière minute** non reflétée dans le GTFS-RT.

C'est exactement le périmètre que le capteur vient compléter : `calibrate` liste
séparément les passages observés sans prédiction correspondante. Sur plusieurs
jours, un « train fantôme » qui revient toujours à la même heure le même jour
est presque sûrement un sillon fret régulier — et rien d'autre ne permet de le
savoir.

---

## 6. Ce qui reste à vérifier

L'environnement de rédaction n'avait accès ni à Overpass, ni à OpenStreetMap, ni
aux portails SNCF : la géométrie exacte des voies au droit du point **n'a pas pu
être mesurée**. Trois valeurs de la configuration sont donc des estimations,
explicitement marquées « À MESURER » / « À VÉRIFIER » :

1. **`track_distance_m` de chaque branche** — distance *le long de la voie*
   entre Matabiau et le point. L'estimation actuelle (1 650 m) est la distance à
   vol d'oiseau (1 534 m) majorée d'une sinuosité de 5 %. La vraie valeur peut
   en différer de 100 à 200 m en sortie de gare, soit 5 à 10 s.
2. **`passes_observer` de la branche `sud`** — l'axe de Latour-de-Carol
   passe-t-il réellement devant ce point, ou s'écarte-t-il vers l'ouest avant ?
   Si la réponse est non, il faut le passer à `false`, sinon la moitié des
   prédictions seront fausses.
3. **La vitesse limite locale** — 90 km/h est une hypothèse prudente pour une
   sortie de gare urbaine.

Une seule commande règle les trois :

```bash
nexttraintosee tracks -c config/toulouse-guilhemery.toml
```

Elle interroge Overpass (puis met en cache, la géométrie ne bouge pas), regroupe
les voies en corridors parallèles, et affiche pour chacun sa distance au point,
son axe, son nombre de voies, sa vitesse limite, et la distance curviligne
jusqu'à Matabiau — soit très exactement les valeurs à reporter dans le TOML.

À défaut, la même requête à la main sur <https://overpass-turbo.eu/> :

```overpassql
[out:json][timeout:90];
(
  way(around:500,43.597833,1.458194)["railway"~"^(rail|light_rail|narrow_gauge)$"];
  node(around:2000,43.597833,1.458194)["railway"~"^(station|halt)$"];
);
out tags geom;
```

---

## Sources

* [Réseau SNCF TGV, Intercités et TER — jeux de données ouverts (GTFS, GTFS-RT, NeTEx, SIRI)](https://transport.data.gouv.fr/datasets/horaires-sncf)
* [Documentation Navitia / api.sncf.com](https://doc.navitia.io/)
* [Réseau ferroviaire de Toulouse — Wikipédia](https://fr.wikipedia.org/wiki/R%C3%A9seau_ferroviaire_de_Toulouse)
* [Gare de Toulouse-Matabiau — Wikipédia](https://fr.wikipedia.org/wiki/Gare_de_Toulouse-Matabiau)
* [API Overpass — OpenStreetMap](https://overpass-api.de/)
