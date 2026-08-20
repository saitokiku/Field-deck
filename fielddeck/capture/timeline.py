"""The unified timeline.

One SQLite database per session holds every event, measurement, mark and
artifact from every subsystem on a single monotonic axis.  That is what makes
this answerable::

    fdctl session window --around <fault> --before 300ms --after 100ms

CAN frames, UART bytes, PSU current, scope captures and operator marks all
land in the same table with the same clock, so correlating them is a query
rather than a spreadsheet exercise.

Writes are batched.  A 1 kfps CAN bus would otherwise spend the session doing
fsyncs instead of capturing.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from fielddeck.common.events import Event
from fielddeck.common.models import CaptureArtifact, SessionMark
from fielddeck.common.timebase import monotonic_ns

__all__ = ["Timeline", "TimelineRow"]

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,
    event_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    monotonic_ns INTEGER NOT NULL,
    utc_ns       INTEGER NOT NULL,
    source       TEXT NOT NULL,
    severity     TEXT NOT NULL,
    device_id    TEXT,
    action       TEXT,
    permission   TEXT,
    request_id   TEXT,
    message      TEXT,
    payload      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_mono   ON events(monotonic_ns);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id);

CREATE TABLE IF NOT EXISTS measurements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    monotonic_ns INTEGER NOT NULL,
    utc_ns       INTEGER NOT NULL,
    device_id    TEXT,
    quantity     TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT
);
CREATE INDEX IF NOT EXISTS idx_meas_mono ON measurements(monotonic_ns);
CREATE INDEX IF NOT EXISTS idx_meas_q    ON measurements(quantity);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id        TEXT PRIMARY KEY,
    relative_path      TEXT NOT NULL,
    kind               TEXT NOT NULL,
    media_type         TEXT,
    size_bytes         INTEGER NOT NULL DEFAULT 0,
    sha256             TEXT,
    created_monotonic_ns INTEGER NOT NULL,
    created_utc_ns     INTEGER NOT NULL,
    device_id          TEXT,
    raw                INTEGER NOT NULL DEFAULT 1,
    source_artifact_ids TEXT,
    producer           TEXT,
    producer_version   TEXT,
    producer_config    TEXT,
    metadata           TEXT
);

CREATE TABLE IF NOT EXISTS marks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    monotonic_ns INTEGER NOT NULL,
    utc_ns       INTEGER NOT NULL,
    source       TEXT NOT NULL,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_marks_mono ON marks(monotonic_ns);
"""


class TimelineRow(dict):
    """A timeline entry.  Plain dict so it serialises straight to JSON."""


class Timeline:
    """Session-scoped SQLite store with batched event writes."""

    def __init__(self, path: Path, *, batch_size: int = 64, max_batch_age_s: float = 1.0) -> None:
        self.path = path
        self._batch_size = batch_size
        #: Batching trades durability for throughput. Bounding the batch by
        #: *age* as well as count keeps that trade honest: on a quiet session
        #: a count-only trigger can hold events in memory indefinitely, and a
        #: field instrument that loses power then loses exactly the records
        #: leading up to whatever caused it.
        self._max_batch_age_ns = int(max_batch_age_s * 1e9)
        self._last_flush_ns = monotonic_ns()
        self._pending: list[tuple[Any, ...]] = []
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # -- meta --------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -- events ------------------------------------------------------------

    def add_event(self, event: Event) -> None:
        self._pending.append(
            (
                event.seq,
                event.event_id,
                str(event.type),
                event.monotonic_ns,
                event.utc_ns,
                str(event.source),
                str(event.severity),
                event.device_id,
                event.action,
                str(event.permission) if event.permission else None,
                event.request_id,
                event.message,
                json.dumps(event.payload, default=str) if event.payload else None,
            )
        )
        if (
            len(self._pending) >= self._batch_size
            or monotonic_ns() - self._last_flush_ns >= self._max_batch_age_ns
        ):
            self.flush()

    def flush(self) -> None:
        self._last_flush_ns = monotonic_ns()
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self._conn.executemany(
            "INSERT OR REPLACE INTO events("
            "seq, event_id, type, monotonic_ns, utc_ns, source, severity, "
            "device_id, action, permission, request_id, message, payload"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )

    # -- measurements ------------------------------------------------------

    def add_measurement(
        self,
        *,
        monotonic_ns: int,
        utc_ns: int,
        quantity: str,
        value: float,
        device_id: str | None = None,
        unit: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO measurements(monotonic_ns, utc_ns, device_id, quantity, value, unit) "
            "VALUES(?,?,?,?,?,?)",
            (monotonic_ns, utc_ns, device_id, quantity, float(value), unit),
        )

    def measurements(
        self,
        *,
        quantity: str | None = None,
        device_id: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if quantity:
            clauses.append("quantity=?")
            args.append(quantity)
        if device_id:
            clauses.append("device_id=?")
            args.append(device_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        rows = self._conn.execute(
            # `where` is assembled only from fixed column names below; every
            # caller-supplied value goes through a ? placeholder in `args`.
            f"SELECT * FROM measurements {where} ORDER BY monotonic_ns LIMIT ?",  # noqa: S608
            args,
        ).fetchall()
        return [dict(row) for row in rows]

    # -- artifacts and marks ----------------------------------------------

    def add_artifact(self, artifact: CaptureArtifact) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO artifacts("
            "artifact_id, relative_path, kind, media_type, size_bytes, sha256, "
            "created_monotonic_ns, created_utc_ns, device_id, raw, "
            "source_artifact_ids, producer, producer_version, producer_config, metadata"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact.artifact_id,
                artifact.relative_path,
                artifact.kind,
                artifact.media_type,
                artifact.size_bytes,
                artifact.sha256,
                artifact.created_monotonic_ns,
                artifact.created_utc_ns,
                artifact.device_id,
                int(artifact.raw),
                json.dumps(artifact.source_artifact_ids),
                artifact.producer,
                artifact.producer_version,
                json.dumps(artifact.producer_config, default=str),
                json.dumps(artifact.metadata, default=str),
            ),
        )

    def artifacts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts ORDER BY created_monotonic_ns"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["raw"] = bool(item["raw"])
            for key in ("source_artifact_ids", "producer_config", "metadata"):
                if item.get(key):
                    item[key] = json.loads(item[key])
            out.append(item)
        return out

    def add_mark(self, mark: SessionMark) -> None:
        self._conn.execute(
            "INSERT INTO marks(label, monotonic_ns, utc_ns, source, note) VALUES(?,?,?,?,?)",
            (mark.label, mark.monotonic_ns, mark.utc_ns, str(mark.source), mark.note),
        )

    def marks(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM marks ORDER BY monotonic_ns").fetchall()
        return [dict(row) for row in rows]

    # -- queries -----------------------------------------------------------

    def events(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        types: Sequence[str] | None = None,
        device_id: str | None = None,
        since_monotonic_ns: int | None = None,
        severity_at_least: str | None = None,
    ) -> list[TimelineRow]:
        self.flush()
        clauses: list[str] = []
        args: list[Any] = []
        if types:
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            args.extend(types)
        if device_id:
            clauses.append("device_id=?")
            args.append(device_id)
        if since_monotonic_ns is not None:
            clauses.append("monotonic_ns >= ?")
            args.append(since_monotonic_ns)
        if severity_at_least:
            order = ["debug", "info", "warning", "error", "critical"]
            wanted = order[order.index(severity_at_least) :]
            clauses.append(f"severity IN ({','.join('?' * len(wanted))})")
            args.extend(wanted)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.extend([limit, offset])
        rows = self._conn.execute(
            # Same as above: fixed column names only, values are parameterised.
            f"SELECT * FROM events {where} ORDER BY monotonic_ns, seq LIMIT ? OFFSET ?",  # noqa: S608
            args,
        ).fetchall()
        return [self._row(row) for row in rows]

    def window(
        self,
        *,
        center_monotonic_ns: int,
        before_ms: float = 300.0,
        after_ms: float = 100.0,
        limit: int = 2000,
    ) -> dict[str, Any]:
        """Everything that happened around one instant.

        The flagship correlation query: hand it the timestamp of a fault and
        get back the CAN, serial, bench and operator activity on both sides
        of it, already ordered on the monotonic axis.
        """
        self.flush()
        start = center_monotonic_ns - int(before_ms * 1e6)
        end = center_monotonic_ns + int(after_ms * 1e6)
        events = self._conn.execute(
            "SELECT * FROM events WHERE monotonic_ns BETWEEN ? AND ? "
            "ORDER BY monotonic_ns, seq LIMIT ?",
            (start, end, limit),
        ).fetchall()
        measurements = self._conn.execute(
            "SELECT * FROM measurements WHERE monotonic_ns BETWEEN ? AND ? "
            "ORDER BY monotonic_ns LIMIT ?",
            (start, end, limit),
        ).fetchall()
        marks = self._conn.execute(
            "SELECT * FROM marks WHERE monotonic_ns BETWEEN ? AND ? ORDER BY monotonic_ns",
            (start, end),
        ).fetchall()
        return {
            "center_monotonic_ns": center_monotonic_ns,
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "events": [self._row(row) for row in events],
            "measurements": [dict(row) for row in measurements],
            "marks": [dict(row) for row in marks],
        }

    def find_event(self, *, type: str, nth: int = 0) -> TimelineRow | None:
        self.flush()
        row = self._conn.execute(
            "SELECT * FROM events WHERE type=? ORDER BY monotonic_ns LIMIT 1 OFFSET ?",
            (type, nth),
        ).fetchone()
        return self._row(row) if row else None

    def counts(self) -> dict[str, int]:
        self.flush()
        rows = self._conn.execute(
            "SELECT type, COUNT(*) AS n FROM events GROUP BY type ORDER BY n DESC"
        ).fetchall()
        return {row["type"]: row["n"] for row in rows}

    def summary(self) -> dict[str, Any]:
        self.flush()
        bounds = self._conn.execute(
            "SELECT MIN(monotonic_ns) AS lo, MAX(monotonic_ns) AS hi, COUNT(*) AS n FROM events"
        ).fetchone()
        return {
            "events": bounds["n"] or 0,
            "first_monotonic_ns": bounds["lo"],
            "last_monotonic_ns": bounds["hi"],
            "duration_s": ((bounds["hi"] - bounds["lo"]) / 1e9) if bounds["n"] else 0.0,
            "by_type": self.counts(),
            "artifacts": len(self.artifacts()),
            "marks": len(self.marks()),
        }

    @staticmethod
    def _row(row: sqlite3.Row) -> TimelineRow:
        item = TimelineRow(row)
        if item.get("payload"):
            with contextlib.suppress(json.JSONDecodeError):
                item["payload"] = json.loads(item["payload"])
        return item

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.flush()
        # Fold the WAL back into the main file so a copied session directory
        # is self-contained even if the -wal file is left behind.
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()

    def __enter__(self) -> Timeline:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_readonly(path: Path) -> Timeline:
    """Open an existing timeline for querying a finished session."""
    if not path.exists():
        raise FileNotFoundError(path)
    return Timeline(path)


def iter_rows(rows: Iterable[TimelineRow]) -> Iterable[dict[str, Any]]:  # pragma: no cover
    return (dict(row) for row in rows)
