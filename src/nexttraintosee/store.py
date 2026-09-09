"""Journal SQLite des prédictions et des passages observés.

Conserver les deux dans le même fichier permet de recaler le modèle a
posteriori : on rejoue une soirée de détections face aux passages qui étaient
prédits ce soir-là, et on en tire le biais et la dispersion réels.

C'est aussi ce qui donne du sens aux détections inexpliquées : sur plusieurs
jours, un « train fantôme » qui revient tous les mardis à la même heure est
presque sûrement un sillon fret régulier.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .motion import Regime
from .observation import Observation, ObservationKind
from .predict import Branch, Direction, Passage
from .sensor.base import Detection

SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site         TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    peak_at      TEXT    NOT NULL,
    ended_at     TEXT    NOT NULL,
    peak_db      REAL    NOT NULL,
    baseline_db  REAL    NOT NULL,
    sample_count INTEGER NOT NULL,
    UNIQUE (site, peak_at)
);

CREATE TABLE IF NOT EXISTS predictions (
    site         TEXT NOT NULL,
    trip_id      TEXT NOT NULL,
    direction    TEXT NOT NULL,
    anchor_time  TEXT NOT NULL,
    predicted_at TEXT NOT NULL,
    branch_id    TEXT NOT NULL,
    regime       TEXT NOT NULL,
    passes_at    TEXT NOT NULL,
    uncertainty_s REAL NOT NULL,
    speed_kmh    REAL NOT NULL,
    route_label  TEXT NOT NULL DEFAULT '',
    headsign     TEXT NOT NULL DEFAULT '',
    delay_s      INTEGER,
    PRIMARY KEY (site, trip_id, direction, anchor_time)
);

-- Historique append-only : `predictions` ne garde que la dernière estimation,
-- alors que mesurer la dérive d'une prédiction exige de conserver chacune.
CREATE TABLE IF NOT EXISTS prediction_samples (
    site         TEXT NOT NULL,
    trip_id      TEXT NOT NULL,
    direction    TEXT NOT NULL,
    anchor_time  TEXT NOT NULL,
    predicted_at TEXT NOT NULL,
    passes_at    TEXT NOT NULL,
    delay_s      INTEGER,
    PRIMARY KEY (site, trip_id, direction, anchor_time, predicted_at)
);

CREATE INDEX IF NOT EXISTS samples_by_passage ON prediction_samples
    (site, trip_id, direction, anchor_time, predicted_at);

-- Passages rapportés, capteur et humains confondus : c'est la même donnée.
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site        TEXT NOT NULL,
    observed_at TEXT,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    trip_id     TEXT,
    direction   TEXT,
    anchor_time TEXT,
    precision_s REAL NOT NULL DEFAULT 5.0,
    note        TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS observations_by_time ON observations (site, observed_at);

CREATE INDEX IF NOT EXISTS detections_by_time  ON detections (site, peak_at);
CREATE INDEX IF NOT EXISTS predictions_by_time ON predictions (site, passes_at);
"""


def _to_db(moment: datetime) -> str:
    """Normalise un instant en UTC avant stockage.

    Les instants sont comparés en SQL par ordre lexicographique : il faut donc
    qu'ils partagent tous le même décalage horaire, sinon un « 08:00+02:00 » et
    un « 07:00+00:00 » — pourtant simultanés — se compareraient à l'envers. On
    stocke donc systématiquement en UTC.
    """
    return moment.astimezone(timezone.utc).isoformat()


def _from_db(value: str) -> datetime:
    """Relit un instant stocké, ramené au fuseau local."""
    return datetime.fromisoformat(value).astimezone()


class Store:
    """Accès au journal. Utilisable comme gestionnaire de contexte."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # -- écriture ----------------------------------------------------------

    def record_detection(self, site: str, detection: Detection) -> None:
        """Enregistre un passage observé (idempotent sur l'instant du pic)."""
        self.connection.execute(
            """
            INSERT OR IGNORE INTO detections
                (site, started_at, peak_at, ended_at, peak_db, baseline_db, sample_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site,
                _to_db(detection.started_at),
                _to_db(detection.peak_at),
                _to_db(detection.ended_at),
                detection.peak_db,
                detection.baseline_db,
                detection.sample_count,
            ),
        )
        self.connection.commit()

    def record_passages(self, site: str, passages: Iterable[Passage], predicted_at: datetime) -> int:
        """Enregistre des passages prédits, en remplaçant la version précédente.

        Une même circulation est prédite plusieurs fois au fil de la journée, de
        plus en plus précisément à mesure que le temps réel se précise : on ne
        garde que la dernière estimation.
        """
        rows = [
            (
                site,
                passage.trip_id,
                passage.direction.value,
                _to_db(passage.anchor_time),
                _to_db(predicted_at),
                passage.branch.branch_id,
                passage.regime.value,
                _to_db(passage.when),
                passage.uncertainty_s,
                passage.speed_kmh,
                passage.route_label,
                passage.headsign,
                passage.delay_s,
            )
            for passage in passages
        ]
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO prediction_samples
                (site, trip_id, direction, anchor_time, predicted_at, passes_at, delay_s)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(r[0], r[1], r[2], r[3], r[4], r[7], r[12]) for r in rows],
        )
        self.connection.executemany(
            """
            INSERT OR REPLACE INTO predictions
                (site, trip_id, direction, anchor_time, predicted_at, branch_id,
                 regime, passes_at, uncertainty_s, speed_kmh, route_label, headsign, delay_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    # -- lecture -----------------------------------------------------------

    def detections_between(self, site: str, start: datetime, end: datetime) -> list[Detection]:
        """Passages observés sur une période."""
        cursor = self.connection.execute(
            "SELECT * FROM detections WHERE site = ? AND peak_at BETWEEN ? AND ? ORDER BY peak_at",
            (site, _to_db(start), _to_db(end)),
        )
        return [
            Detection(
                started_at=_from_db(row["started_at"]),
                peak_at=_from_db(row["peak_at"]),
                ended_at=_from_db(row["ended_at"]),
                peak_db=row["peak_db"],
                baseline_db=row["baseline_db"],
                sample_count=row["sample_count"],
            )
            for row in cursor
        ]

    def passages_between(
        self, site: str, start: datetime, end: datetime, branches: Sequence[Branch]
    ) -> list[Passage]:
        """Passages prédits sur une période.

        Les branches sont fournies par la configuration : la base ne stocke que
        leur identifiant, pour rester cohérente si la configuration évolue.
        """
        by_id = {branch.branch_id: branch for branch in branches}
        cursor = self.connection.execute(
            "SELECT * FROM predictions WHERE site = ? AND passes_at BETWEEN ? AND ? ORDER BY passes_at",
            (site, _to_db(start), _to_db(end)),
        )
        passages: list[Passage] = []
        for row in cursor:
            branch = by_id.get(row["branch_id"])
            if branch is None:
                # Branche supprimée de la configuration : la prédiction n'est
                # plus interprétable, on l'ignore plutôt que d'inventer.
                continue
            passages.append(
                Passage(
                    trip_id=row["trip_id"],
                    when=_from_db(row["passes_at"]),
                    uncertainty_s=row["uncertainty_s"],
                    branch=branch,
                    regime=Regime(row["regime"]),
                    direction=Direction(row["direction"]),
                    speed_kmh=row["speed_kmh"],
                    anchor_time=_from_db(row["anchor_time"]),
                    route_label=row["route_label"],
                    headsign=row["headsign"],
                    delay_s=row["delay_s"],
                )
            )
        return passages

    def record_observation(self, site: str, observation: Observation) -> None:
        """Enregistre un passage rapporté."""
        self.connection.execute(
            """
            INSERT INTO observations
                (site, observed_at, kind, source, trip_id, direction,
                 anchor_time, precision_s, note, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site,
                _to_db(observation.observed_at) if observation.observed_at else None,
                observation.kind.value,
                observation.source,
                observation.trip_id,
                observation.direction,
                _to_db(observation.anchor_time) if observation.anchor_time else None,
                observation.precision_s,
                observation.note,
                _to_db(datetime.now().astimezone()),
            ),
        )
        self.connection.commit()

    def observations_between(
        self, site: str, start: datetime, end: datetime
    ) -> list[Observation]:
        """Passages rapportés sur une période.

        Les passages annoncés mais non vus n'ont pas d'heure : ils sont datés
        par l'horaire de la circulation qu'ils infirment, pour rester
        retrouvables dans une fenêtre.
        """
        cursor = self.connection.execute(
            """
            SELECT * FROM observations
            WHERE site = ?
              AND COALESCE(observed_at, anchor_time, recorded_at) BETWEEN ? AND ?
            ORDER BY COALESCE(observed_at, anchor_time, recorded_at)
            """,
            (site, _to_db(start), _to_db(end)),
        )
        return [
            Observation(
                observed_at=_from_db(row["observed_at"]) if row["observed_at"] else None,
                kind=ObservationKind(row["kind"]),
                source=row["source"],
                trip_id=row["trip_id"],
                direction=row["direction"],
                anchor_time=_from_db(row["anchor_time"]) if row["anchor_time"] else None,
                precision_s=row["precision_s"],
                note=row["note"],
            )
            for row in cursor
        ]

    def prediction_samples(
        self, site: str, start: datetime, end: datetime
    ) -> dict[tuple[str, str, str], list[tuple[datetime, datetime, int | None]]]:
        """Historique des estimations, groupé par passage.

        Chaque passage est identifié par sa circulation, son sens et son horaire
        en gare ; la liste donne, dans l'ordre, les couples (instant du calcul,
        heure de passage estimée, retard appliqué).
        """
        cursor = self.connection.execute(
            """
            SELECT trip_id, direction, anchor_time, predicted_at, passes_at, delay_s
            FROM prediction_samples
            WHERE site = ? AND passes_at BETWEEN ? AND ?
            ORDER BY predicted_at
            """,
            (site, _to_db(start), _to_db(end)),
        )
        history: dict[tuple[str, str, str], list[tuple[datetime, datetime, int | None]]] = {}
        for row in cursor:
            key = (row["trip_id"], row["direction"], row["anchor_time"])
            history.setdefault(key, []).append(
                (_from_db(row["predicted_at"]), _from_db(row["passes_at"]), row["delay_s"])
            )
        return history

    def counts(self, site: str) -> tuple[int, int]:
        """Nombre de détections et de prédictions enregistrées."""
        detections = self.connection.execute(
            "SELECT COUNT(*) FROM detections WHERE site = ?", (site,)
        ).fetchone()[0]
        predictions = self.connection.execute(
            "SELECT COUNT(*) FROM predictions WHERE site = ?", (site,)
        ).fetchone()[0]
        return detections, predictions
