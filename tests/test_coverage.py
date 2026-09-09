from datetime import datetime, timedelta, timezone

import pytest

from nexttraintosee.coverage import (
    DEFAULT_BUCKETS,
    LeadBucket,
    analyse,
    summarise_delays,
)

PASSAGE = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def estimates(*items):
    """(minutes avant le passage, décalage de l'estimation en s, retard)."""
    return [
        (PASSAGE - timedelta(minutes=lead), PASSAGE + timedelta(seconds=offset), delay)
        for lead, offset, delay in items
    ]


def history(**passages):
    return {(name, "outbound", "anchor"): value for name, value in passages.items()}


# -- tranches d'échéance -------------------------------------------------------


def test_a_bucket_is_half_open():
    bucket = LeadBucket("test", 600.0, 1800.0)
    assert bucket.contains(600.0)
    assert bucket.contains(1799.0)
    assert not bucket.contains(1800.0)
    assert not bucket.contains(599.0)


def test_default_buckets_cover_every_lead_time():
    for lead in (0.0, 599.0, 600.0, 3599.0, 3600.0, 86_400.0):
        assert sum(1 for b in DEFAULT_BUCKETS if b.contains(lead)) == 1


# -- couverture temps réel -----------------------------------------------------


def test_realtime_share_is_measured_per_lead_time():
    # Loin de l'échéance, pas de temps réel ; près, il arrive.
    stats = {s.bucket.label: s for s in analyse(history(
        a=estimates((90, 0, None), (45, 0, None), (5, 0, 60)),
    ))}

    assert stats["plus d'une heure"].realtime_share == 0.0
    assert stats["30 à 60 min"].realtime_share == 0.0
    assert stats["moins de 10 min"].realtime_share == 1.0


def test_estimations_are_counted_in_their_bucket():
    stats = {s.bucket.label: s for s in analyse(history(
        a=estimates((90, 0, None), (20, 0, None), (15, 0, None)),
    ))}
    assert stats["plus d'une heure"].sample_count == 1
    assert stats["10 à 30 min"].sample_count == 2
    assert stats["moins de 10 min"].sample_count == 0


def test_empty_buckets_are_still_reported():
    # L'absence de données doit se voir plutôt que disparaître.
    stats = analyse(history(a=estimates((5, 0, 0))))
    assert len(stats) == len(DEFAULT_BUCKETS)
    assert any(s.sample_count == 0 for s in stats)


def test_a_bucket_without_samples_has_no_share_or_drift():
    empty = analyse(history(a=estimates((5, 0, 0))))[-1]
    assert empty.sample_count == 0
    assert empty.realtime_share == 0.0
    assert empty.drift_median_s is None
    assert empty.drift_worst_s is None


# -- dérive --------------------------------------------------------------------


def test_drift_is_measured_against_the_final_estimate():
    # L'estimation lointaine annonçait 120 s trop tôt ; la dernière fait foi.
    stats = {s.bucket.label: s for s in analyse(history(
        a=estimates((90, -120, None), (5, 0, 0)),
    ))}
    assert stats["plus d'une heure"].drift_median_s == pytest.approx(120.0)
    assert stats["moins de 10 min"].drift_median_s == pytest.approx(0.0)


def test_a_stable_prediction_shows_no_drift():
    stats = analyse(history(a=estimates((90, 0, None), (30, 0, None), (5, 0, 0))))
    assert all(s.drift_median_s in (None, 0.0) for s in stats)


def test_drift_is_unsigned():
    # Avancer ou retarder de 60 s est également gênant pour qui attend.
    early = analyse(history(a=estimates((20, -60, None), (2, 0, 0))))
    late = analyse(history(a=estimates((20, 60, None), (2, 0, 0))))
    labels = {s.bucket.label: s.drift_median_s for s in early}
    assert labels["10 à 30 min"] == pytest.approx(60.0)
    assert {s.bucket.label: s.drift_median_s for s in late}["10 à 30 min"] == pytest.approx(60.0)


def test_the_worst_case_reports_the_upper_tail():
    # Neuf estimations stables, une mauvaise de 300 s. L'échéance se compte sur
    # l'heure estimée, pas sur l'heure finale : la mauvaise est calculée
    # 15 minutes avant, mais annonce un passage 300 s plus tard, donc 20 minutes
    # après le calcul — elle reste dans la même tranche.
    items = [(20, 0, None)] * 9 + [(15, 300, None)]
    stats = {s.bucket.label: s for s in analyse(history(a=estimates(*items, (1, 0, 0))))}
    entry = stats["10 à 30 min"]
    assert entry.sample_count == 10
    assert entry.drift_median_s == pytest.approx(0.0)
    assert entry.drift_worst_s == pytest.approx(300.0)


def test_the_lead_time_is_counted_on_the_estimated_hour():
    # C'est ce que voit l'utilisateur : « dans X minutes » se lit sur l'heure
    # annoncée, pas sur celle qui se vérifiera.
    stats = {s.bucket.label: s for s in analyse(history(
        a=estimates((20, 600, None), (1, 0, 0)),
    ))}
    # Calculée 20 min avant, mais annonçant 10 min plus tard : 30 min d'échéance.
    assert stats["30 à 60 min"].sample_count == 1
    assert stats["10 à 30 min"].sample_count == 0


def test_estimates_made_after_the_passage_are_ignored():
    # Une estimation postérieure au passage n'apprend rien sur l'anticipation.
    stats = analyse(history(a=[(PASSAGE + timedelta(minutes=5), PASSAGE, 0)]))
    assert sum(s.sample_count for s in stats) == 0


def test_several_passages_are_pooled():
    stats = {s.bucket.label: s for s in analyse(history(
        a=estimates((20, 0, None), (2, 0, 0)),
        b=estimates((20, 120, None), (2, 0, 0)),
    ))}
    assert stats["10 à 30 min"].sample_count == 2
    assert stats["10 à 30 min"].drift_median_s == pytest.approx(60.0)


# -- retards -------------------------------------------------------------------


def test_delays_are_summarised_on_the_last_estimate():
    summary = summarise_delays(history(
        a=estimates((60, 0, None), (5, 300, 300)),
        b=estimates((60, 0, None), (5, 0, 0)),
    ))
    assert summary.passage_count == 2
    assert summary.with_realtime == 2
    assert summary.on_time == 1
    assert summary.median_delay_s == pytest.approx(150.0)
    assert summary.worst_delay_s == 300


def test_passages_never_seen_in_realtime_are_counted_apart():
    summary = summarise_delays(history(
        a=estimates((60, 0, None)),
        b=estimates((5, 60, 60)),
    ))
    assert summary.passage_count == 2
    assert summary.with_realtime == 1
    assert summary.realtime_share == pytest.approx(0.5)


def test_an_early_train_counts_as_a_delay_too():
    summary = summarise_delays(history(a=estimates((5, -180, -180))))
    assert summary.worst_delay_s == -180
    assert summary.on_time == 0


def test_an_empty_history_is_harmless():
    summary = summarise_delays({})
    assert summary.passage_count == 0
    assert summary.realtime_share == 0.0
    assert summary.on_time_share == 0.0
    assert summary.median_delay_s is None
