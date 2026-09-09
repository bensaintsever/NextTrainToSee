import pytest

from nexttraintosee.geo import from_local_xy, haversine_m
from nexttraintosee.osm import (
    RailStop,
    RailWay,
    build_corridors,
    build_query,
    nearest_stops,
    parse_overpass,
    stitch_ways,
)

POINT = (43.597833, 1.458194)


def _line(offset_east_m: float, offset_north_m: float, bearing: str = "ns", length_m: float = 2000.0):
    """Construit une voie droite décalée du point d'observation."""
    half = length_m / 2
    if bearing == "ns":
        ends = [(offset_east_m, offset_north_m - half), (offset_east_m, offset_north_m + half)]
    else:
        ends = [(offset_east_m - half, offset_north_m), (offset_east_m + half, offset_north_m)]
    return tuple(from_local_xy(POINT, xy) for xy in ends)


def _way(osm_id: int, geometry, **tags):
    tags.setdefault("railway", "rail")
    return RailWay(osm_id=osm_id, geometry=tuple(geometry), tags=tags)


def test_parse_overpass_splits_ways_and_nodes():
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "tags": {"railway": "rail", "maxspeed": "90"},
                "geometry": [{"lat": 43.5, "lon": 1.4}, {"lat": 43.6, "lon": 1.4}],
            },
            {"type": "node", "id": 2, "lat": 43.61, "lon": 1.45, "tags": {"railway": "station"}},
        ]
    }
    ways, stops = parse_overpass(payload)
    assert [w.osm_id for w in ways] == [1]
    assert ways[0].maxspeed_kmh == 90.0
    assert [s.osm_id for s in stops] == [2]
    assert stops[0].kind == "station"


def test_parse_overpass_drops_ways_without_usable_geometry():
    payload = {
        "elements": [
            {"type": "way", "id": 1, "geometry": [{"lat": 43.5, "lon": 1.4}]},
            {"type": "way", "id": 2, "tags": {"railway": "rail"}},
        ]
    }
    ways, stops = parse_overpass(payload)
    assert ways == [] and stops == []


def test_maxspeed_ignores_unparsable_values():
    assert _way(1, _line(0, 0), maxspeed="RS100").maxspeed_kmh is None
    assert _way(1, _line(0, 0), maxspeed="120 km/h").maxspeed_kmh == 120.0


def test_stitch_joins_segments_regardless_of_order_and_direction():
    a = ((43.50, 1.45), (43.51, 1.45))
    b = ((43.51, 1.45), (43.52, 1.45))
    c = ((43.53, 1.45), (43.52, 1.45))  # tracé en sens inverse
    chains = stitch_ways([_way(2, b), _way(3, c), _way(1, a)])

    assert len(chains) == 1
    assert chains[0][0] == (43.50, 1.45)
    assert chains[0][-1] == (43.53, 1.45)
    assert len(chains[0]) == 4


def test_stitch_keeps_disconnected_groups_apart():
    a = ((43.50, 1.45), (43.51, 1.45))
    far = ((43.80, 1.90), (43.81, 1.90))
    chains = stitch_ways([_way(1, a), _way(2, far)])
    assert len(chains) == 2


def test_parallel_tracks_form_a_single_corridor():
    # Deux voies nord-sud espacées de 8 m, à ~60 m du point : une ligne à double voie.
    ways = [_way(1, _line(60, 0)), _way(2, _line(68, 0))]
    corridors = build_corridors(ways, POINT, max_distance_m=400)

    assert len(corridors) == 1
    assert corridors[0].track_count == 2
    assert corridors[0].distance_m == pytest.approx(60.0, abs=2.0)
    assert corridors[0].axis_deg == pytest.approx(0.0, abs=1.0)


def test_crossing_lines_form_distinct_corridors():
    ways = [_way(1, _line(60, 0, "ns")), _way(2, _line(0, 40, "ew"))]
    corridors = build_corridors(ways, POINT, max_distance_m=400)

    assert len(corridors) == 2
    # Le plus proche est trié en premier.
    assert corridors[0].distance_m < corridors[1].distance_m
    assert corridors[0].axis_deg == pytest.approx(90.0, abs=1.0)


def test_distant_tracks_are_ignored():
    ways = [_way(1, _line(60, 0)), _way(2, _line(900, 0))]
    corridors = build_corridors(ways, POINT, max_distance_m=400)
    assert len(corridors) == 1


def test_service_tracks_are_excluded_unless_requested():
    ways = [_way(1, _line(60, 0)), _way(2, _line(120, 0), service="yard")]

    assert sum(c.track_count for c in build_corridors(ways, POINT)) == 1
    assert sum(c.track_count for c in build_corridors(ways, POINT, include_service=True)) == 2


def test_non_rail_ways_are_excluded():
    ways = [_way(1, _line(60, 0)), _way(2, _line(70, 0), railway="abandoned")]
    corridors = build_corridors(ways, POINT)
    assert sum(c.track_count for c in corridors) == 1


def test_corridor_label_prefers_the_line_name():
    ways = [_way(1, _line(60, 0)), _way(2, _line(68, 0), name="Ligne de Bordeaux à Sète")]
    assert build_corridors(ways, POINT)[0].label == "Ligne de Bordeaux à Sète"


def test_corridor_reports_the_highest_track_speed():
    ways = [_way(1, _line(60, 0), maxspeed="90"), _way(2, _line(68, 0), maxspeed="110")]
    assert build_corridors(ways, POINT)[0].maxspeed_kmh == 110.0


def test_along_distance_follows_the_track_not_the_crow():
    # Voie nord-sud ; une gare 1 500 m au nord et 300 m à l'est du point.
    # À vol d'oiseau elle est plus loin que le long de la voie.
    ways = [_way(1, _line(0, 0, "ns", length_m=6000))]
    corridor = build_corridors(ways, POINT)[0]
    station = from_local_xy(POINT, (300.0, 1500.0))

    along = corridor.along_distance_to_m(station)
    assert along == pytest.approx(1500.0, abs=5.0)
    assert along < haversine_m(POINT, station)


def test_nearest_stops_are_sorted_by_distance():
    near = RailStop(1, from_local_xy(POINT, (0.0, 500.0)), {"railway": "halt"})
    far = RailStop(2, from_local_xy(POINT, (0.0, 5000.0)), {"railway": "station"})
    ranked = nearest_stops([far, near], POINT, limit=2)
    assert [s.osm_id for s, _ in ranked] == [1, 2]
    assert ranked[0][1] == pytest.approx(500.0, abs=5.0)


def test_build_query_embeds_the_point_and_radius():
    query = build_query(POINT, 400.0)
    assert "around:400,43.597833,1.458194" in query
    assert "railway" in query and "out tags geom" in query


def test_build_query_can_exclude_service_tracks():
    assert '["service"!~"."]' in build_query(POINT, 400.0, include_service=False)
