# Ticket 01 — Couche service partagée et serveur HTTP

Référence : `docs/app-v0.md` (le contrat § 4 fait foi). Modèle exécutant : sonnet.

## Objectif

Une commande `nexttraintosee serve [--port 8770]` qui sert `webapp/` et expose
l'API du contrat, sans aucune dépendance hors bibliothèque standard.

## Travaux

1. **`src/nexttraintosee/service.py`** — logique partagée CLI/serveur :
   - Extraire de `cli.cmd_observe` le rattachement d'une observation au passage
     prédit le plus proche (recherche des candidats, règle d'ambiguïté quand
     deux candidats sont à portée comparable). `cmd_observe` doit consommer
     cette fonction : une seule implémentation.
   - Une classe `PassageService` qui encapsule : chargement du feed,
     rafraîchissement périodique (GTFS-RT + recalcul + `record_passages`,
     comme `cli._collect_passages`), cache des passages, calcul de
     l'histogramme moyenné semaine/week-end (5 jours ouvrés + 2 jours de
     week-end couverts par le flux). Thread de rafraîchissement démarrable et
     arrêtable proprement (pour les tests).
2. **`src/nexttraintosee/server.py`** — `ThreadingHTTPServer` + handler :
   - Routes du contrat (`/api/next`, `/api/histogram`, `/api/observe`,
     `/api/health`), statique depuis `webapp/` (MIME corrects, interdiction de
     sortir du dossier — rejeter les chemins contenant `..` après résolution).
   - Erreurs en JSON (`{"error": "…"}`), 400 sur corps invalide, 404 sinon.
   - Journalisation sobre : une ligne par requête API, rien pour le statique.
3. **`cli.py`** — sous-commande `serve` (`--port`, `--host` défaut
   `0.0.0.0`, `--refresh-every` défaut 90 s). Affiche au démarrage l'URL à
   ouvrir depuis le téléphone (`http://<hostname>.local:<port>`).
4. **Tests** (`tests/test_service.py`, `tests/test_server.py`) : sans réseau,
   avec le mini-GTFS de `tests/conftest.py`. Démarrer le serveur sur le port 0,
   requêter via `http.client` ou `urllib`. Couvrir : contrat de chaque route
   (champs, tri, libellés de direction), observe lié / ambigu / non vu,
   histogramme à échelle commune (`peak`), traversée de chemin refusée, 400/404.

## Contraintes

- Zéro dépendance nouvelle. Docstrings et messages en français, code en
  anglais, style des modules existants (regarder `store.py`, `cli.py`).
- Le temps réel absent ne doit jamais faire tomber le serveur : repli
  théorique, `realtime: false` (voir `cli._collect_passages`).
- Ne pas committer. Ne pas toucher à `webapp/` (un autre agent y travaille).
- `python -m pytest` doit passer intégralement à la fin, y compris l'existant.
