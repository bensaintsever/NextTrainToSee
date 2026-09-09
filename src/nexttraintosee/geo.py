"""Géométrie sphérique locale.

Toutes les fonctions travaillent en WGS84 (lat, lon en degrés décimaux) et
renvoient des distances en mètres. Aux échelles qui nous intéressent (quelques
kilomètres autour d'un point d'observation), la projection ENU tangente locale
introduit une erreur très inférieure au mètre : c'est elle qu'on utilise pour
tout ce qui est projection / distance point-segment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

# Un point géographique : (latitude, longitude) en degrés.
LatLon = tuple[float, float]


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Distance orthodromique entre deux points, en mètres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def initial_bearing_deg(a: LatLon, b: LatLon) -> float:
    """Cap initial de a vers b, en degrés dans [0, 360[ (0 = nord, 90 = est)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def bearing_delta_deg(a_deg: float, b_deg: float) -> float:
    """Écart signé minimal entre deux caps, dans ]-180, 180]."""
    return (b_deg - a_deg + 180.0) % 360.0 - 180.0


def bearing_distance_deg(a_deg: float, b_deg: float) -> float:
    """Écart absolu minimal entre deux caps, dans [0, 180]."""
    return abs(bearing_delta_deg(a_deg, b_deg))


def axis_distance_deg(a_deg: float, b_deg: float) -> float:
    """Écart entre deux *axes* non orientés, dans [0, 90].

    Deux voies parcourues en sens inverse ont des caps opposés mais le même axe ;
    c'est cette mesure qu'il faut utiliser pour dire « ces rails sont parallèles ».
    """
    d = bearing_distance_deg(a_deg, b_deg)
    return min(d, 180.0 - d)


def to_local_xy(origin: LatLon, point: LatLon) -> tuple[float, float]:
    """Projette `point` dans un repère métrique plan centré sur `origin`.

    Renvoie (est, nord) en mètres.
    """
    lat0 = math.radians(origin[0])
    dlat = math.radians(point[0] - origin[0])
    dlon = math.radians(point[1] - origin[1])
    return (EARTH_RADIUS_M * dlon * math.cos(lat0), EARTH_RADIUS_M * dlat)


def from_local_xy(origin: LatLon, xy: tuple[float, float]) -> LatLon:
    """Opération inverse de `to_local_xy`."""
    lat0 = math.radians(origin[0])
    east, north = xy
    lat = origin[0] + math.degrees(north / EARTH_RADIUS_M)
    lon = origin[1] + math.degrees(east / (EARTH_RADIUS_M * math.cos(lat0)))
    return (lat, lon)


def polyline_length_m(points: Sequence[LatLon]) -> float:
    """Longueur cumulée d'une polyligne."""
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def cumulative_lengths_m(points: Sequence[LatLon]) -> list[float]:
    """Abscisses curvilignes des sommets, en partant de 0."""
    out = [0.0]
    for i in range(len(points) - 1):
        out.append(out[-1] + haversine_m(points[i], points[i + 1]))
    return out


@dataclass(frozen=True)
class Projection:
    """Résultat de la projection d'un point sur une polyligne."""

    point: LatLon
    """Point de la polyligne le plus proche."""
    distance_m: float
    """Distance latérale entre le point requêté et la polyligne."""
    along_m: float
    """Abscisse curviligne du point projeté, mesurée depuis le début de la polyligne."""
    segment_index: int
    """Index du segment portant la projection."""
    bearing_deg: float
    """Cap du segment portant la projection, dans le sens de parcours de la polyligne."""


def project_on_polyline(point: LatLon, polyline: Sequence[LatLon]) -> Projection:
    """Projette `point` sur la polyligne et renvoie le pied de projection.

    Lève `ValueError` si la polyligne compte moins de deux sommets.
    """
    if len(polyline) < 2:
        raise ValueError("une polyligne doit avoir au moins deux sommets")

    cumulative = cumulative_lengths_m(polyline)
    best: Projection | None = None

    for i in range(len(polyline) - 1):
        a, b = polyline[i], polyline[i + 1]
        # Repère local centré sur le point requêté : l'erreur de projection est
        # alors nulle au point d'intérêt et négligeable sur un segment court.
        ax, ay = to_local_xy(point, a)
        bx, by = to_local_xy(point, b)
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0.0:
            t = 0.0
        else:
            # Le point requêté est l'origine du repère, d'où le produit scalaire -a.d
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))
        px, py = ax + t * dx, ay + t * dy
        distance = math.hypot(px, py)
        if best is None or distance < best.distance_m:
            seg_len = math.sqrt(seg_len_sq)
            best = Projection(
                point=from_local_xy(point, (px, py)),
                distance_m=distance,
                along_m=cumulative[i] + t * seg_len,
                segment_index=i,
                bearing_deg=initial_bearing_deg(a, b),
            )

    assert best is not None
    return best


def interpolate_along(polyline: Sequence[LatLon], along_m: float) -> LatLon:
    """Point situé à l'abscisse curviligne `along_m` sur la polyligne.

    Borné aux extrémités : une abscisse négative renvoie le premier sommet, une
    abscisse au-delà de la longueur totale renvoie le dernier.
    """
    if len(polyline) < 2:
        raise ValueError("une polyligne doit avoir au moins deux sommets")
    cumulative = cumulative_lengths_m(polyline)
    if along_m <= 0:
        return polyline[0]
    if along_m >= cumulative[-1]:
        return polyline[-1]
    for i in range(len(polyline) - 1):
        if cumulative[i] <= along_m <= cumulative[i + 1]:
            span = cumulative[i + 1] - cumulative[i]
            fraction = 0.0 if span == 0 else (along_m - cumulative[i]) / span
            ax, ay = to_local_xy(polyline[i], polyline[i])
            bx, by = to_local_xy(polyline[i], polyline[i + 1])
            return from_local_xy(
                polyline[i], (ax + fraction * (bx - ax), ay + fraction * (by - ay))
            )
    return polyline[-1]


def densify(polyline: Sequence[LatLon], max_step_m: float) -> list[LatLon]:
    """Insère des sommets pour qu'aucun segment ne dépasse `max_step_m`.

    Utile avant un échantillonnage régulier ; sans effet si la polyligne est déjà
    assez dense.
    """
    if max_step_m <= 0:
        raise ValueError("max_step_m doit être strictement positif")
    out: list[LatLon] = []
    for i in range(len(polyline) - 1):
        a, b = polyline[i], polyline[i + 1]
        out.append(a)
        seg = haversine_m(a, b)
        steps = int(seg // max_step_m)
        if seg > max_step_m:
            ax, ay = to_local_xy(a, a)
            bx, by = to_local_xy(a, b)
            for k in range(1, steps + 1):
                f = k / (steps + 1)
                out.append(from_local_xy(a, (ax + f * (bx - ax), ay + f * (by - ay))))
    if polyline:
        out.append(polyline[-1])
    return out


def bounding_box(points: Iterable[LatLon], margin_m: float = 0.0) -> tuple[float, float, float, float]:
    """Boîte englobante (sud, ouest, nord, est) éventuellement dilatée."""
    pts = list(points)
    if not pts:
        raise ValueError("bounding_box requiert au moins un point")
    south = min(p[0] for p in pts)
    north = max(p[0] for p in pts)
    west = min(p[1] for p in pts)
    east = max(p[1] for p in pts)
    if margin_m:
        dlat = math.degrees(margin_m / EARTH_RADIUS_M)
        mid_lat = math.radians((south + north) / 2)
        dlon = math.degrees(margin_m / (EARTH_RADIUS_M * math.cos(mid_lat)))
        south, north, west, east = south - dlat, north + dlat, west - dlon, east + dlon
    return (south, west, north, east)
