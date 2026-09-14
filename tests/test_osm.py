import pytest

from nexttraintosee.geo import from_local_xy, haversine_m, polyline_length_m
from nexttraintosee.osm import (
    MAX_ANCHOR_OFFSET_M,
    RailStop,
    RailWay,
    build_corridors,
    build_query,
    nearest_stops,
    parse_overpass,
    speed_profile,
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
    assert corridors[0].segment_count == 2
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

    assert sum(c.segment_count for c in build_corridors(ways, POINT)) == 1
    assert sum(c.segment_count for c in build_corridors(ways, POINT, include_service=True)) == 2


def test_non_rail_ways_are_excluded():
    ways = [_way(1, _line(60, 0)), _way(2, _line(70, 0), railway="abandoned")]
    corridors = build_corridors(ways, POINT)
    assert sum(c.segment_count for c in corridors) == 1


def test_corridor_label_prefers_the_line_name():
    ways = [_way(1, _line(60, 0)), _way(2, _line(68, 0), name="Ligne de Bordeaux à Sète")]
    assert build_corridors(ways, POINT)[0].label == "Ligne de Bordeaux à Sète"


def test_corridor_label_names_every_line_of_a_shared_trunk():
    # Dans un tronc commun, un faisceau porte des voies de plusieurs lignes :
    # n'en nommer qu'une donnerait au corridor une identité trompeuse.
    ways = [
        _way(1, _line(60, 0), name="Ligne de Bordeaux à Sète"),
        _way(2, _line(64, 0), name="Ligne de Bordeaux à Sète"),
        _way(3, _line(68, 0), name="Ligne de Toulouse à Bayonne"),
    ]
    label = build_corridors(ways, POINT)[0].label
    assert "Sète" in label and "Bayonne" in label
    # La ligne la plus représentée vient en premier.
    assert label.index("Sète") < label.index("Bayonne")


def test_corridor_reports_the_highest_track_speed():
    ways = [_way(1, _line(60, 0), maxspeed="90"), _way(2, _line(68, 0), maxspeed="110")]
    assert build_corridors(ways, POINT)[0].maxspeed_kmh == 110.0


def test_along_distance_follows_the_track_not_the_crow():
    # Voie nord-sud ; une gare 1 500 m au nord et 300 m à l'est du point.
    # À vol d'oiseau elle est plus loin que le long de la voie.
    ways = [_way(1, _line(0, 0, "ns", length_m=6000))]
    corridor = build_corridors(ways, POINT)[0]
    station = from_local_xy(POINT, (300.0, 1500.0))

    link = corridor.link_to(station)
    assert link.along_distance_m == pytest.approx(1500.0, abs=5.0)
    assert link.along_distance_m < haversine_m(POINT, station)
    assert link.is_plausible


def test_a_station_set_back_from_the_track_stays_plausible():
    # La gare est à 300 m de la voie : le décalage est déduit avant contrôle.
    ways = [_way(1, _line(0, 0, "ns", length_m=6000))]
    link = build_corridors(ways, POINT)[0].link_to(from_local_xy(POINT, (300.0, 1500.0)))
    assert link.anchor_offset_m == pytest.approx(300.0, abs=5.0)
    assert link.is_plausible


def test_a_truncated_centerline_is_flagged_instead_of_lying():
    # Régression : quand la géométrie récupérée n'atteint pas la gare, la
    # projection est bornée à l'extrémité de la polyligne et la distance
    # mesurée devient absurde (plus courte qu'à vol d'oiseau). Il faut le dire,
    # pas le publier.
    ways = [_way(1, _line(0, 0, "ns", length_m=600))]
    corridor = build_corridors(ways, POINT)[0]
    station = from_local_xy(POINT, (0.0, 1500.0))

    link = corridor.link_to(station)
    assert link.along_distance_m < link.straight_distance_m
    assert link.anchor_offset_m > MAX_ANCHOR_OFFSET_M
    assert not link.is_plausible


def test_outbound_bearing_points_away_from_the_station():
    # Gare au nord, voie nord-sud : passé le point, le corridor part au sud.
    ways = [_way(1, _line(0, 0, "ns", length_m=12000))]
    corridor = build_corridors(ways, POINT)[0]
    link = corridor.link_to(from_local_xy(POINT, (0.0, 1500.0)), lookahead_m=4000.0)

    assert link.outbound_bearing_deg == pytest.approx(180.0, abs=2.0)


def test_outbound_bearing_is_absent_when_the_geometry_is_too_short():
    ways = [_way(1, _line(0, 0, "ns", length_m=300))]
    corridor = build_corridors(ways, POINT)[0]
    assert corridor.link_to(from_local_xy(POINT, (0.0, 200.0))).outbound_bearing_deg is None


def test_lateral_spread_measures_the_width_of_the_fan():
    ways = [_way(1, _line(60, 0)), _way(2, _line(68, 0)), _way(3, _line(100, 0))]
    corridor = build_corridors(ways, POINT, corridor_width_m=80.0)[0]
    assert corridor.segment_count == 3
    assert corridor.lateral_spread_m == pytest.approx(40.0, abs=2.0)


def test_centerlines_are_stitched_across_the_whole_fetch():
    # Deux tronçons bout à bout : seul le premier est dans le rayon de
    # recherche, mais la polyligne doit courir sur les deux.
    near = _line(0, 0, "ns", length_m=1000)
    far = (near[1], from_local_xy(POINT, (0.0, 3000.0)))
    corridor = build_corridors([_way(1, near), _way(2, far)], POINT, max_distance_m=100)[0]

    assert corridor.segment_count == 1  # un seul tronçon est proche du point
    assert polyline_length_m(list(corridor.centerline)) == pytest.approx(3500.0, abs=50.0)


def test_nearest_stops_are_sorted_by_distance():
    near = RailStop(1, from_local_xy(POINT, (0.0, 500.0)), {"railway": "halt"})
    far = RailStop(2, from_local_xy(POINT, (0.0, 5000.0)), {"railway": "station"})
    ranked = nearest_stops([far, near], POINT, limit=2)
    assert [s.osm_id for s, _ in ranked] == [1, 2]
    assert ranked[0][1] == pytest.approx(500.0, abs=5.0)


def test_stitching_follows_the_straightest_branch_at_a_junction():
    # Une ligne droite nord-sud coupée en deux, plus une branche divergente
    # partant du même nœud : la chaîne doit rester sur la ligne droite.
    node = from_local_xy(POINT, (0.0, 0.0))
    upstream = (from_local_xy(POINT, (0.0, -1000.0)), node)
    straight = (node, from_local_xy(POINT, (0.0, 1000.0)))
    diverging = (node, from_local_xy(POINT, (700.0, 700.0)))

    chains = stitch_ways([_way(1, upstream), _way(2, straight), _way(3, diverging)])

    longest = chains[0]
    assert longest[0] == upstream[0]
    assert longest[-1] == straight[-1]
    assert len(chains) == 2  # la branche divergente forme sa propre chaîne


def test_build_query_embeds_the_point_and_radius():
    query = build_query(POINT, 400.0)
    assert "around:400,43.597833,1.458194" in query
    assert "railway" in query and "out tags geom" in query


def test_build_query_can_exclude_service_tracks():
    assert '["service"!~"."]' in build_query(POINT, 400.0, include_service=False)


def test_build_query_sweeps_the_corridor_up_to_the_anchor():
    # Sans ce balayage, les polylignes s'arrêtent au bord du rayon de recherche
    # et la distance jusqu'à la gare est fausse.
    query = build_query(POINT, 400.0, anchor=(43.6112, 1.4535))
    assert "43.597833,1.458194,43.611200,1.453500" in query
    # Le rayon de recherche des gares doit lui aussi couvrir la gare d'appui.
    assert "around:2534" in query


def test_build_query_excludes_metro_stations():
    # Sans ce filtre, les stations de métro toulousaines masquent Matabiau.
    assert '["station"!~"^(subway|light_rail|monorail)$"]' in build_query(POINT, 400.0)


# -- relevé des vitesses le long du parcours ---------------------------------


def test_speed_profile_locates_a_tunnel_restriction():
    # Voie nord-sud continue, coupée en trois tronçons OSM : le tronçon central
    # est un tunnel limité à 60, comme le laisse supposer un panneau vu au sol.
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    a = (anchor, from_local_xy(POINT, (0.0, 1200.0)))
    b = (a[1], from_local_xy(POINT, (0.0, 800.0)))
    c = (b[1], from_local_xy(POINT, (0.0, -500.0)))
    ways = [
        _way(1, a, maxspeed="113"),
        _way(2, b, maxspeed="60", tunnel="yes"),
        _way(3, c, maxspeed="113"),
    ]
    corridor = build_corridors(ways, POINT)[0]

    segments = speed_profile(corridor, ways, anchor)

    assert [round(s.length_m) for s in segments] == [364, 400, 800]
    tunnel = segments[1]
    assert tunnel.tunnel is True
    assert tunnel.maxspeed_kmh == 60.0
    # La restriction commence à ~364 m de la gare, pas au point d'observation.
    assert tunnel.start_m == pytest.approx(364.0, abs=5.0)


def test_speed_profile_stops_at_the_observation_point():
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    ways = [_way(1, (anchor, from_local_xy(POINT, (0.0, -3000.0))), maxspeed="113")]
    corridor = build_corridors(ways, POINT)[0]

    segments = speed_profile(corridor, ways, anchor)

    assert len(segments) == 1
    assert segments[0].start_m == pytest.approx(0.0, abs=1.0)
    # Le parcours s'arrête au point, pas à la fin de la voie.
    assert segments[0].end_m == pytest.approx(1564.0, abs=5.0)


def test_speed_profile_ignores_parallel_tracks():
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    main = (anchor, from_local_xy(POINT, (0.0, -500.0)))
    parallel = (from_local_xy(POINT, (300.0, 1564.0)), from_local_xy(POINT, (300.0, -500.0)))
    ways = [_way(1, main, maxspeed="113"), _way(2, parallel, maxspeed="40")]
    corridor = build_corridors(ways, POINT, max_distance_m=400.0, corridor_width_m=500.0)[0]

    segments = speed_profile(corridor, ways, anchor)

    assert [s.maxspeed_kmh for s in segments] == [113.0]


def test_speed_profile_reports_an_unmapped_speed_rather_than_guessing():
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    ways = [_way(1, (anchor, from_local_xy(POINT, (0.0, -500.0))))]
    corridor = build_corridors(ways, POINT)[0]

    assert speed_profile(corridor, ways, anchor)[0].maxspeed_kmh is None
