"""Tests de `PassageService` : rattachement, rafraîchissement, histogramme.

Tout se joue sans réseau (`use_realtime=False`, ou temps réel neutralisé par
un correctif) et sans thread : `refresh()` est appelé directement, comme le
ticket l'exige — le thread périodique n'est qu'une boucle autour, testée à
part.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.config import AppConfig, DataPaths
from nexttraintosee.predict import predict_passages
from nexttraintosee.realtime import RealtimeError
from nexttraintosee.service import PassageBinding, PassageService, bind_passage

PARIS = ZoneInfo("Europe/Paris")
MORNING = datetime(2026, 9, 9, 8, 0, tzinfo=PARIS)  # mercredi : jour ouvré du mini-GTFS


@pytest.fixture
def config(tmp_path: Path, gtfs_zip: Path, site) -> AppConfig:
    return AppConfig(
        site=site,
        data=DataPaths(gtfs_path=gtfs_zip, database=tmp_path / "journal.sqlite"),
    )


@pytest.fixture
def service(config: AppConfig) -> PassageService:
    return PassageService(config, use_realtime=False, refresh_every_s=3600.0)


# -- rafraîchissement, sans thread ---------------------------------------------


def test_refresh_is_callable_directly_without_starting_a_thread(service: PassageService):
    service.refresh(now=MORNING)
    response = service.next_response(now=MORNING)
    assert response["passages"], "le mini-GTFS doit produire des passages ce jour-là"
    assert response["realtime"] is False  # use_realtime=False : pas de temps réel


def test_a_realtime_failure_falls_back_to_the_theoretical_schedule(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    service = PassageService(config, use_realtime=True, refresh_every_s=3600.0)

    def _boom(*_args, **_kwargs):
        raise RealtimeError("flux injoignable (test)")

    monkeypatch.setattr("nexttraintosee.service.load_snapshot", _boom)
    service.refresh(now=MORNING)

    response = service.next_response(now=MORNING)
    assert response["realtime"] is False
    assert response["passages"], "l'absence de temps réel ne doit pas vider le cache"


def test_the_cache_is_recorded_in_the_store(service: PassageService, config: AppConfig):
    from nexttraintosee.store import Store

    service.refresh(now=MORNING)
    with Store(config.data.database) as store:
        _, predictions = store.counts(config.site.name)
    assert predictions > 0


# -- contrat de /api/next -------------------------------------------------------


def test_next_response_matches_the_contract_fields(service: PassageService):
    service.refresh(now=MORNING)
    response = service.next_response(now=MORNING, limit=10)

    assert set(response) == {"generated_at", "site", "realtime", "passages"}
    assert response["site"] == service.config.site.name

    whens = [p["when"] for p in response["passages"]]
    assert whens == sorted(whens)

    expected_fields = {
        "trip_id", "when", "announce_at", "uncertainty_s", "direction",
        "direction_label", "look", "branch_id", "branch_label", "category_id",
        "headsign", "route_label", "delay_s", "realtime", "speed_kmh",
    }
    for passage in response["passages"]:
        assert set(passage) == expected_fields


def test_direction_label_and_look_are_computed_server_side(service: PassageService):
    service.refresh(now=MORNING)
    response = service.next_response(now=MORNING, limit=10)

    by_direction = {p["direction"]: p for p in response["passages"]}
    assert "outbound" in by_direction
    assert by_direction["outbound"]["direction_label"] == "Depuis Matabiau"
    assert by_direction["outbound"]["look"] == "tunnel"

    assert "inbound" in by_direction
    assert by_direction["inbound"]["direction_label"] == "Vers Matabiau"
    assert by_direction["inbound"]["look"] == "sud"


def test_the_limit_is_clamped_between_one_and_ten(service: PassageService):
    service.refresh(now=MORNING)
    assert len(service.next_response(now=MORNING, limit=0)["passages"]) <= 1
    assert len(service.next_response(now=MORNING, limit=999)["passages"]) <= 10


def test_next_response_only_covers_a_twelve_hour_window(service: PassageService):
    service.refresh(now=MORNING)
    response = service.next_response(now=MORNING, limit=10)
    for passage in response["passages"]:
        when = datetime.fromisoformat(passage["when"])
        assert MORNING <= when <= MORNING + timedelta(hours=12)


# -- histogramme moyenné semaine / week-end -------------------------------------


def test_histogram_response_matches_the_contract_shape(service: PassageService):
    histogram = service.histogram_response(now=MORNING)

    assert histogram["first_hour"] == 5
    assert histogram["last_hour"] == 23
    assert histogram["weekday"]["label"] == "Semaine"
    assert histogram["weekend"]["label"] == "Week-end"
    assert histogram["weekday"]["days"] <= 5
    assert histogram["weekend"]["days"] <= 2

    for series in (histogram["weekday"], histogram["weekend"]):
        hours = series["hours"]
        assert [h["hour"] for h in hours] == list(range(5, 23))
        assert all(h["total"] >= 0 for h in hours)

    all_totals = [h["total"] for h in histogram["weekday"]["hours"]]
    all_totals += [h["total"] for h in histogram["weekend"]["hours"]]
    assert histogram["peak"] == max(all_totals)


def test_the_histogram_is_computed_once_and_cached(service: PassageService, monkeypatch):
    first = service.histogram_response(now=MORNING)

    calls = []
    original = predict_passages

    def _counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr("nexttraintosee.service.predict_passages", _counting)
    second = service.histogram_response(now=MORNING + timedelta(days=1))

    assert second == first
    assert not calls, "un histogramme déjà calculé ne doit pas être recalculé"


# -- rattachement d'une observation ---------------------------------------------


def test_bind_passage_returns_none_for_no_candidates():
    binding = bind_passage([], datetime(2026, 9, 9, 8, 0, tzinfo=PARIS), tolerance_s=180.0)
    assert binding == PassageBinding(None, False, ())


def test_bind_passage_refuses_two_comparable_candidates(service: PassageService):
    service.refresh(now=MORNING)
    predicted = sorted(
        predict_passages(service.feed, service.config.site, MORNING.date()),
        key=lambda p: p.when,
    )
    middle = predicted[0].when + (predicted[1].when - predicted[0].when) / 2

    binding = bind_passage(predicted, middle, tolerance_s=900.0)
    assert binding.passage is None
    assert binding.ambiguous is True
    assert len(binding.candidates) >= 2


def test_bind_passage_accepts_a_clearly_closer_candidate(service: PassageService):
    service.refresh(now=MORNING)
    predicted = sorted(
        predict_passages(service.feed, service.config.site, MORNING.date()),
        key=lambda p: p.when,
    )
    moment = predicted[0].when + timedelta(seconds=18)

    binding = bind_passage(predicted, moment, tolerance_s=180.0)
    assert binding.passage is predicted[0]
    assert binding.ambiguous is False


# -- POST /api/observe (via le service) -----------------------------------------


def test_observe_binds_a_seen_passage_to_the_nearest_prediction(service: PassageService):
    service.refresh(now=MORNING)
    predicted = sorted(service.next_response(now=MORNING, limit=10)["passages"], key=lambda p: p["when"])
    target = datetime.fromisoformat(predicted[0]["when"])
    observed_at = target + timedelta(seconds=12)

    result = service.observe(
        {"seen": True, "observed_at": observed_at.isoformat(), "precision_s": 3, "source": "app"}
    )
    assert result["recorded"] is True
    assert result["ambiguous"] is False
    assert result["bound_to"] is not None
    assert result["bound_to"]["trip_id"] == predicted[0]["trip_id"]
    assert result["gap_s"] == pytest.approx(12.0, abs=0.5)
    assert isinstance(result["id"], int)


def test_observe_leaves_an_ambiguous_observation_unbound(
    service: PassageService, monkeypatch: pytest.MonkeyPatch
):
    service.refresh(now=MORNING)
    predicted = sorted(
        predict_passages(service.feed, service.config.site, MORNING.date()),
        key=lambda p: p.when,
    )
    middle = predicted[0].when + (predicted[1].when - predicted[0].when) / 2

    # Les deux premiers passages du mini-GTFS sont à près de dix minutes
    # d'écart : une tolérance large est nécessaire pour que le point médian
    # les mette réellement à portée comparable l'un de l'autre.
    monkeypatch.setattr("nexttraintosee.service.DEFAULT_OBSERVE_TOLERANCE_S", 900.0)

    result = service.observe({"seen": True, "observed_at": middle.isoformat()})
    assert result["recorded"] is True
    assert result["ambiguous"] is True
    assert result["bound_to"] is None
    assert result["gap_s"] is None


def test_observe_records_a_not_seen_passage(service: PassageService):
    service.refresh(now=MORNING)
    result = service.observe(
        {"seen": False, "anchor": MORNING.replace(hour=3).isoformat(), "source": "app"}
    )
    assert result["recorded"] is True
    assert result["bound_to"] is None


def test_observe_rejects_a_body_missing_the_seen_field(service: PassageService):
    with pytest.raises(ValueError):
        service.observe({"observed_at": MORNING.isoformat()})


def test_observe_rejects_a_seen_body_without_observed_at(service: PassageService):
    with pytest.raises(ValueError):
        service.observe({"seen": True})


# -- annulation (DELETE /api/observe/{id}) ---------------------------------------


def test_delete_observation_removes_a_recorded_report(service: PassageService):
    service.refresh(now=MORNING)
    result = service.observe(
        {"seen": True, "observed_at": MORNING.isoformat(), "precision_s": 3, "source": "app"}
    )
    assert service.delete_observation(result["id"]) is True


def test_delete_observation_a_second_time_has_no_effect(service: PassageService):
    # Un « Annuler » relancé deux fois — double appui, latence réseau — ne
    # doit jamais échouer : rien à annuler la seconde fois n'est pas une erreur.
    service.refresh(now=MORNING)
    result = service.observe({"seen": False, "anchor": MORNING.isoformat()})
    assert service.delete_observation(result["id"]) is True
    assert service.delete_observation(result["id"]) is False


def test_delete_observation_rejects_an_unknown_id(service: PassageService):
    assert service.delete_observation(999_999) is False


def test_observe_rejects_a_non_object_body(service: PassageService):
    with pytest.raises(ValueError):
        service.observe(["not", "an", "object"])


# -- /api/health -----------------------------------------------------------------


def test_health_response_reports_feed_coverage_and_cache_size(service: PassageService):
    before = service.health_response(now=MORNING)
    assert before["ok"] is True
    assert before["feed_start"] == "2026-01-01"
    assert before["feed_end"] == "2026-12-31"
    assert before["last_refresh"] is None
    assert before["passages_cached"] == 0

    service.refresh(now=MORNING)
    after = service.health_response(now=MORNING + timedelta(seconds=30))
    assert after["last_refresh"] == MORNING.isoformat()
    assert after["realtime_age_s"] == pytest.approx(30.0)
    assert after["passages_cached"] > 0


# -- thread périodique : démarrage et arrêt propres ------------------------------


def test_the_background_thread_starts_and_stops_cleanly(config: AppConfig):
    service = PassageService(config, use_realtime=False, refresh_every_s=0.02)
    service.start()
    try:
        assert service._thread is not None
        assert service._thread.is_alive()
    finally:
        service.stop()

    assert service._thread is None


def test_stopping_an_unstarted_service_is_a_no_op(service: PassageService):
    service.stop()  # ne doit rien lever


def test_starting_twice_does_not_spawn_a_second_thread(config: AppConfig):
    service = PassageService(config, use_realtime=False, refresh_every_s=0.05)
    service.start()
    first_thread = service._thread
    service.start()
    try:
        assert service._thread is first_thread
    finally:
        service.stop()


# -- désignation de la circulation par l'observateur --------------------------


def test_designating_a_trip_binds_to_it_even_if_another_is_nearer(service: PassageService):
    # Le cœur du problème vécu sur le terrain : deux trains rapprochés, une
    # prédiction décalée, et l'observateur qui sait lequel il a vu. Sa
    # désignation doit l'emporter sur la proximité temporelle.
    service.refresh(now=MORNING)
    predicted = sorted(
        service.next_response(now=MORNING, limit=10)["passages"], key=lambda p: p["when"]
    )
    wanted, nearer = predicted[1], predicted[0]
    observed_at = datetime.fromisoformat(nearer["when"]) + timedelta(seconds=5)

    result = service.observe(
        {"seen": True, "observed_at": observed_at.isoformat(), "trip_id": wanted["trip_id"]}
    )

    assert result["binding_method"] == "designated"
    assert result["bound_to"]["trip_id"] == wanted["trip_id"]
    assert result["ambiguous"] is False


def test_designation_measures_a_gap_far_beyond_the_usual_tolerance(service: PassageService):
    # Sans désignation, un écart supérieur à la tolérance est invisible : le
    # rattachement par l'heure ne peut par construction mesurer que de petits
    # écarts, ce qui flatte la calibration.
    service.refresh(now=MORNING)
    predicted = sorted(
        service.next_response(now=MORNING, limit=10)["passages"], key=lambda p: p["when"]
    )
    target = predicted[0]
    far_off = datetime.fromisoformat(target["when"]) + timedelta(seconds=600)

    result = service.observe(
        {"seen": True, "observed_at": far_off.isoformat(), "trip_id": target["trip_id"]}
    )

    assert result["bound_to"]["trip_id"] == target["trip_id"]
    assert result["gap_s"] == pytest.approx(600.0, abs=1.0)


def test_an_unknown_designation_leaves_the_observation_unbound(service: PassageService):
    service.refresh(now=MORNING)
    result = service.observe(
        {"seen": True, "observed_at": MORNING.isoformat(), "trip_id": "T:INEXISTANT"}
    )

    assert result["recorded"] is True
    assert result["bound_to"] is None
    assert result["binding_method"] == "none"
    assert result["ambiguous"] is False


def test_without_a_designation_the_behaviour_is_unchanged(service: PassageService):
    service.refresh(now=MORNING)
    predicted = sorted(
        service.next_response(now=MORNING, limit=10)["passages"], key=lambda p: p["when"]
    )
    observed_at = datetime.fromisoformat(predicted[0]["when"]) + timedelta(seconds=12)

    result = service.observe({"seen": True, "observed_at": observed_at.isoformat()})

    assert result["binding_method"] == "nearest"
    assert result["bound_to"]["trip_id"] == predicted[0]["trip_id"]
