"""NextTrainToSee — prédire le passage des trains devant un point d'observation.

Le point d'entrée usuel est la ligne de commande (`nexttraintosee --help`) ;
les briques sont aussi utilisables directement :

    from nexttraintosee.config import load_config
    from nexttraintosee.gtfs import GtfsFeed
    from nexttraintosee.predict import predict_passages
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
