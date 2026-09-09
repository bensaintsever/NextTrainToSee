"""Confrontation prédictions / observations : recalage et détection d'inconnus.

Deux usages, tous deux essentiels :

* **recaler le modèle** — les résidus (observé moins prédit) donnent le biais
  systématique du modèle de marche, qu'on peut ensuite retrancher ;
* **repérer ce qui n'est pas au GTFS** — une détection qui ne correspond à
  aucune prédiction est, sur une ligne classique, très probablement un train de
  fret, un haut-le-pied ou un engin de travaux.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Callable, Iterable, Sequence

from .motion import Regime, TractionProfile, segment_time_s, travel_time_s
from .predict import Passage
from .sensor.base import Detection


@dataclass(frozen=True)
class Match:
    """Une détection appariée à un passage prédit."""

    detection: Detection
    passage: Passage

    @property
    def residual_s(self) -> float:
        """Observé moins prédit, en secondes.

        Positif : le train est passé plus tard que prévu.
        """
        return (self.detection.midpoint - self.passage.when).total_seconds()


@dataclass
class MatchResult:
    """Résultat d'un appariement sur une période."""

    matches: list[Match]
    unmatched_detections: list[Detection]
    """Passages observés sans prédiction correspondante : candidats fret."""
    unmatched_passages: list[Passage]
    """Passages prédits que le capteur n'a pas vus : suppressions, ou capteur muet."""

    @property
    def residuals_s(self) -> list[float]:
        return [m.residual_s for m in self.matches]

    def summary(self) -> str:
        total = len(self.matches) + len(self.unmatched_passages)
        rate = 100 * len(self.matches) / total if total else 0.0
        return (
            f"{len(self.matches)} appariements sur {total} passages prédits ({rate:.0f} %), "
            f"{len(self.unmatched_detections)} détections inexpliquées"
        )


def match_detections(
    detections: Sequence[Detection],
    passages: Sequence[Passage],
    tolerance_s: float = 180.0,
) -> MatchResult:
    """Apparie détections et prédictions, au plus une fois chacune.

    On construit tous les couples compatibles puis on les attribue par écart
    croissant : un appariement glouton sur l'écart absolu, qui évite qu'une
    détection précoce ne monopolise le mauvais train.

    Args:
        detections: passages observés par le capteur.
        passages: passages prédits sur la même période.
        tolerance_s: écart maximal toléré entre observé et prédit.
    """
    candidates = []
    for detection_index, detection in enumerate(detections):
        for passage_index, passage in enumerate(passages):
            gap = abs((detection.midpoint - passage.when).total_seconds())
            if gap <= tolerance_s:
                candidates.append((gap, detection_index, passage_index))
    candidates.sort()

    used_detections: set[int] = set()
    used_passages: set[int] = set()
    matches: list[Match] = []
    for _, detection_index, passage_index in candidates:
        if detection_index in used_detections or passage_index in used_passages:
            continue
        used_detections.add(detection_index)
        used_passages.add(passage_index)
        matches.append(Match(detections[detection_index], passages[passage_index]))

    matches.sort(key=lambda m: m.detection.midpoint)
    return MatchResult(
        matches=matches,
        unmatched_detections=[d for i, d in enumerate(detections) if i not in used_detections],
        unmatched_passages=[p for i, p in enumerate(passages) if i not in used_passages],
    )


@dataclass(frozen=True)
class Calibration:
    """Correction apprise à partir des passages réellement observés."""

    bias_s: float
    """Biais médian global, à retrancher des prédictions."""
    spread_s: float
    """Dispersion des résidus (écart absolu médian) : l'incertitude réelle."""
    sample_size: int
    per_regime_bias_s: dict[Regime, float]
    """Biais par régime de marche : départ, arrivée et passage se recalent
    rarement de la même façon."""

    def adjust(self, passage: Passage) -> Passage:
        """Applique la correction à un passage prédit."""
        bias = self.per_regime_bias_s.get(passage.regime, self.bias_s)
        return replace(
            passage,
            when=passage.when + timedelta(seconds=bias),
            uncertainty_s=self.spread_s or passage.uncertainty_s,
        )

    def describe(self) -> str:
        details = ", ".join(
            f"{regime.value} {bias:+.0f}s" for regime, bias in sorted(self.per_regime_bias_s.items())
        )
        return (
            f"biais {self.bias_s:+.0f}s, dispersion ±{self.spread_s:.0f}s "
            f"sur {self.sample_size} passages ({details})"
        )


def calibrate(matches: Sequence[Match]) -> Calibration:
    """Estime le biais du modèle à partir d'appariements.

    On utilise médiane et écart absolu médian plutôt que moyenne et écart-type :
    un appariement erroné (deux trains rapprochés) déplacerait fortement une
    moyenne, beaucoup moins une médiane.

    Raises:
        ValueError: si aucun appariement n'est fourni.
    """
    if not matches:
        raise ValueError("calibration impossible : aucun passage apparié")

    residuals = [m.residual_s for m in matches]
    bias = statistics.median(residuals)
    spread = statistics.median(abs(r - bias) for r in residuals)

    per_regime: dict[Regime, float] = {}
    for regime in Regime:
        subset = [m.residual_s for m in matches if m.passage.regime is regime]
        if subset:
            per_regime[regime] = statistics.median(subset)

    return Calibration(
        bias_s=bias, spread_s=spread, sample_size=len(matches), per_regime_bias_s=per_regime
    )


@dataclass(frozen=True)
class ObservedRun:
    """Un temps de parcours réellement mesuré entre la gare d'appui et le point."""

    distance_m: float
    regime: Regime
    observed_travel_s: float


def runs_from_matches(matches: Iterable[Match], distance_for: dict[str, float]) -> list[ObservedRun]:
    """Convertit des appariements en temps de parcours mesurés.

    Args:
        matches: appariements détection / prédiction.
        distance_for: distance gare d'appui -> point, par identifiant de branche.
    """
    runs: list[ObservedRun] = []
    for match in matches:
        distance = distance_for.get(match.passage.branch.branch_id)
        if distance is None:
            continue
        observed = abs((match.detection.midpoint - match.passage.anchor_time).total_seconds())
        runs.append(ObservedRun(distance, match.passage.regime, observed))
    return runs


def _rms_error(runs: Sequence[ObservedRun], profile: TractionProfile) -> float:
    total = 0.0
    for run in runs:
        error = travel_time_s(run.distance_m, run.regime, profile) - run.observed_travel_s
        total += error * error
    return (total / len(runs)) ** 0.5


def _grid_search(
    cost: Callable[[TractionProfile], float],
    start: TractionProfile,
    rounds: int = 3,
    steps: int = 9,
) -> TractionProfile:
    """Minimise `cost` sur les trois paramètres de marche, par grilles resserrées.

    Le problème a trois paramètres bornés et une poignée d'observations : une
    grille que l'on resserre autour du meilleur point converge en quelques
    passes, là où une descente de gradient serait démesurée.
    """
    bounds = {"accel_ms2": (0.15, 1.30), "decel_ms2": (0.15, 1.30), "line_speed_kmh": (30.0, 200.0)}
    best = start
    best_cost = cost(best)

    for round_index in range(rounds):
        shrink = 0.5**round_index
        grids = {}
        for name, (low, high) in bounds.items():
            centre = getattr(best, name)
            span = (high - low) * shrink / 2
            lo, hi = max(low, centre - span), min(high, centre + span)
            grids[name] = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]

        for accel in grids["accel_ms2"]:
            for decel in grids["decel_ms2"]:
                for speed in grids["line_speed_kmh"]:
                    candidate = replace(
                        best, accel_ms2=accel, decel_ms2=decel, line_speed_kmh=speed
                    )
                    candidate_cost = cost(candidate)
                    if candidate_cost < best_cost:
                        best, best_cost = candidate, candidate_cost

    return best


def fit_profile(
    runs: Sequence[ObservedRun],
    start: TractionProfile | None = None,
    rounds: int = 3,
) -> tuple[TractionProfile, float]:
    """Ajuste accélération, freinage et vitesse de ligne sur des temps mesurés.

    Returns:
        Le profil ajusté et l'erreur quadratique moyenne résiduelle, en secondes.

    Raises:
        ValueError: si aucun temps de parcours n'est fourni.
    """
    if not runs:
        raise ValueError("ajustement impossible : aucun temps de parcours observé")

    def cost(profile: TractionProfile) -> float:
        return _rms_error(runs, profile)

    best = _grid_search(cost, start or TractionProfile(), rounds=rounds)
    return best, cost(best)


def fit_segment_profile(
    segments: Sequence[tuple[float, float]],
    start: TractionProfile | None = None,
    rounds: int = 3,
    overshoot_penalty: float = 3.0,
) -> tuple[TractionProfile, float]:
    """Ajuste le profil sur des temps de parcours d'arrêt à arrêt.

    Args:
        segments: couples (longueur en mètres, durée de référence en secondes).
            La durée attendue est **la plus rapide observée** sur le segment,
            celle qui approche la limite physique — pas une médiane, qui porte
            la marge de régularité.
        start: profil de départ.
        rounds: nombre de resserrements de grille.
        overshoot_penalty: poids appliqué aux écarts où le modèle est *plus
            rapide* que l'horaire le plus rapide. Un tel écart est physiquement
            impossible, alors qu'être un peu plus lent est seulement prudent :
            on pénalise donc les deux différemment.

    Returns:
        Le profil ajusté et l'erreur quadratique moyenne résiduelle, en secondes.

    Raises:
        ValueError: si aucun segment n'est fourni.
    """
    if not segments:
        raise ValueError("ajustement impossible : aucun segment fourni")

    def cost(profile: TractionProfile) -> float:
        total = 0.0
        for length_m, reference_s in segments:
            error = segment_time_s(length_m, profile) - reference_s
            weight = overshoot_penalty if error < 0 else 1.0
            total += weight * error * error
        return (total / len(segments)) ** 0.5

    best = _grid_search(cost, start or TractionProfile(), rounds=rounds)
    residual = (
        sum((segment_time_s(l, best) - t) ** 2 for l, t in segments) / len(segments)
    ) ** 0.5
    return best, residual


def fit_line_speed_kmh(
    length_m: float,
    target_s: float,
    profile: TractionProfile,
    low_kmh: float = 20.0,
    high_kmh: float = 200.0,
    tolerance_s: float = 0.5,
) -> float:
    """Vitesse de ligne reproduisant une durée de parcours donnée.

    Accélération et freinage sont conservés : sur un unique segment, ajuster
    trois paramètres à la fois n'a aucun sens, alors que la vitesse de ligne est
    à la fois la plus incertaine et la plus déterminante. La durée décroît
    strictement avec la vitesse, ce qui autorise une simple dichotomie.

    Returns:
        La vitesse ajustée, bornée à l'intervalle de recherche quand la durée
        visée est hors d'atteinte.

    Raises:
        ValueError: si la longueur ou la durée visée n'est pas positive.
    """
    if length_m <= 0 or target_s <= 0:
        raise ValueError("longueur et durée visée doivent être strictement positives")

    def duration(speed_kmh: float) -> float:
        return segment_time_s(length_m, replace(profile, line_speed_kmh=speed_kmh))

    if duration(high_kmh) > target_s:
        return high_kmh
    if duration(low_kmh) < target_s:
        return low_kmh

    for _ in range(60):
        middle = (low_kmh + high_kmh) / 2
        elapsed = duration(middle)
        if abs(elapsed - target_s) <= tolerance_s:
            return middle
        if elapsed > target_s:
            low_kmh = middle
        else:
            high_kmh = middle
    return (low_kmh + high_kmh) / 2
