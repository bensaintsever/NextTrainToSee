"""Capteur audio : niveau sonore capté par un micro.

Adaptateur mince au-dessus de `sounddevice`. Toute l'intelligence est dans
`PassageDetector` : ici on se contente de produire un niveau (RMS) par bloc.

Un micro posé à une fenêtre suffit à distinguer un train du bruit urbain : le
passage produit un souffle large bande de plusieurs secondes, bien au-dessus du
fond, avec une montée et une descente nettes. Un micro de contact (piézo) collé
à une vitre est encore plus sélectif si le bruit routier gêne.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Iterator

log = logging.getLogger(__name__)


class AudioUnavailable(RuntimeError):
    """`sounddevice` absent, ou aucune entrée audio utilisable."""


def rms(block) -> float:
    """Valeur efficace d'un bloc d'échantillons.

    Accepte un tableau numpy comme une simple séquence de flottants, ce qui
    permet de tester sans matériel ni dépendance.
    """
    values = [float(v) for v in (block.flatten() if hasattr(block, "flatten") else block)]
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


def listen(
    sample_rate_hz: int = 16_000,
    block_seconds: float = 0.5,
    device: str | int | None = None,
) -> Iterator[tuple[datetime, float]]:
    """Produit un couple (instant, niveau RMS) par bloc audio.

    Raises:
        AudioUnavailable: si `sounddevice` n'est pas installé ou si l'entrée
            audio ne peut pas être ouverte.
    """
    try:
        import sounddevice
    except ImportError as exc:  # pragma: no cover - dépend de l'environnement
        raise AudioUnavailable(
            "capture audio indisponible : installez les extras capteur "
            '(pip install "nexttraintosee[sensor]")'
        ) from exc

    block_size = max(1, int(sample_rate_hz * block_seconds))
    try:
        stream = sounddevice.InputStream(
            samplerate=sample_rate_hz, blocksize=block_size, channels=1, device=device
        )
    except Exception as exc:  # pragma: no cover - dépend du matériel
        raise AudioUnavailable(f"impossible d'ouvrir l'entrée audio : {exc}") from exc

    with stream:
        log.info(
            "capture audio démarrée : %d Hz, blocs de %.2f s", sample_rate_hz, block_seconds
        )
        while True:
            block, overflowed = stream.read(block_size)
            if overflowed:
                log.warning("dépassement de tampon audio : bloc probablement tronqué")
            yield datetime.now(timezone.utc).astimezone(), rms(block)
