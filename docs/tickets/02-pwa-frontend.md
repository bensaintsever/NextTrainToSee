# Ticket 02 — PWA (webapp/)

Référence : `docs/app-v0.md` (UI § 3, contrat § 4 — figé, sémantique § 5,
règles anti-biais § 6, histogramme § 7). Modèle exécutant : sonnet.

## Objectif

Le dossier `webapp/` complet : une page unique, installable sur l'écran
d'accueil, fond `assets/toulouse.jpg` plein écran, surcouches vivantes.

## Travaux

1. `index.html`, `app.css`, `app.js` — **vanilla, sans build, sans framework,
   sans CDN de librairies** (la police Google Fonts est permise, avec repli
   `monospace` obligatoire).
2. Zone haute (ciel) : heure du prochain passage en très grand, `±Ns`,
   compte à rebours « guette dès HH:MM:SS » piloté par `announce_at`,
   direction avec flèche et libellé (« ▼ From Matabiau · il sort du tunnel » /
   « ▲ To Matabiau · il arrive du sud »), n° et destination du train, badge
   « TR » ou « horaire théorique » selon `realtime`, retard s'il existe
   (`+5 min`). État « imminent » (pulsation discrète) entre
   `announce_at - 30 s` et `when + uncertainty_s`.
3. Zone basse (voie) : passage suivant en petit (« puis 23:02 · To Matabiau »),
   bouton « 🚆 Il passe ! », bouton « 📊 ».
4. Observation (§ 6, à la lettre) : appui « Il passe ! » → POST immédiat,
   retour visuel (confirmation, `bound_to`/`ambiguous`) ; carte « le train de
   HH:MM est-il passé ? » après `when + uncertainty + 90 s` sans appui, avec
   Oui / Non ; « Non » ouvre une bottom sheet : heure réelle (champ time,
   précision 30 s) ou « non passé ». Ignorée, la carte disparaît après 10 min
   sans rien envoyer.
5. Bottom sheet histogramme (§ 7) : onglets Semaine / Week-end, barres
   horizontales par heure **à échelle commune** (`peak`), valeurs affichées,
   phrase de comparaison calculée. Fermeture par glissement ou toucher hors
   zone. Rendu en HTML/CSS (pas de canvas, pas de librairie).
6. PWA : `manifest.webmanifest` (icônes `assets/icon-512.png`,
   `assets/icon-180.png`, `display: standalone`, orientation portrait),
   balises iOS (`apple-touch-icon`, `apple-mobile-web-app-capable`),
   `sw.js` minimal (cache du statique uniquement) enregistré seulement si
   `window.isSecureContext`.
7. États dégradés (§ 3) : API muette, temps réel absent, aucun passage la nuit.
   Rafraîchissement `/api/next` toutes les 30 s, horloge locale à la seconde.

## Contraintes

- Esthétique : assortie au pixel-art (police pixel, couleurs sombres du ciel,
  pas de matériau « flat design » qui jurerait). Textes lisibles sur l'image
  (ombre nette + bandeau translucide).
- Les surcouches ne recouvrent jamais le conducteur ni la face du train
  (tiers central de l'image).
- Pour développer sans serveur : si `/api/next` échoue ET que l'URL contient
  `?mock=1`, charger `assets/mock.json` (à créer, fidèle au contrat, avec un
  passage TER outbound + un inbound grandes-lignes). C'est aussi la démo.
- Ne pas committer. Ne toucher qu'à `webapp/`. `assets/toulouse.jpg`,
  `icon-512.png`, `icon-180.png` existent déjà — ne pas les régénérer.
