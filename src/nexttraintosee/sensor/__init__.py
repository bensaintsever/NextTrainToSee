"""Capteurs de passage : ce que les horaires ne disent pas.

Les horaires publics ne couvrent que les circulations commerciales. Un point
d'observation situé près d'un nœud ferroviaire voit aussi passer du fret, des
trains vides en haut-le-pied, des engins de travaux — invisibles dans le GTFS.

Un capteur local sert deux fins :

* **calibrer** le modèle de marche sur des passages réellement observés, ce qui
  ramène l'erreur de prédiction de quelques dizaines de secondes à quelques
  secondes ;
* **détecter** ce que la donnée ouverte ne publie pas, en signalant les
  passages observés qui ne correspondent à aucune prédiction.
"""

from .base import Detection, PassageDetector, to_db

__all__ = ["Detection", "PassageDetector", "to_db"]
