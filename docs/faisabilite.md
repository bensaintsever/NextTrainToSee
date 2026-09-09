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

### Ce que la géométrie OSM a confirmé

Une exécution de `tracks` sur le terrain relève **trois corridors parallèles**
dans un rayon de 500 m — à 1 m, 383 m et 488 m du point, tous orientés
nord-sud, tous limités à 120 km/h — nommés « Ligne de Bordeaux-Saint-Jean à
Sète-Ville » et « Ligne de Toulouse à Bayonne ».

C'est cohérent avec la description du réseau : au sud de Matabiau, la ligne de
Saint-Agne à Auch « descend vers le sud sur environ deux kilomètres aux côtés
d'autres lignes, dont elle se sépare aux bifurcations près du Grand-Rond et
d'Empalot ». Le Grand-Rond est à 680 m du point.

**Conséquence pratique : le point est dans le tronc commun.** Tout ce qui quitte
Matabiau vers le sud passe devant — Narbonne, Latour-de-Carol, Bayonne, *et*
Auch/Colomiers. Seuls les axes nord (Bordeaux, Montauban) ne passent pas.

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
| Toulouse Saint-Agne (axe Latour-de-Carol, Bayonne, Auch) | 184,2° | oui |
| Colomiers, en desserte directe (axe Auch) | 269,8° | **oui** |
| Montauban (axe Bordeaux) | 348,6° | non |

Les quatre secteurs sont largement séparés : un simple secteur angulaire autour
de chaque cap suffit à rattacher n'importe quelle circulation à sa branche, sans
avoir à énumérer les gares une par une. C'est robuste aux évolutions de
desserte, et ça se configure en quatre lignes de TOML.

**Le piège de la méthode**, et il est réel ici : le cap vers l'arrêt voisin n'est
la direction de départ que si cet arrêt est dans le prolongement de la voie.
Colomiers est plein **ouest** de Matabiau, mais les trains qui s'y rendent
partent vers le **sud** — la ligne de Saint-Agne à Auch descend avec les autres,
bifurque à l'ouest après Saint-Agne, puis remonte vers Saint-Cyprien-Arènes. Une
desserte directe Matabiau → Colomiers affiche donc un cap de 270° tout en
passant devant le point. D'où `passes_observer = true` sur cette branche, malgré
son cap. La règle à retenir : **c'est la géométrie mesurée par `tracks` qui
tranche, pas le cap.**

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

## 6. Régler la configuration à partir de `tracks`

`nexttraintosee tracks` interroge Overpass, regroupe les voies en corridors
parallèles, mesure pour chacun la distance **le long de la voie** jusqu'à la
gare d'appui, détermine vers où il repart, et imprime enfin le bloc
`[[branches]]` prêt à coller :

```bash
nexttraintosee tracks -c config/toulouse-guilhemery.toml
```

Deux garde-fous y sont intégrés, tous deux nés d'erreurs constatées :

* **Distance non mesurable.** Une distance sur la voie ne peut pas être plus
  courte qu'à vol d'oiseau. Quand la géométrie récupérée n'atteint pas la gare,
  la projection est bornée à l'extrémité de la polyligne et la valeur devient
  absurde. L'outil le détecte et refuse de publier le chiffre au lieu de le
  présenter comme mesuré. La requête Overpass balaie désormais tout le couloir
  entre le point et la gare, ce qui évite le cas dans la quasi-totalité des
  situations ; si le message apparaît malgré tout, augmentez `search_radius_m`
  et relancez avec `--refresh`.
* **Chaînes qui changent de ligne.** Dans une gare, toutes les lignes partagent
  des nœuds : une polyligne reconstituée naïvement traverse la gare et repart
  sur la ligne voisine. Chaque corridor est donc amorcé sur ses propres voies,
  puis prolongé uniquement par les tronçons qui le continuent sans virage
  brusque.

Deux choses restent à votre appréciation, parce qu'aucune donnée ne les décide :

1. **`passes_observer` pour un corridor éloigné.** L'outil dit qu'un corridor
   est à 488 m ; savoir si vous le voyez depuis votre position — bâti, végétation,
   tranchée — n'appartient qu'à vous. Dans le doute, laissez `true` : le capteur
   tranchera, une branche à tort visible produit des prédictions qui n'arrivent
   jamais et ressort dans `unmatched_passages`.
2. **Le nombre réel de voies.** L'outil affiche des *tronçons OSM*, pas des
   voies physiques : OSM découpe une même voie à chaque pont ou changement de
   vitesse. La largeur du faisceau est plus parlante, et elle est affichée aussi.

## Sources

* [Réseau SNCF TGV, Intercités et TER — jeux de données ouverts (GTFS, GTFS-RT, NeTEx, SIRI)](https://transport.data.gouv.fr/datasets/horaires-sncf)
* [Documentation Navitia / api.sncf.com](https://doc.navitia.io/)
* [Réseau ferroviaire de Toulouse — Wikipédia](https://fr.wikipedia.org/wiki/R%C3%A9seau_ferroviaire_de_Toulouse)
* [Gare de Toulouse-Matabiau — Wikipédia](https://fr.wikipedia.org/wiki/Gare_de_Toulouse-Matabiau)
* [API Overpass — OpenStreetMap](https://overpass-api.de/)
