import pytest

from nexttraintosee.geo import from_local_xy, haversine_m, polyline_length_m
from nexttraintosee.osm import (
    MAX_ANCHOR_OFFSET_M,
    RailStop,
    RailWay,
    SpeedSegment,
    build_corridors,
    build_query,
    merged_length_m,
    nearest_stops,
    parse_overpass,
    slice_polyline,
    speed_profile,
    stitch_ways,
    to_geojson,
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


def test_speed_profile_counts_from_the_station_whichever_way_the_line_was_stitched():
    # Régression : la polyligne peut être reconstituée du point vers la gare.
    # Les abscisses doivent rester comptées depuis la gare, sinon le profil se
    # lit à l'envers — l'avant-gare apparaît près du point d'observation.
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    south = from_local_xy(POINT, (0.0, -600.0))
    # Géométrie tracée du SUD vers le NORD : la gare est à l'abscisse haute.
    near_point = _way(1, (south, from_local_xy(POINT, (0.0, 400.0))), maxspeed="120")
    near_station = _way(2, (near_point.geometry[-1], anchor), maxspeed="30")
    corridor = build_corridors([near_point, near_station], POINT)[0]

    segments = speed_profile(corridor, [near_point, near_station], anchor)

    # Le 30 km/h de l'avant-gare doit sortir en premier, collé à l'origine.
    assert segments[0].maxspeed_kmh == 30.0
    assert segments[0].start_m == pytest.approx(0.0, abs=2.0)
    assert segments[-1].end_m == pytest.approx(1564.0, abs=5.0)


def test_speed_profile_keeps_only_the_ways_the_centreline_travels():
    # Dans un avant-gare, des voies parallèles passent à quelques mètres : un
    # filtre par distance latérale les confondrait toutes.
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    main = _way(1, (anchor, from_local_xy(POINT, (0.0, -500.0))), maxspeed="120")
    sibling = _way(
        2,
        (from_local_xy(POINT, (4.0, 1564.0)), from_local_xy(POINT, (4.0, -500.0))),
        maxspeed="30",
    )
    corridor = build_corridors([main, sibling], POINT, corridor_width_m=60.0)[0]

    assert corridor.segment_count == 2  # les deux voies forment bien un corridor
    assert [s.maxspeed_kmh for s in speed_profile(corridor, [main, sibling], anchor)] == [120.0]


def test_merged_length_does_not_count_overlaps_twice():
    def seg(start, end):
        return SpeedSegment(start, end, 30.0, False, False, "x")

    # Quatre voies parallèles décrivant le même avant-gare : 300 m de voie,
    # pas 1 200. Sans fusion, le total dépassait la longueur du parcours.
    assert merged_length_m([seg(0, 300)] * 4) == pytest.approx(300.0)
    assert merged_length_m([seg(0, 100), seg(50, 200), seg(400, 450)]) == pytest.approx(250.0)
    assert merged_length_m([]) == 0.0


# -- export cartographique ---------------------------------------------------


def _profiled_corridor():
    anchor = from_local_xy(POINT, (0.0, 1564.0))
    fast = _way(1, (anchor, from_local_xy(POINT, (0.0, 1000.0))), maxspeed="120")
    tunnel = _way(2, (fast.geometry[-1], from_local_xy(POINT, (0.0, 900.0))),
                  maxspeed="90", tunnel="yes")
    slow = _way(3, (tunnel.geometry[-1], from_local_xy(POINT, (0.0, -400.0))), maxspeed="30")
    ways = [fast, tunnel, slow]
    return build_corridors(ways, POINT)[0], ways, anchor


def test_geojson_carries_the_point_the_station_and_the_corridor():
    corridor, ways, anchor = _profiled_corridor()
    collection = to_geojson([corridor], ways, POINT, anchor, "Toulouse Matabiau")

    assert collection["type"] == "FeatureCollection"
    titles = [f["properties"].get("title", "") for f in collection["features"]]
    assert "Point d'observation" in titles
    assert "Toulouse Matabiau" in titles
    points = [f for f in collection["features"] if f["geometry"]["type"] == "Point"]
    assert len(points) == 2


def test_geojson_uses_longitude_first_as_the_format_requires():
    corridor, ways, anchor = _profiled_corridor()
    observer = to_geojson([corridor], ways, POINT, anchor)["features"][0]
    longitude, latitude = observer["geometry"]["coordinates"]

    assert longitude == pytest.approx(POINT[1])
    assert latitude == pytest.approx(POINT[0])


def test_geojson_colours_segments_by_speed_and_thickens_tunnels():
    corridor, ways, anchor = _profiled_corridor()
    features = to_geojson([corridor], ways, POINT, anchor)["features"]
    segments = [f for f in features if "maxspeed_kmh" in f["properties"]]

    by_speed = {f["properties"]["maxspeed_kmh"]: f["properties"] for f in segments}
    assert by_speed[30.0]["stroke"] != by_speed[120.0]["stroke"]
    assert by_speed[90.0]["tunnel"] is True
    assert by_speed[90.0]["stroke-width"] > by_speed[30.0]["stroke-width"]


def test_geojson_segments_are_drawn_where_they_belong():
    corridor, ways, anchor = _profiled_corridor()
    features = to_geojson([corridor], ways, POINT, anchor)["features"]
    tunnel = next(f for f in features if f["properties"].get("tunnel") is True)

    # Le tunnel court de 564 à 664 m de la gare : sa géométrie doit être
    # longue d'une centaine de mètres, pas de la ligne entière.
    coords = [(c[1], c[0]) for c in tunnel["geometry"]["coordinates"]]
    assert polyline_length_m(coords) == pytest.approx(100.0, abs=10.0)


def test_geojson_without_a_station_still_describes_the_corridors():
    corridor, ways, _ = _profiled_corridor()
    collection = to_geojson([corridor], ways, POINT)

    lines = [f for f in collection["features"] if f["geometry"]["type"] == "LineString"]
    assert len(lines) == 1  # la polyligne du corridor, sans profil de vitesse


def test_slicing_a_polyline_returns_the_requested_span():
    line = [from_local_xy(POINT, (0.0, y)) for y in (0.0, 500.0, 1000.0)]
    portion = slice_polyline(line, 200.0, 800.0)
    assert polyline_length_m(portion) == pytest.approx(600.0, abs=1.0)
    assert slice_polyline(line, 500.0, 500.0) == []


def test_a_way_stopping_short_of_the_point_is_not_a_separate_corridor():
    # Régression : `project_on_polyline` borne la projection aux extrémités.
    # Une voie parfaitement colinéaire qui s'arrête 383 m avant le point s'y
    # projette donc sur son extrémité, et la distance mesurée est
    # longitudinale, pas latérale. Sans garde, la même voie unique se
    # présentait comme deux corridors, dont un « à 383 m » qui n'existe pas.
    crossing = _way(1, (from_local_xy(POINT, (0.0, 383.0)), from_local_xy(POINT, (0.0, -500.0))))
    stopping = _way(2, (from_local_xy(POINT, (0.0, 1560.0)), from_local_xy(POINT, (0.0, 383.0))))

    corridors = build_corridors([crossing, stopping], POINT, max_distance_m=500)

    assert len(corridors) == 1
    assert corridors[0].distance_m == pytest.approx(0.0, abs=1.0)
    # Le tronçon écarté du groupement rejoint tout de même la polyligne.
    assert polyline_length_m(list(corridors[0].centerline)) == pytest.approx(2060.0, abs=10.0)


def test_a_way_ending_just_before_the_point_is_still_accepted():
    # OSM peut découper un tronçon à quelques mètres du point : la tolérance
    # d'extrémité évite de perdre une voie bien réelle.
    stub = _way(1, (from_local_xy(POINT, (0.0, 600.0)), from_local_xy(POINT, (0.0, 10.0))))
    assert len(build_corridors([stub], POINT, max_distance_m=500)) == 1


def test_a_genuinely_parallel_track_remains_its_own_corridor():
    # Le garde-fou ne doit pas fusionner deux lignes réellement distinctes.
    here = _way(1, _line(0, 0, "ns", length_m=3000))
    beside = _way(2, _line(300, 0, "ns", length_m=3000))

    corridors = build_corridors([here, beside], POINT, max_distance_m=500)

    assert len(corridors) == 2
    assert [round(c.distance_m) for c in corridors] == [0, 300]


def test_geojson_names_the_osm_way_behind_each_segment():
    # Près d'une gare, savoir si un tronçon à 30 km/h est une ligne principale
    # ou une voie de service change entièrement la lecture du profil.
    corridor, ways, anchor = _profiled_corridor()
    features = to_geojson([corridor], ways, POINT, anchor)["features"]
    segments = [f["properties"] for f in features if "maxspeed_kmh" in f["properties"]]

    assert segments
    assert all(p.get("voie") for p in segments)


def test_a_segment_describes_which_way_it_came_from():
    segment = SpeedSegment(0.0, 300.0, 30.0, False, False, "Ligne de Toulouse à Bayonne")
    described = segment.describe()
    assert "30 km/h" in described
    assert "Ligne de Toulouse à Bayonne" in described
