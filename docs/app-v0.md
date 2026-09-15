# NextTrainToSee — application v0 (Toulouse)

Conception de référence. Les tickets de `docs/tickets/` renvoient ici : ce
document fait foi pour le contrat d'API et la sémantique ; en cas de doute, il
gagne.

## 1. Architecture : une PWA servie par un serveur local

Une PWA suffit — mais **pas une PWA seule**. Le calcul des passages exige le
GTFS complet (~100 Mo à dépouiller), le décodage protobuf du temps réel, la
géométrie OSM et le modèle de marche : rien de tout cela n'est raisonnable dans
un navigateur, et les flux SNCF ne sont pas servis avec les en-têtes CORS qu'il
faudrait. Or tout ce pipeline existe déjà, validé et testé, dans le paquet
Python.

L'architecture est donc :

```
téléphone ──http──▶  nexttraintosee serve  (Mac, même Wi-Fi)
   PWA                 ├── sert webapp/ (statique)
                       ├── /api/*  (JSON, même origine → pas de CORS)
                       └── boucle de fond : GTFS-RT toutes les 90 s,
                           passages recalculés, relevés journalisés
```

Un utilisateur, un serveur, zéro dépendance nouvelle : le serveur repose sur
`http.server` de la bibliothèque standard (ThreadingHTTPServer), fidèle au
principe « noyau sans dépendance » du projet.

Accès depuis le téléphone : `http://<mac>.local:8770`. L'installation sur
l'écran d'accueil passe par « Ajouter à l'écran d'accueil » (manifest + balises
iOS). Le service worker ne s'enregistre qu'en contexte sécurisé : sur http
local il est simplement absent, et l'app fonctionne à l'identique — c'est
assumé pour la v0.

## 2. Périmètre v0

- Site unique : Toulouse (`config/toulouse-guilhemery.toml`).
- Prochain passage en grand, le suivant en petit dessous.
- Direction : « Depuis Matabiau » / « Vers Matabiau », avec le côté où regarder.
- Journalisation des passages : vu / non vu / heure réelle (cf. § 6).
- Bouton ouvrant une bottom sheet avec l'histogramme semaine / week-end.
- Fond d'écran : `webapp/assets/toulouse.jpg` (1205×2160), l'heure apposée dessus.

Hors périmètre v0 : multi-site (Bordeaux attendra), notifications push, HTTPS,
comptes, capteur audio.

## 3. UI — zones sur l'image

L'image est affichée plein écran (`object-fit: cover`, centrée). Les textes
sont des **surcouches HTML positionnées**, jamais incrustés dans l'image : ils
doivent rester nets et vivants. Trois zones, choisies parce que l'illustration
y est calme :

```
┌─────────────────────────┐
│  ZONE HAUTE (le ciel)   │  heure du prochain passage, très grande,
│  22:47:43   ±20 s       │  police pixel ; dessous : compte à rebours
│  Depuis Matabiau        │  « Guette dans MM:SS » + direction + n° train
├─────────────────────────┤
│                         │
│   (illustration libre : │
│    train, conducteur)   │
│                         │
├─────────────────────────┤
│  ZONE BASSE (la voie)   │  passage d'après, en petit :
│  puis 23:02 · To Matab. │  heure + direction abrégée
│  [🚆 Il passe !] [📊]   │  bouton observation + bouton histogramme
└─────────────────────────┘
```

- Police : « Press Start 2P » (Google Fonts) avec repli `monospace` — l'app
  doit rester lisible sans réseau vers Google.
- Lisibilité sur image : textes blancs, `text-shadow` net + léger bandeau
  `rgba(0,0,0,.35)` derrière les blocs.
- Direction (cf. § 5) : « Depuis Matabiau » s'accompagne de « sort du tunnel »
  (le tunnel est en haut de l'illustration, côté Matabiau) ; « Vers Matabiau »
  de « arrive du sud ». Texte seul, sans flèche (retour utilisateur : la
  flèche devant le libellé n'apportait rien que le texte ne dise déjà).
- Compte à rebours piloté par `announce_at`, pas par `when` : annoncer tard
  fait rater le train, annoncer tôt ne coûte rien. Quand
  `now ≥ announce_at - 30 s`, la zone haute passe en état « imminent »
  (pulsation discrète) jusqu'à `when + uncertainty_s`.
- États dégradés, tous prévus : API injoignable (« — » + bandeau discret),
  temps réel absent (`realtime: false` → mention « horaire théorique »),
  aucun passage dans l'horizon (nuit : « prochain train à HH:MM » du matin).
- Rafraîchissement : `/api/next` toutes les 30 s (le serveur recalcule
  toutes les 90 s de toute façon) ; l'horloge du compte à rebours tourne en
  local à la seconde.

## 4. Contrat d'API (figé pour la v0)

Toutes les réponses en JSON UTF-8, dates en ISO 8601 avec fuseau.

### GET /api/next?limit=N  (défaut 2, max 10)

```json
{
  "generated_at": "2026-09-09T22:41:07+02:00",
  "site": "Toulouse — sud de Matabiau (Guilheméry / Port Saint-Sauveur)",
  "realtime": true,
  "passages": [
    {
      "trip_id": "OCESN876255…",
      "when": "2026-09-09T22:47:43+02:00",
      "announce_at": "2026-09-09T22:47:10+02:00",
      "uncertainty_s": 20.0,
      "direction": "outbound",
      "direction_label": "Depuis Matabiau",
      "look": "tunnel",
      "branch_id": "se",
      "branch_label": "Axe Narbonne / Sète (via Montaudran)",
      "category_id": "ter",
      "headsign": "876255",
      "route_label": "Toulouse Matabiau - Narbonne",
      "delay_s": 300,
      "realtime": true,
      "speed_kmh": 66.0
    }
  ]
}
```

- `direction_label` : `outbound` → « Depuis Matabiau », `inbound` → « Vers Matabiau ».
- `look` : `outbound` → `"tunnel"`, `inbound` → `"sud"`. Le client n'infère
  rien : le serveur donne les libellés.
- `realtime` (par passage) : vrai si `delay_s` n'est pas null. Au niveau
  racine : vrai si le dernier rafraîchissement GTFS-RT a réussi.
- Les passages sont triés par `when` croissant, fenêtre de 12 h.

### GET /api/histogram

Moyennes par heure sur les 5 prochains jours ouvrés et les 2 prochains jours
de week-end couverts par le flux, plage 05 h → 23 h (bornes du projet).

```json
{
  "first_hour": 5,
  "last_hour": 23,
  "peak": 18.0,
  "weekday": { "label": "Semaine", "days": 5, "hours": [ {"hour": 5, "total": 2.0}, … ] },
  "weekend": { "label": "Week-end", "days": 2, "hours": [ … ] }
}
```

`peak` est le maximum des deux séries : **les deux histogrammes partagent la
même échelle**, sinon le dimanche paraîtrait aussi chargé qu'un mardi.
Calculé une fois au démarrage puis mis en cache (les horaires théoriques ne
bougent pas en cours de journée).

### POST /api/observe

Corps, deux formes :

```json
{ "seen": true,  "observed_at": "2026-09-09T22:47:51+02:00",
  "precision_s": 3, "source": "app" }
{ "seen": false, "anchor": "2026-09-09T22:47:43+02:00", "source": "app" }
```

Champ optionnel commun aux deux formes : **`trip_id`**, la circulation que
l'observateur désigne explicitement.

```json
{ "seen": true, "observed_at": "…", "precision_s": 30,
  "source": "app:heure-saisie", "trip_id": "OCESN876255…" }
```

Réponse :

```json
{ "recorded": true, "id": 42,
  "bound_to": { "trip_id": "…", "when": "…", "headsign": "…" },
  "ambiguous": false, "binding_method": "designated", "gap_s": 8.0 }
```

Deux rattachements, jamais mélangés — `binding_method` dit lequel a servi :

- **`"designated"`** — `trip_id` fourni : le serveur cherche cette circulation
  dans une fenêtre de 30 min et s'y tient. Si elle n'y est pas, l'observation
  est enregistrée **sans rattachement** (`bound_to: null`, méthode `"none"`) ;
  elle ne retombe *jamais* sur le rattachement par l'heure. C'est délibéré :
  une désignation qui échoue signale une erreur d'identifiant, et deviner à sa
  place recréerait exactement les attributions croisées constatées le 14/09.
- **`"nearest"`** — pas de `trip_id` : rattachement au passage prédit le plus
  proche, **refusé quand deux candidats sont à portée comparable**
  (`ambiguous: true`, `bound_to: null`) — une observation mal attribuée fausse
  le recalage bien plus qu'une observation ignorée.

Quand l'observateur ne peut pas distinguer deux trains de même type et de même
sens (le cas depuis la passerelle), le client **ne désigne pas** : mieux vaut
un rattachement que le serveur refuse qu'un rattachement confiant et faux.
C'est pourquoi « Il passe ! » n'envoie pas de `trip_id`, là où la carte
différée — qui nomme le train à l'écran — le fait.

`id` identifie la ligne enregistrée, y compris quand `ambiguous` est vrai —
l'observation est toujours conservée, seul son rattachement à une circulation
précise est refusé. Le client s'en sert pour permettre d'y revenir (§ 6) :
sans cela, un appui accidentel sur « Il passe ! » n'a aucun rattrapage possible.

### DELETE /api/observe/{id}

Annule une observation, par exemple pendant la fenêtre de « Annuler » qui suit
un appui sur « Il passe ! ». Sans corps.

Réponse :

```json
{ "deleted": true }
```

`deleted: false` pour un identifiant inconnu, déjà supprimé, ou appartenant à
un autre site — jamais une erreur : un « Annuler » relancé deux fois doit
rester sans effet, pas échouer.

### GET /api/health

```json
{ "ok": true, "feed_start": "2026-09-08", "feed_end": "2027-02-28",
  "realtime_age_s": 42.0, "last_refresh": "…", "passages_cached": 61 }
```

### Statique

`GET /` → `webapp/index.html` ; tout chemin sans `/api/` est servi depuis
`webapp/` (types MIME corrects, refus de sortir du dossier).

## 5. Sémantique de la direction

Le point d'observation est au sud de Matabiau, sur le tronc commun.

| `direction` | Sens physique | Libellé | D'où le train surgit |
|---|---|---|---|
| `outbound` | s'éloigne de Matabiau vers le sud | **Depuis Matabiau** | du tunnel (nord) |
| `inbound` | remonte vers Matabiau | **Vers Matabiau** | du sud |

## 6. Journalisation des passages — règles anti-biais

Elles découlent de l'analyse faite en amont (voir historique du dépôt) et ne
sont pas négociables en v0 :

1. **Le bouton « Il passe ! » est l'instrument principal.** Un appui au
   moment du passage = `seen`, `observed_at = maintenant`, `precision_s = 3`.
   C'est la donnée la plus précise qu'un humain puisse fournir.
2. **Après** `when + uncertainty_s + 90 s`, si aucun appui n'a eu lieu, une
   carte discrète apparaît : « Train annoncé à 22:47 — tu l'as vu passer ? »,
   avec l'identité du train dessous (catégorie, destination), sans quoi on ne
   peut pas savoir de quelle circulation la carte parle.
   - « Oui, à… » → **n'envoie rien** : ouvre la saisie de l'heure, champ
     **vide**, et c'est la saisie qui enregistre (`precision_s = 30`,
     `source = app:heure-saisie`, désignée par `trip_id`).
   - « Non, rien vu » → `seen: false` directement, sans détour par la saisie :
     une absence de passage n'a pas d'heure à saisir.
   - « Je ne sais plus » → **aucune donnée**, et la question est close.
   - Ignorer la carte = **aucune donnée** non plus ; elle expire seule.
3. **Aucun bouton n'écrit une heure que l'observateur n'a pas dite.** Deux
   pré-remplissages sont explicitement interdits, chacun pour une raison
   différente :
   - l'heure **prédite** — la valider donne un écart nul *par construction*,
     et écrase la mesure réelle (constaté : le 15/09, un train vu 30 s en
     avance enregistré à `ecart_s = 0.0`) ;
   - l'heure **courante** — la carte peut arriver dix minutes après le
     passage, donc « maintenant » n'est pas « quand il est passé ».

   La seule exception est le bouton « Je n'ai pas noté l'heure » de la saisie,
   qui envoie bien l'heure prédite mais sous `source = app:confirmation` : le
   recalage l'écarte (voir `NON_TIMING_SOURCES`), elle ne sert qu'à attester
   que la circulation a eu lieu.
4. La carte concerne le dernier passage écoulé uniquement, et disparaît
   d'elle-même après 10 minutes. Refermer la saisie d'un glissement sans
   répondre **ramène la carte** : un geste d'échappement ne répond pas.
5. **Toute observation envoyée reste réversible quelques secondes.** Un appui
   accidentel est une source d'erreur réelle (constatée : un « Il passe ! »
   pressé par erreur en testant l'app), et il n'y a aucun moyen de distinguer
   côté serveur une vraie observation d'un test. Le toast de confirmation porte
   donc un bouton « Annuler » qui appelle `DELETE /api/observe/{id}` — voir § 4.

## 7. Histogramme (bottom sheet)

- Ouverte par le bouton 📊, fermée par glissement ou toucher hors zone.
- Deux onglets : **Semaine** / **Week-end** — même échelle (`peak`), barres
  horizontales par heure, valeur affichée, style pixel cohérent avec le fond.
- Sous l'onglet Week-end, une ligne : « le dimanche, ~2× moins de trains
  qu'en semaine » (calculée, pas codée en dur).

## 8. Répartition du travail

| Ticket | Contenu | Agent | Modèle |
|---|---|---|---|
| 01 | Couche service partagée + serveur HTTP + tests | dédié | sonnet |
| 02 | PWA (webapp/) contre le contrat § 4 | dédié | sonnet |
| 03 | Intégration, test bout-en-bout, docs, commit | chef de projet | fable |

Les deux premiers travaillent en parallèle : le contrat § 4 est figé, leurs
fichiers sont disjoints (`src/` + `tests/` pour 01, `webapp/` pour 02), et
aucun des deux ne committe — l'intégration est faite en 03.
