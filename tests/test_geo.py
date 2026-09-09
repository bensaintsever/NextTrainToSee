import math

import pytest

from nexttraintosee.geo import (
    axis_distance_deg,
    bearing_delta_deg,
    bounding_box,
    cumulative_lengths_m,
    densify,
    from_local_xy,
    haversine_m,
    initial_bearing_deg,
    polyline_length_m,
    project_on_polyline,
    to_local_xy,
)

POINT = (43.597833, 1.458194)


def test_haversine_one_degree_of_latitude():
    # Un degré de latitude vaut ~111.19 km quelle que soit la longitude.
    assert haversine_m((43.0, 1.0), (44.0, 1.0)) == pytest.approx(111_195, rel=1e-3)


def test_haversine_is_zero_on_identical_points():
    assert haversine_m(POINT, POINT) == 0.0


def test_initial_bearing_cardinal_directions():
    assert initial_bearing_deg((43.0, 1.0), (44.0, 1.0)) == pytest.approx(0.0, abs=1e-6)
    assert initial_bearing_deg((43.0, 1.0), (43.0, 2.0)) == pytest.approx(90.0, abs=0.5)
    assert initial_bearing_deg((44.0, 1.0), (43.0, 1.0)) == pytest.approx(180.0, abs=1e-6)


def test_bearing_delta_wraps_around_north():
    assert bearing_delta_deg(350.0, 10.0) == pytest.approx(20.0)
    assert bearing_delta_deg(10.0, 350.0) == pytest.approx(-20.0)


def test_axis_distance_treats_opposite_directions_as_parallel():
    # Deux voies parcourues en sens inverse : caps opposés, même axe.
    assert axis_distance_deg(10.0, 190.0) == pytest.approx(0.0)
    assert axis_distance_deg(0.0, 90.0) == pytest.approx(90.0)
    assert axis_distance_deg(30.0, 200.0) == pytest.approx(10.0)


def test_local_xy_roundtrip():
    other = (43.6112, 1.4535)
    assert from_local_xy(POINT, to_local_xy(POINT, other))[0] == pytest.approx(other[0], abs=1e-9)
    assert from_local_xy(POINT, to_local_xy(POINT, other))[1] == pytest.approx(other[1], abs=1e-9)


def test_local_xy_matches_haversine():
    other = (43.6112, 1.4535)
    east, north = to_local_xy(POINT, other)
    assert math.hypot(east, north) == pytest.approx(haversine_m(POINT, other), rel=1e-4)


def test_project_on_polyline_perpendicular_offset():
    # Voie est-ouest le long de la latitude du point, décalée de ~100 m au nord.
    north_offset = 100.0
    shifted_lat = from_local_xy(POINT, (0.0, north_offset))[0]
    line = [(shifted_lat, POINT[1] - 0.01), (shifted_lat, POINT[1] + 0.01)]

    projection = project_on_polyline(POINT, line)

    assert projection.distance_m == pytest.approx(north_offset, abs=0.5)
    assert projection.along_m == pytest.approx(polyline_length_m(line) / 2, rel=1e-3)
    assert projection.segment_index == 0


def test_project_on_polyline_clamps_to_endpoints():
    # Le point est au-delà de l'extrémité du segment : la projection est bornée.
    line = [(43.60, 1.50), (43.61, 1.50)]
    projection = project_on_polyline((43.50, 1.50), line)
    assert projection.along_m == pytest.approx(0.0, abs=1e-6)
    assert projection.point[0] == pytest.approx(43.60, abs=1e-6)


def test_project_on_polyline_picks_nearest_segment():
    line = [(43.50, 1.45), (43.60, 1.45), (43.60, 1.55)]
    projection = project_on_polyline((43.601, 1.50), line)
    assert projection.segment_index == 1


def test_project_on_polyline_rejects_degenerate_input():
    with pytest.raises(ValueError):
        project_on_polyline(POINT, [POINT])


def test_cumulative_lengths_are_monotonic():
    line = [(43.50, 1.45), (43.60, 1.45), (43.60, 1.55)]
    cumulative = cumulative_lengths_m(line)
    assert cumulative[0] == 0.0
    assert cumulative == sorted(cumulative)
    assert cumulative[-1] == pytest.approx(polyline_length_m(line))


def test_densify_bounds_segment_length_and_preserves_geometry():
    line = [(43.50, 1.45), (43.60, 1.45)]
    dense = densify(line, 500.0)

    assert dense[0] == line[0]
    assert dense[-1] == line[-1]
    steps = [haversine_m(dense[i], dense[i + 1]) for i in range(len(dense) - 1)]
    assert max(steps) <= 500.0
    assert polyline_length_m(dense) == pytest.approx(polyline_length_m(line), rel=1e-6)


def test_densify_leaves_short_segments_untouched():
    line = [(43.50, 1.45), (43.5001, 1.45)]
    assert densify(line, 500.0) == line


def test_bounding_box_with_margin_contains_points():
    points = [(43.50, 1.45), (43.60, 1.55)]
    south, west, north, east = bounding_box(points, margin_m=1000.0)
    assert south < 43.50 and north > 43.60
    assert west < 1.45 and east > 1.55
